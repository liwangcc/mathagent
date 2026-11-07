"""Business-path tests for opportunistic template preprocessing."""
from __future__ import annotations

import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models_accelerator  # noqa: F401
import models_user  # noqa: F401
from database import Base
from models_accelerator import AcceleratorJob
from models_user import User
from services.accelerator_business import (
    AcceleratorEvidence,
    claim_cpu_fallback,
    finish_business_preprocessing,
)
from services.paper_cache import PaperCache
from services.template_evidence import EvidenceResult


def _session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'business.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    db.add(User(id=1, name="教师", role="teacher", status="active"))
    db.commit()
    db.close()
    return Session


def _client_capabilities(**values):
    return {
        "artifact_contracts": ["client-evidence-v1"],
        **values,
    }


def _job(
    Session,
    tmp_path,
    *,
    status="queued",
    artifact=False,
    job_id="job-1",
    page_type_id="page_1",
):
    source = tmp_path / "source.png"
    default_job = job_id == "job-1" and page_type_id == "page_1"
    template = tmp_path / (
        "template.png" if default_job else f"template-{job_id}.png"
    )
    source.write_bytes(b"source")
    template_bytes = b"template" if default_job else f"template:{job_id}".encode()
    template.write_bytes(template_bytes)
    artifact_path = tmp_path / (
        "evidence-0123456789abcdef0123456789abcdef.png"
        if default_job else f"evidence-{job_id}.png"
    )
    if artifact:
        artifact_path.write_bytes(b"evidence")
    validation = {
        "page_type_id": page_type_id,
        "question_numbers": ["1"],
        "blank_question_numbers": [],
        "evidence_diagnostics": {"ownership": "answer_regions"},
    }
    db = Session()
    row = AcceleratorJob(
        id=job_id,
        batch_id="batch-1",
        paper_id="paper-1",
        teacher_id=1,
        source_path=str(source),
        source_sha256=hashlib.sha256(b"source").hexdigest(),
        template_id=f"template-1:{page_type_id}",
        template_path=str(template),
        template_sha256=hashlib.sha256(template_bytes).hexdigest(),
        layout_snapshot_json=json.dumps({
            "page_type_id": page_type_id,
            "answer_regions": [],
        }),
        layout_snapshot_sha256=(
            "a" * 64
            if default_job
            else hashlib.sha256(page_type_id.encode()).hexdigest()
        ),
        stage="template_registration",
        lane="web_gpu",
        algorithm_version="template-registration-v2",
        status=status,
        deadline_at=datetime.utcnow() + timedelta(minutes=10),
        server_validation_status="accepted" if status == "validated" else "not_started",
        server_validation_json=json.dumps(validation),
        server_artifact_path=str(artifact_path) if artifact else None,
        server_artifact_sha256=(
            hashlib.sha256(b"evidence").hexdigest() if artifact else None
        ),
    )
    db.add(row)
    db.commit()
    job_id = row.id
    db.close()
    return job_id, artifact_path


def test_cpu_fallback_claim_and_finish_are_persistent(tmp_path, monkeypatch):
    Session = _session_factory(tmp_path)
    monkeypatch.setattr("services.accelerator_business.SessionLocal", Session)
    job_id, _artifact = _job(Session, tmp_path)
    handle = AcceleratorEvidence(None, (job_id,))

    claimed = claim_cpu_fallback(handle)
    assert claimed == handle
    db = Session()
    row = db.get(AcceleratorJob, job_id)
    assert row.status == "fallback_running"
    assert row.server_validation_status == "cpu_running"
    db.close()

    assert finish_business_preprocessing(
        claimed,
        fallback_succeeded=True,
    ) == 1
    db = Session()
    row = db.get(AcceleratorJob, job_id)
    assert row.status == "fallback_completed"
    assert row.server_validation_status == "cpu_completed"
    assert row.consumed_at is not None
    db.close()


@pytest.mark.parametrize("remote_status", [
    "leased", "candidate_received", "server_validating",
])
@pytest.mark.parametrize("lease_timing", ["before_claim", "before_cas"])
@pytest.mark.parametrize("with_sibling", [False, True])
def test_cpu_fallback_preserves_new_remote_owner(
    tmp_path, monkeypatch, remote_status, lease_timing, with_sibling
):
    from services import accelerator_business as business
    from services import accelerator_jobs as jobs

    Session = _session_factory(tmp_path)
    monkeypatch.setattr(business, "SessionLocal", Session)
    job_id, _ = _job(Session, tmp_path)
    job_ids = (job_id,)
    sibling_artifact = None
    if with_sibling:
        sibling_id, sibling_artifact = _job(
            Session, tmp_path, job_id="job-2", page_type_id="page_2",
            artifact=True,
        )
        job_ids += (sibling_id,)
    # PaperCache may deliberately request CPU after observing unavailable.
    # That observation is stale as soon as a node obtains a fresh lease.
    observed = AcceleratorEvidence(None, job_ids, state="unavailable")
    fallback = AcceleratorEvidence(None, observed.job_ids, state="cpu_fallback")
    with Session() as db:
        node, token = jobs.register_node(
            db, owner_user_id=1, name="fresh-lease-node", lane="web_gpu",
        )
        node = jobs.authenticate_node(db, node.id, token)
        jobs.heartbeat(
            db, node,
            capabilities=_client_capabilities(stages=["template_registration"]),
            algorithm_version=jobs.ALGORITHM_VERSION, free_slots=1,
        )
        node_id = node.id

    lease_snapshot = {}

    def remote_claim():
        with Session() as other:
            node = other.get(models_accelerator.AcceleratorNode, node_id)
            leased = jobs.lease_next_job(other, node)
            assert leased is not None
            row, _secret = leased
            assert row.id == job_id
            assert row.lease_expires_at > datetime.utcnow()
            row.status = remote_status
            if remote_status != "leased":
                # Model the subsequent receipt/validation ownership boundary.
                row.server_validation_status = "pending"
                row.candidate_received_at = datetime.utcnow()
            other.commit()
            lease_snapshot.update({
                key: getattr(row, key) for key in (
                    "status", "assigned_node_id", "lease_id_hash",
                    "lease_expires_at", "server_validation_status",
                    "candidate_received_at", "attempt_count", "fallback_at",
                )
            })

    if lease_timing == "before_claim":
        remote_claim()
    else:
        real_update = business.update

        def race_update(model):
            if not lease_snapshot:
                # Commit after claim_cpu_fallback's SELECT, before its CAS.
                remote_claim()
            return real_update(model)

        monkeypatch.setattr(business, "update", race_update)

    claimed = claim_cpu_fallback(fallback)

    assert lease_snapshot, "the remote producer must actually win the race"
    assert claimed is None
    with Session() as verify:
        row = verify.get(AcceleratorJob, job_id)
        assert {key: getattr(row, key) for key in lease_snapshot} == lease_snapshot
        if with_sibling:
            sibling = verify.get(AcceleratorJob, sibling_id)
            assert sibling.status == "queued"
            assert sibling.fallback_at is None
            assert sibling.server_validation_status == "not_started"
            assert sibling.server_artifact_path == str(sibling_artifact)
            assert sibling_artifact.read_bytes() == b"evidence"


@pytest.mark.parametrize("status,exhausted,expected_error", [
    ("leased", False, "lease_expired_requeued"),
    ("leased", True, "lease_attempts_exhausted"),
    ("candidate_received", False, "server_validation_deadline_exceeded"),
    ("server_validating", False, "server_validation_deadline_exceeded"),
])
def test_cpu_fallback_claims_work_only_after_expiry_reclaims_it(
    tmp_path, monkeypatch, status, exhausted, expected_error
):
    from services import accelerator_business as business
    from services import accelerator_jobs as jobs

    Session = _session_factory(tmp_path)
    monkeypatch.setattr(business, "SessionLocal", Session)
    job_id, _ = _job(Session, tmp_path, status=status)
    with Session() as db:
        row = db.get(AcceleratorJob, job_id)
        row.lease_id_hash = "expired-lease-hash"
        row.lease_expires_at = jobs.utcnow() - timedelta(seconds=1)
        row.attempt_count = jobs.MAX_ATTEMPTS if exhausted else 1
        if status != "leased":
            row.deadline_at = jobs.utcnow() - timedelta(seconds=1)
        db.commit()

    handle = AcceleratorEvidence(None, (job_id,))
    requeued = status == "leased" and not exhausted
    # Template registration is offload-only: exhausted/deadline-expired jobs
    # close as fallback_failed; the existing closed-job retry has no job IDs.
    expected_handle = handle if requeued else AcceleratorEvidence(None)
    assert claim_cpu_fallback(handle) == expected_handle
    with Session() as verify:
        row = verify.get(AcceleratorJob, job_id)
        assert row.status == ("fallback_running" if requeued else "fallback_failed")
        if requeued:
            assert row.server_validation_status == "cpu_running"
        elif status != "leased":
            assert row.server_validation_status == "timed_out"
        # This reason proves the existing reaper, not the CPU CAS, revoked it.
        assert row.error_code == expected_error
        assert row.lease_id_hash is None
        assert row.lease_expires_at is None


def test_cpu_fallback_claims_entire_unowned_group(tmp_path, monkeypatch):
    Session = _session_factory(tmp_path)
    monkeypatch.setattr("services.accelerator_business.SessionLocal", Session)
    first, _ = _job(Session, tmp_path)
    second, _ = _job(
        Session, tmp_path, job_id="job-2", page_type_id="page_2",
        status="server_fallback",
    )
    handle = AcceleratorEvidence(None, (first, second))

    assert claim_cpu_fallback(handle) == handle
    with Session() as verify:
        rows = verify.query(AcceleratorJob).all()
        assert len(rows) == 2
        assert all(row.status == "fallback_running" for row in rows)
        assert all(row.server_validation_status == "cpu_running" for row in rows)
        assert all(row.fallback_at is not None for row in rows)
    assert claim_cpu_fallback(handle) is None


def test_cpu_fallback_cas_loser_cannot_become_second_owner(tmp_path, monkeypatch):
    from services import accelerator_business as business

    Session = _session_factory(tmp_path)
    monkeypatch.setattr(business, "SessionLocal", Session)
    job_id, _ = _job(Session, tmp_path)
    handle = AcceleratorEvidence(None, (job_id,))
    real_update = business.update
    raced = False
    winners = []

    def race_update(model):
        nonlocal raced
        if not raced:
            raced = True
            winners.append(claim_cpu_fallback(handle))
        return real_update(model)

    monkeypatch.setattr(business, "update", race_update)

    assert claim_cpu_fallback(handle) is None
    assert winners == [handle]
    with Session() as verify:
        row = verify.get(AcceleratorJob, job_id)
        assert row.status == "fallback_running"
        assert row.server_validation_status == "cpu_running"
    assert finish_business_preprocessing(winners[0], fallback_succeeded=True) == 1


def test_validated_artifact_wins_race_at_fallback_claim(tmp_path, monkeypatch):
    Session = _session_factory(tmp_path)
    monkeypatch.setattr("services.accelerator_business.SessionLocal", Session)
    job_id, artifact = _job(Session, tmp_path, status="validated", artifact=True)

    claimed = claim_cpu_fallback(AcceleratorEvidence(None, (job_id,)))

    assert claimed is not None
    assert claimed.used_job_id == job_id
    assert claimed.evidence is not None
    assert Path(claimed.evidence.evidence_path) == artifact
    db = Session()
    assert db.get(AcceleratorJob, job_id).status == "validated"
    db.close()


def test_validated_client_evidence_is_not_reported_as_server_cpu_replay(
    tmp_path, monkeypatch
):
    from services import accelerator_business as business

    Session = _session_factory(tmp_path)
    monkeypatch.setattr(business, "SessionLocal", Session)
    job_id, _artifact = _job(Session, tmp_path, status="validated", artifact=True)
    db = Session()
    row = db.get(AcceleratorJob, job_id)
    validation = json.loads(row.server_validation_json)
    validation["validator"] = "trusted_client_evidence_v1"
    row.server_validation_json = json.dumps(validation)
    db.commit()

    evidence = business._validated_evidence(row)

    assert evidence is not None
    assert evidence.diagnostics["accelerator"] == {
        "job_id": job_id,
        "server_validated": True,
        "candidate_reused_for_acceptance": True,
        "evidence_authority": "trusted_client_evidence",
        "server_source_transform_replayed": False,
        "server_evidence_rendered": False,
    }
    db.close()


def test_historical_client_full_column_artifact_is_not_consumed(
    tmp_path, monkeypatch
):
    from services import accelerator_business as business

    Session = _session_factory(tmp_path)
    monkeypatch.setattr(business, "SessionLocal", Session)
    job_id, _artifact = _job(Session, tmp_path, status="validated", artifact=True)
    db = Session()
    row = db.get(AcceleratorJob, job_id)
    validation = json.loads(row.server_validation_json)
    validation["validator"] = "trusted_client_evidence_full_column_v1"
    row.server_validation_json = json.dumps(validation)
    db.commit()

    assert business._validated_evidence(row) is None
    db.close()


@pytest.mark.parametrize("missing_flag", [
    None, "bound_node_identity_validated", "local_quality_report_validated",
    "client_answer_source_complete",
])
def test_bound_full_column_consumer_requires_new_acceptance_contract(
    tmp_path, missing_flag
):
    from services import accelerator_artifacts as artifacts
    from services import accelerator_business as business

    Session = _session_factory(tmp_path)
    job_id, _ = _job(Session, tmp_path, status="validated", artifact=True)
    image = tmp_path / "evidence_01.png"
    image.write_bytes(b"bound-column-evidence")
    index = tmp_path / "manifest.json"
    index.write_text(json.dumps({
        "contract_version": artifacts.CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION,
        "evidence_images": [{
            "kind": "evidence_01",
            "byte_size": image.stat().st_size,
            "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
        }],
    }), encoding="utf-8")
    db = Session()
    row = db.get(AcceleratorJob, job_id)
    details = json.loads(row.server_validation_json)
    details.update({
        "validator": artifacts.BOUND_CLIENT_FULL_COLUMN_VALIDATOR,
        "bound_node_identity_validated": True,
        "local_quality_report_validated": True,
        "client_answer_source_complete": True,
        "server_source_transform_replayed": False,
        "server_evidence_rendered": False,
    })
    if missing_flag:
        details.pop(missing_flag)
    row.server_validation_json = json.dumps(details)
    row.server_artifact_path = str(index)
    row.server_artifact_sha256 = hashlib.sha256(index.read_bytes()).hexdigest()
    db.commit()
    evidence = business._validated_evidence(row)
    if missing_flag:
        assert evidence is None
    else:
        assert evidence is not None
        accelerator = evidence.diagnostics["accelerator"]
        assert accelerator["evidence_authority"] == "bound_client_full_columns"
        assert accelerator["bound_node_identity_validated"] is True
        assert accelerator["local_quality_report_validated"] is True
        assert accelerator["server_source_transform_replayed"] is False
    db.close()


def test_validated_artifact_wins_when_fallback_cas_loses_race(
    tmp_path, monkeypatch
):
    Session = _session_factory(tmp_path)
    monkeypatch.setattr("services.accelerator_business.SessionLocal", Session)
    job_id, artifact = _job(Session, tmp_path, status="server_validating")

    from services import accelerator_business as business

    real_update = business.update
    raced = False

    def race_update(model):
        nonlocal raced
        if not raced:
            raced = True
            other = Session()
            row = other.get(AcceleratorJob, job_id)
            artifact.write_bytes(b"evidence")
            row.status = "validated"
            row.server_validation_status = "accepted"
            row.server_artifact_path = str(artifact)
            row.server_artifact_sha256 = hashlib.sha256(b"evidence").hexdigest()
            other.commit()
            other.close()
        return real_update(model)

    monkeypatch.setattr(business, "update", race_update)

    claimed = claim_cpu_fallback(AcceleratorEvidence(None, (job_id,)))

    assert claimed is not None
    assert claimed.used_job_id == job_id
    assert claimed.evidence is not None
    assert Path(claimed.evidence.evidence_path) == artifact


def test_second_fallback_claim_does_not_run_duplicate_cpu_matcher(
    tmp_path, monkeypatch
):
    Session = _session_factory(tmp_path)
    monkeypatch.setattr("services.accelerator_business.SessionLocal", Session)
    job_id, _artifact = _job(Session, tmp_path, status="fallback_running")
    db = Session()
    db.get(AcceleratorJob, job_id).fallback_at = datetime.utcnow()
    db.commit()
    db.close()

    claimed = claim_cpu_fallback(AcceleratorEvidence(None, (job_id,)))

    assert claimed is None


def test_stale_fallback_owner_allows_later_cpu_retry(tmp_path, monkeypatch):
    Session = _session_factory(tmp_path)
    monkeypatch.setattr("services.accelerator_business.SessionLocal", Session)
    job_id, _artifact = _job(Session, tmp_path, status="fallback_running")
    db = Session()
    row = db.get(AcceleratorJob, job_id)
    row.fallback_at = datetime.utcnow() - timedelta(minutes=11)
    db.commit()
    db.close()

    claimed = claim_cpu_fallback(AcceleratorEvidence(None, (job_id,)))

    assert claimed == AcceleratorEvidence(None)
    db = Session()
    row = db.get(AcceleratorJob, job_id)
    assert row.status == "fallback_failed"
    assert row.server_validation_status == "cpu_abandoned"
    assert row.error_code == "cpu_fallback_owner_lost"
    db.close()


def test_abandoned_fallback_cannot_be_closed_by_stale_owner(
    tmp_path, monkeypatch
):
    Session = _session_factory(tmp_path)
    monkeypatch.setattr("services.accelerator_business.SessionLocal", Session)
    job_id, _artifact = _job(Session, tmp_path, status="fallback_running")
    db = Session()
    row = db.get(AcceleratorJob, job_id)
    row.fallback_at = datetime.utcnow() - timedelta(minutes=11)
    db.commit()
    db.close()

    from services import accelerator_business as business
    from services import accelerator_jobs as jobs

    real_update = business.update
    reaped = False

    def reap_before_finish_update(model):
        nonlocal reaped
        if not reaped:
            reaped = True
            authoritative = Session()
            assert jobs.expire_leases(authoritative) == 1
            authoritative.close()
        return real_update(model)

    monkeypatch.setattr(business, "update", reap_before_finish_update)

    assert finish_business_preprocessing(
        AcceleratorEvidence(None, (job_id,)),
        fallback_succeeded=True,
    ) == 0
    db = Session()
    row = db.get(AcceleratorJob, job_id)
    assert row.status == "fallback_failed"
    assert row.server_validation_status == "cpu_abandoned"
    db.close()


def test_closed_fallback_allows_later_request_to_retry_on_cpu(
    tmp_path, monkeypatch
):
    Session = _session_factory(tmp_path)
    monkeypatch.setattr("services.accelerator_business.SessionLocal", Session)
    job_id, _artifact = _job(Session, tmp_path, status="fallback_completed")

    claimed = claim_cpu_fallback(AcceleratorEvidence(None, (job_id,)))

    assert claimed == AcceleratorEvidence(None)


def test_saturated_desktop_lane_routes_new_work_to_available_web_node(
    tmp_path, monkeypatch
):
    Session = _session_factory(tmp_path)
    monkeypatch.setattr("services.accelerator_business.SessionLocal", Session)
    from services import accelerator_business as business
    from services import accelerator_jobs as jobs

    db = Session()
    desktop, desktop_token = jobs.register_node(
        db,
        owner_user_id=1,
        name="desktop",
        lane="teacher_desktop",
        device_id="device-1",
    )
    desktop = jobs.authenticate_node(db, desktop.id, desktop_token)
    jobs.heartbeat(
        db,
        desktop,
        capabilities=_client_capabilities(lease_slots=6),
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=0,
    )
    web, web_token = jobs.register_node(
        db,
        owner_user_id=1,
        name="web",
        lane="web_gpu",
    )
    web = jobs.authenticate_node(db, web.id, web_token)
    jobs.heartbeat(
        db,
        web,
        capabilities=_client_capabilities(lease_slots=2),
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )

    assert business._select_lane(db, 1) == "web_gpu"
    db.close()


def test_expired_desktop_token_routes_new_work_to_available_web_node(
    tmp_path, monkeypatch
):
    Session = _session_factory(tmp_path)
    monkeypatch.setattr("services.accelerator_business.SessionLocal", Session)
    from services import accelerator_business as business
    from services import accelerator_jobs as jobs

    db = Session()
    desktop, desktop_token = jobs.register_node(
        db,
        owner_user_id=1,
        name="desktop",
        lane="teacher_desktop",
        device_id="device-expired",
        token_ttl_days=1,
    )
    desktop = jobs.authenticate_node(db, desktop.id, desktop_token)
    jobs.heartbeat(
        db,
        desktop,
        capabilities=_client_capabilities(lease_slots=6),
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=6,
    )
    desktop.token_expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    web, web_token = jobs.register_node(
        db,
        owner_user_id=1,
        name="web",
        lane="web_gpu",
    )
    web = jobs.authenticate_node(db, web.id, web_token)
    jobs.heartbeat(
        db,
        web,
        capabilities=_client_capabilities(lease_slots=2),
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )

    assert business._select_lane(db, 1) == "web_gpu"
    db.close()


def test_live_node_without_template_stage_is_not_selected(
    tmp_path, monkeypatch
):
    Session = _session_factory(tmp_path)
    monkeypatch.setattr("services.accelerator_business.SessionLocal", Session)
    from services import accelerator_business as business
    from services import accelerator_jobs as jobs

    db = Session()
    desktop, desktop_token = jobs.register_node(
        db,
        owner_user_id=1,
        name="desktop-other-stage",
        lane="teacher_desktop",
        device_id="other-stage",
    )
    desktop = jobs.authenticate_node(db, desktop.id, desktop_token)
    jobs.heartbeat(
        db,
        desktop,
        capabilities=_client_capabilities(
            stages=["document_render"],
            lease_slots=1,
        ),
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )
    web, web_token = jobs.register_node(
        db,
        owner_user_id=1,
        name="web-template-stage",
        lane="web_gpu",
    )
    web = jobs.authenticate_node(db, web.id, web_token)
    jobs.heartbeat(
        db,
        web,
        capabilities=_client_capabilities(
            stages=["template_registration"],
            lease_slots=1,
        ),
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )

    assert business._select_lane(db, 1) == "web_gpu"
    db.close()


def test_live_legacy_artifact_node_is_not_selected(
    tmp_path, monkeypatch
):
    Session = _session_factory(tmp_path)
    monkeypatch.setattr("services.accelerator_business.SessionLocal", Session)
    from services import accelerator_business as business
    from services import accelerator_jobs as jobs

    db = Session()
    node, token = jobs.register_node(db, owner_user_id=1, name="legacy-web")
    node = jobs.authenticate_node(db, node.id, token)
    jobs.heartbeat(
        db,
        node,
        capabilities={
            "stages": ["template_registration"],
            "artifact_contracts": ["aligned-page-v1"],
            "lease_slots": 1,
        },
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )

    assert business._select_lane(db, 1) is None
    assert business._select_queue_lane(db, 1) == ("web_gpu", False)
    db.close()


def _accelerator_inputs(tmp_path):
    source = tmp_path / "student-source.png"
    source.write_bytes(b"student-source")
    template_dir = tmp_path / "templates"
    template_dir.mkdir(exist_ok=True)
    template = template_dir / "page-1.png"
    template.write_bytes(b"template-source")
    from services.template_anchor_crops import build_anchor_crop_plan

    question_crop_plan = build_anchor_crop_plan(
        {
            "column_layout": {
                "column_count": 1,
                "columns": [
                    {"column_index": 1, "x1_1000": 0, "x2_1000": 1000}
                ],
            }
        },
        artifact_sha256=hashlib.sha256(b"template-source").hexdigest(),
        expected_question_numbers=["1"],
    )
    artifacts = {
        "page_1": {
            "template_image": template.name,
            "answer_regions": [{
                "question_number": "1",
                "question_type": "solve",
                "answer_bbox": [0, 0, 1000, 1000],
            }],
            "blank_detection_enabled": False,
            "question_crop_plan": question_crop_plan,
            "template_handwriting_regions": [{
                "question_number": "1",
                "regions": [[100, 100, 200, 200]],
            }],
        }
    }
    return source, template_dir, artifacts


def test_enqueue_template_pages_preserves_handwriting_exemptions(
    tmp_path,
    monkeypatch,
):
    from services import accelerator_business as business

    Session = _session_factory(tmp_path)
    monkeypatch.setattr(business, "SessionLocal", Session)
    source, template_dir, artifacts = _accelerator_inputs(tmp_path)

    enqueued = business._enqueue_template_pages(
        teacher_id=1,
        batch_id="batch-handwriting-exemptions",
        paper_id="paper-handwriting-exemptions",
        source_path=str(source),
        template_id="template-handwriting-exemptions",
        artifacts=artifacts,
        template_dir=str(template_dir),
    )

    db = Session()
    row = db.get(AcceleratorJob, enqueued.job_ids[0])
    layout = json.loads(row.layout_snapshot_json)
    db.close()
    assert layout["template_handwriting_regions"] == artifacts["page_1"][
        "template_handwriting_regions"
    ]


def _accept_multi_evidence(Session, tmp_path, job_id):
    from services.accelerator_artifacts import (
        CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION,
    )

    artifact_dir = tmp_path / f"accepted-{job_id}"
    artifact_dir.mkdir()
    image_path = artifact_dir / "evidence_01.png"
    image_bytes = b"trusted-client-evidence"
    image_path.write_bytes(image_bytes)
    index = {
        "contract_version": CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION,
        "evidence_images": [{
            "kind": "evidence_01",
            "byte_size": len(image_bytes),
            "sha256": hashlib.sha256(image_bytes).hexdigest(),
            "width": 1000,
            "height": 1000,
            "channels": 3,
            "format": "png",
            "sheet_id": "focus_01",
            "regions": [{
                "question_number": "1",
                "bbox_1000": [0, 0, 1000, 1000],
            }],
        }],
    }
    index_path = artifact_dir / "index.json"
    index_bytes = json.dumps(index).encode()
    index_path.write_bytes(index_bytes)

    db = Session()
    row = db.get(AcceleratorJob, job_id)
    row.status = "validated"
    row.server_validation_status = "accepted"
    row.server_validation_json = json.dumps({
        "validator": "trusted_client_evidence_multi_v3",
        "page_type_id": "page_1",
        "question_numbers": ["1"],
        "blank_question_numbers": [],
        "evidence_diagnostics": {
            "ownership": "confirmed_bbox_focus_sheets",
            "answer_source_coverage": [],
            "incomplete_answer_regions": [],
            "evidence_sheets": [{
                "sheet_id": "focus_01",
                "regions": [{
                    "question_number": "1",
                    "bbox_1000": [0, 0, 1000, 1000],
                }],
            }],
            "evidence_locations": [{
                "question_number": "1",
                "image_index": 1,
                "answer_bbox_1000": [0, 0, 1000, 1000],
            }],
        },
    })
    row.server_artifact_path = str(index_path)
    row.server_artifact_sha256 = hashlib.sha256(index_bytes).hexdigest()
    db.commit()
    db.close()
    return index_path, image_path


@pytest.mark.parametrize(
    ("error_code", "expected_status"),
    [
        ("job_deadline_exceeded", "queued"),
        ("lease_attempts_exhausted", "queued"),
        ("server_validation_deadline_exceeded", "queued"),
        ("server_validation_failed", "fallback_failed"),
    ],
)
def test_template_registration_enqueue_only_revives_retryable_terminal_job(
    tmp_path,
    monkeypatch,
    error_code,
    expected_status,
):
    from services import accelerator_business as business

    Session = _session_factory(tmp_path)
    monkeypatch.setattr(business, "SessionLocal", Session)
    source, template_dir, artifacts = _accelerator_inputs(tmp_path)
    first = business._enqueue_template_pages(
        teacher_id=1,
        batch_id="batch-registration-resume",
        paper_id="paper-registration-resume",
        source_path=str(source),
        template_id="template-registration-resume",
        artifacts=artifacts,
        template_dir=str(template_dir),
    )
    job_id = first.job_ids[0]
    db = Session()
    row = db.get(AcceleratorJob, job_id)
    row.status = "fallback_failed"
    row.attempt_count = 2
    row.error_code = error_code
    row.deadline_at = business.utcnow() - timedelta(seconds=1)
    db.commit()
    db.close()

    repeated = business._enqueue_template_pages(
        teacher_id=1,
        batch_id="batch-registration-resume",
        paper_id="paper-registration-resume",
        source_path=str(source),
        template_id="template-registration-resume",
        artifacts=artifacts,
        template_dir=str(template_dir),
    )

    assert repeated.job_ids == (job_id,)
    db = Session()
    resumed = db.get(AcceleratorJob, job_id)
    assert resumed.status == expected_status
    assert resumed.attempt_count == (0 if expected_status == "queued" else 2)
    assert resumed.error_code == (
        None if expected_status == "queued" else error_code
    )
    assert db.query(AcceleratorJob).count() == 1
    db.close()


def test_busy_live_desktop_timeout_allows_atomic_cpu_fallback(
    tmp_path, monkeypatch
):
    Session = _session_factory(tmp_path)
    monkeypatch.setattr("services.accelerator_business.SessionLocal", Session)
    from services import accelerator_business as business
    from services import accelerator_jobs as jobs

    monkeypatch.setattr(business, "WAIT_SECONDS", 0.0)

    db = Session()
    desktop, token = jobs.register_node(
        db,
        owner_user_id=1,
        name="offline-desktop",
        lane="teacher_desktop",
        device_id="offline-desktop",
    )
    desktop = jobs.authenticate_node(db, desktop.id, token)
    jobs.heartbeat(
        db,
        desktop,
        capabilities=_client_capabilities(
            stages=["student_multi_evidence"],
                artifact_contracts=["client-evidence-multi-v3"],
            lease_slots=1,
        ),
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=0,
    )
    db.close()
    source, template_dir, artifacts = _accelerator_inputs(tmp_path)

    handle = asyncio.run(business.try_accelerated_evidence(
        teacher_id="1",
        batch_id="batch-offline",
        paper_id="paper-offline",
        source_path=str(source),
        template_id="template-offline",
        artifacts=artifacts,
        template_dir=str(template_dir),
    ))

    assert handle.state == "unavailable"
    assert handle.reason_code == "accelerator_wait_timeout_no_active_worker"
    assert len(handle.job_ids) == 1
    assert claim_cpu_fallback(handle) is None
    fallback_request = AcceleratorEvidence(
        None,
        handle.job_ids,
        state="cpu_fallback",
        reason_code=handle.reason_code,
    )
    assert claim_cpu_fallback(fallback_request) == fallback_request
    verify = Session()
    job = verify.get(AcceleratorJob, handle.job_ids[0])
    assert job.status == "fallback_running"
    assert job.lane == "teacher_desktop"
    assert job.assigned_node_id is None
    verify.close()


def test_existing_validated_artifact_wins_when_node_has_no_free_slot(
    tmp_path, monkeypatch
):
    from services import accelerator_business as business
    from services import accelerator_jobs as jobs

    Session = _session_factory(tmp_path)
    monkeypatch.setattr(business, "SessionLocal", Session)
    monkeypatch.setattr(business, "WAIT_SECONDS", 0.0)
    db = Session()
    node, token = jobs.register_node(db, owner_user_id=1, name="busy-web")
    node = jobs.authenticate_node(db, node.id, token)
    jobs.heartbeat(
        db,
        node,
        capabilities=_client_capabilities(
            stages=["student_multi_evidence"],
                artifact_contracts=["client-evidence-multi-v3"],
            lease_slots=1,
        ),
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=0,
    )
    db.close()
    source, template_dir, artifacts = _accelerator_inputs(tmp_path)
    enqueued = business._enqueue_template_pages(
        teacher_id=1,
        batch_id="batch-existing-ready",
        paper_id="paper-existing-ready",
        source_path=str(source),
        template_id="template-existing-ready",
        artifacts=artifacts,
        template_dir=str(template_dir),
    )
    assert enqueued.node_available is True
    job_id = enqueued.job_ids[0]
    _index_path, image_path = _accept_multi_evidence(
        Session,
        tmp_path,
        job_id,
    )

    handle = asyncio.run(business.try_accelerated_evidence(
        teacher_id="1",
        batch_id="batch-existing-ready",
        paper_id="paper-existing-ready",
        source_path=str(source),
        template_id="template-existing-ready",
        artifacts=artifacts,
        template_dir=str(template_dir),
    ))

    assert handle.state == "ready"
    assert handle.used_job_id == job_id
    assert handle.evidence is not None
    assert handle.evidence.evidence_paths == (str(image_path.resolve()),)


def test_existing_leased_job_waits_when_node_has_no_free_slot(
    tmp_path, monkeypatch
):
    from services import accelerator_business as business
    from services import accelerator_jobs as jobs

    Session = _session_factory(tmp_path)
    monkeypatch.setattr(business, "SessionLocal", Session)
    monkeypatch.setattr(business, "WAIT_SECONDS", 0.02)
    monkeypatch.setattr(business, "POLL_SECONDS", 0.005)
    db = Session()
    node, token = jobs.register_node(db, owner_user_id=1, name="leased-busy-web")
    node = jobs.authenticate_node(db, node.id, token)
    jobs.heartbeat(
        db,
        node,
        capabilities=_client_capabilities(
            stages=["student_multi_evidence"],
                artifact_contracts=["client-evidence-multi-v3"],
            lease_slots=1,
        ),
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=0,
    )
    node_id = node.id
    db.close()
    source, template_dir, artifacts = _accelerator_inputs(tmp_path)
    enqueued = business._enqueue_template_pages(
        teacher_id=1,
        batch_id="batch-existing-leased",
        paper_id="paper-existing-leased",
        source_path=str(source),
        template_id="template-existing-leased",
        artifacts=artifacts,
        template_dir=str(template_dir),
    )
    assert enqueued.node_available is True
    job_id = enqueued.job_ids[0]
    db = Session()
    leased = db.get(AcceleratorJob, job_id)
    leased.status = "leased"
    leased.assigned_node_id = node_id
    leased.lease_id_hash = "b" * 64
    leased.lease_expires_at = business.utcnow() + timedelta(minutes=1)
    db.commit()
    db.close()

    handle = asyncio.run(business.try_accelerated_evidence(
        teacher_id="1",
        batch_id="batch-existing-leased",
        paper_id="paper-existing-leased",
        source_path=str(source),
        template_id="template-existing-leased",
        artifacts=artifacts,
        template_dir=str(template_dir),
    ))

    assert handle.state == "pending"
    assert handle.reason_code == "accelerator_wait_timeout"
    assert handle.job_ids == (job_id,)


@pytest.mark.parametrize("wait_seconds", [0.0, 0.05])
def test_online_but_unclaimed_wait_timeout_allows_atomic_cpu_fallback(
    tmp_path, monkeypatch, wait_seconds
):
    Session = _session_factory(tmp_path)
    monkeypatch.setattr("services.accelerator_business.SessionLocal", Session)
    from services import accelerator_business as business
    from services import accelerator_jobs as jobs

    monkeypatch.setattr(business, "WAIT_SECONDS", 0.0)
    monkeypatch.setattr(business, "POLL_SECONDS", 0.01)
    db = Session()
    node, token = jobs.register_node(db, owner_user_id=1, name="online-web")
    node = jobs.authenticate_node(db, node.id, token)
    jobs.heartbeat(
        db,
        node,
        capabilities=_client_capabilities(
            stages=["student_multi_evidence"],
                artifact_contracts=["client-evidence-multi-v3"],
            lease_slots=1,
        ),
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )
    db.close()
    source, template_dir, artifacts = _accelerator_inputs(tmp_path)

    handle = asyncio.run(business.try_accelerated_evidence(
        teacher_id="1",
        batch_id="batch-pending",
        paper_id="paper-pending",
        source_path=str(source),
        template_id="template-pending",
        artifacts=artifacts,
        template_dir=str(template_dir),
        wait_seconds=wait_seconds,
    ))

    assert handle.state == "unavailable"
    assert handle.reason_code == "accelerator_wait_timeout_no_active_worker"
    assert claim_cpu_fallback(handle) is None
    fallback_request = AcceleratorEvidence(
        None,
        handle.job_ids,
        state="cpu_fallback",
        reason_code=handle.reason_code,
    )
    assert claim_cpu_fallback(fallback_request) == fallback_request
    verify = Session()
    job = verify.get(AcceleratorJob, handle.job_ids[0])
    assert job.status == "fallback_running"
    current_node = verify.get(models_accelerator.AcceleratorNode, node.id)
    assert jobs.lease_next_job(verify, current_node) is None
    verify.close()


def test_validated_but_unloadable_artifact_is_terminal_fault(
    tmp_path, monkeypatch
):
    from services import accelerator_business as business

    Session = _session_factory(tmp_path)
    monkeypatch.setattr(business, "SessionLocal", Session)
    job_id, _artifact = _job(
        Session,
        tmp_path,
        status="validated",
        artifact=False,
    )
    monkeypatch.setattr(
        business,
        "_enqueue_template_pages",
        lambda **_kwargs: business._EnqueuedTemplatePages(
            (job_id,),
            True,
            "web_gpu",
        ),
    )
    monkeypatch.setattr(business, "WAIT_SECONDS", 5.0)

    async def must_not_sleep(*_args, **_kwargs):
        raise AssertionError("an unusable validated artifact must not stay pending")

    monkeypatch.setattr(business.asyncio, "sleep", must_not_sleep)

    handle = asyncio.run(business.try_accelerated_evidence(
        teacher_id="1",
        batch_id="batch-validated-corrupt",
        paper_id="paper-validated-corrupt",
        source_path="source.png",
        template_id="template-validated-corrupt",
        artifacts={"page_1": {}},
        template_dir="templates",
    ))

    assert handle.state == "failed"
    assert handle.reason_code == "accelerator_job_failed"
    assert handle.job_ids == (job_id,)


def test_unloadable_validated_candidate_waits_for_other_page_type(
    tmp_path, monkeypatch
):
    from services import accelerator_business as business

    Session = _session_factory(tmp_path)
    monkeypatch.setattr(business, "SessionLocal", Session)
    corrupt_id, _corrupt_artifact = _job(
        Session,
        tmp_path,
        status="validated",
        artifact=False,
        job_id="job-corrupt",
        page_type_id="page_1",
    )
    pending_id, pending_artifact = _job(
        Session,
        tmp_path,
        status="queued",
        artifact=True,
        job_id="job-pending",
        page_type_id="page_2",
    )

    evidence, used_job_id, terminal = business._poll_evidence_once(
        [corrupt_id, pending_id]
    )

    assert evidence is None
    assert used_job_id == ""
    assert terminal is False

    db = Session()
    pending = db.get(AcceleratorJob, pending_id)
    pending.status = "validated"
    pending.server_validation_status = "accepted"
    db.commit()
    db.close()

    evidence, used_job_id, terminal = business._poll_evidence_once(
        [corrupt_id, pending_id]
    )

    assert evidence is not None
    assert Path(evidence.evidence_path) == pending_artifact
    assert evidence.page_type_id == "page_2"
    assert used_job_id == pending_id
    assert terminal is False


def test_accelerator_wait_releases_default_executor_and_cancels_promptly(
    monkeypatch,
):
    from services import accelerator_business as business

    monkeypatch.setattr(business, "WAIT_SECONDS", 5.0)
    monkeypatch.setattr(business, "POLL_SECONDS", 1.0)
    monkeypatch.setattr(
        business,
        "_enqueue_template_pages",
        lambda **_kwargs: business._EnqueuedTemplatePages(
            ("job-1",),
            True,
            "web_gpu",
        ),
    )
    monkeypatch.setattr(
        business,
        "_inspect_evidence_once",
        lambda job_ids: (None, "", False, False),
    )
    polls = 0

    async def exercise_wait():
        nonlocal polls
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        first_poll_finished = asyncio.Event()

        def fake_poll(job_ids):
            nonlocal polls
            assert job_ids == ["job-1"]
            polls += 1
            loop.call_soon_threadsafe(first_poll_finished.set)
            return None, "", False

        monkeypatch.setattr(business, "_poll_evidence_once", fake_poll)
        waiting = asyncio.create_task(business.try_accelerated_evidence(
            teacher_id="1",
            batch_id="batch-1",
            paper_id="paper-1",
            source_path="source.png",
            template_id="template-1",
            artifacts={"page_1": {}},
            template_dir="templates",
        ))

        await asyncio.wait_for(first_poll_finished.wait(), timeout=1)
        marker = await asyncio.wait_for(
            asyncio.to_thread(lambda: "thread-available"),
            timeout=1,
        )
        assert marker == "thread-available"

        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(waiting, timeout=1)

    asyncio.run(exercise_wait())
    assert polls == 1


def _canonicalization_job(Session, tmp_path, *, operation_id="canonical-op"):
    from services import accelerator_business as business

    source = tmp_path / f"{operation_id}.jpg"
    source.write_bytes(b"immutable-canonical-source")
    job_id, _node_available = business._enqueue_template_canonicalization(
        teacher_id=1,
        operation_id=operation_id,
        source_path=str(source),
    )
    return job_id, source


def test_canonicalization_reuses_unfinished_job_for_same_owner_operation_and_sha(
    tmp_path, monkeypatch
):
    from services import accelerator_business as business

    Session = _session_factory(tmp_path)
    monkeypatch.setattr(business, "SessionLocal", Session)
    first_source = tmp_path / "first-source.jpg"
    second_source = tmp_path / "second-source.jpg"
    first_source.write_bytes(b"same-immutable-source")
    second_source.write_bytes(b"same-immutable-source")

    first_job_id, _available = business._enqueue_template_canonicalization(
        teacher_id=1,
        operation_id="deduplicated-op",
        source_path=str(first_source),
    )
    resumed_job_id, _available = business._enqueue_template_canonicalization(
        teacher_id=1,
        operation_id="deduplicated-op",
        source_path=str(second_source),
    )

    assert resumed_job_id == first_job_id
    db = Session()
    rows = db.query(AcceleratorJob).filter(
        AcceleratorJob.teacher_id == 1,
        AcceleratorJob.paper_id == "deduplicated-op",
        AcceleratorJob.stage == business.CANONICALIZATION_STAGE,
    ).all()
    assert [row.id for row in rows] == [first_job_id]
    assert rows[0].status == "queued"
    db.close()


def test_canonicalization_retry_revives_terminal_job_with_same_id(
    tmp_path, monkeypatch
):
    from services import accelerator_business as business

    Session = _session_factory(tmp_path)
    monkeypatch.setattr(business, "SessionLocal", Session)
    job_id, source = _canonicalization_job(
        Session,
        tmp_path,
        operation_id="canonical-terminal-retry",
    )
    db = Session()
    row = db.get(AcceleratorJob, job_id)
    row.status = "fallback_failed"
    row.attempt_count = 2
    row.error_code = "job_deadline_exceeded"
    row.deadline_at = business.utcnow() - timedelta(seconds=1)
    db.commit()
    db.close()

    resumed_id, _available = business._enqueue_template_canonicalization(
        teacher_id=1,
        operation_id="canonical-terminal-retry",
        source_path=str(source),
    )

    assert resumed_id == job_id
    db = Session()
    resumed = db.get(AcceleratorJob, job_id)
    assert resumed.status == "queued"
    assert resumed.attempt_count == 0
    assert resumed.deadline_at > business.utcnow()
    db.close()


def test_foreign_teacher_cannot_resume_canonicalization_job(tmp_path, monkeypatch):
    from services import accelerator_business as business

    Session = _session_factory(tmp_path)
    monkeypatch.setattr(business, "SessionLocal", Session)
    job_id, _source = _canonicalization_job(Session, tmp_path)

    def forbidden_poll(_job_id):
        raise AssertionError("foreign teacher must not poll another teacher's job")

    monkeypatch.setattr(business, "_poll_canonicalization_once", forbidden_poll)
    result = asyncio.run(
        business.resume_accelerated_template_canonicalization(
            teacher_id=2,
            job_id=job_id,
            wait_seconds=0,
        )
    )

    assert result.state == "unavailable"
    assert result.reason_code == "accelerator_job_unavailable"
    assert result.canonical_path is None
    assert result.canonicalization is None


@pytest.mark.parametrize(
    "error_code",
    [
        "job_deadline_exceeded",
        "lease_attempts_exhausted",
        "server_validation_deadline_exceeded",
    ],
)
def test_explicit_canonicalization_resume_revives_same_retryable_job_id(
    tmp_path,
    monkeypatch,
    error_code,
):
    from services import accelerator_business as business

    Session = _session_factory(tmp_path)
    monkeypatch.setattr(business, "SessionLocal", Session)
    job_id, _source = _canonicalization_job(
        Session,
        tmp_path,
        operation_id=f"canonical-explicit-{error_code}",
    )
    db = Session()
    row = db.get(AcceleratorJob, job_id)
    row.status = "fallback_failed"
    row.attempt_count = 2
    row.error_code = error_code
    row.deadline_at = business.utcnow() - timedelta(seconds=1)
    db.commit()
    db.close()
    monkeypatch.setattr(
        business,
        "_enqueue_template_canonicalization",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("resume must not enqueue replacement work")
        ),
    )

    result = asyncio.run(
        business.resume_accelerated_template_canonicalization(
            teacher_id=1,
            job_id=job_id,
            wait_seconds=0,
        )
    )

    assert result.state == "pending"
    assert result.job_id == job_id
    db = Session()
    resumed = db.get(AcceleratorJob, job_id)
    assert resumed.status == "queued"
    assert resumed.attempt_count == 0
    assert resumed.error_code is None
    assert db.query(AcceleratorJob).count() == 1
    db.close()


@pytest.mark.parametrize(
    ("stored_state", "expected_state", "expected_reason"),
    [
        ("queued", "pending", "accelerator_wait_timeout"),
        ("fallback_failed", "fault", "accelerator_job_failed"),
        ("validated", "ready", ""),
    ],
)
def test_resume_canonicalization_observes_original_job_without_enqueueing(
    tmp_path,
    monkeypatch,
    stored_state,
    expected_state,
    expected_reason,
):
    from services import accelerator_business as business

    Session = _session_factory(tmp_path)
    monkeypatch.setattr(business, "SessionLocal", Session)
    job_id, source = _canonicalization_job(
        Session,
        tmp_path,
        operation_id=f"resume-{stored_state}",
    )
    canonical_path = tmp_path / f"canonical-{stored_state}.png"
    db = Session()
    row = db.get(AcceleratorJob, job_id)
    row.status = stored_state
    if stored_state == "validated":
        canonical_path.write_bytes(b"client-canonical-template")
        canonical_sha = hashlib.sha256(canonical_path.read_bytes()).hexdigest()
        row.server_validation_status = "accepted"
        row.server_artifact_path = str(canonical_path)
        row.server_artifact_sha256 = canonical_sha
        row.candidate_json = json.dumps({
            "artifact_manifest": {
                "contract_version": business.CANONICAL_TEMPLATE_CONTRACT_VERSION,
            },
            "canonicalization": {
                "canonical_sha256": canonical_sha,
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            },
        })
    db.commit()
    db.close()

    def forbidden_enqueue(**_kwargs):
        raise AssertionError("resume must not enqueue a replacement job")

    monkeypatch.setattr(
        business,
        "_enqueue_template_canonicalization",
        forbidden_enqueue,
    )
    result = asyncio.run(
        business.resume_accelerated_template_canonicalization(
            teacher_id=1,
            job_id=job_id,
            wait_seconds=0,
        )
    )

    assert result.state == expected_state
    assert result.reason_code == expected_reason
    assert result.job_id == job_id
    if stored_state == "validated":
        assert Path(result.canonical_path) == canonical_path.resolve()
        assert result.canonicalization["canonical_sha256"] == canonical_sha
    else:
        assert result.canonical_path is None
        assert result.canonicalization is None


def test_paper_cache_uses_accelerator_artifact_without_running_cpu_matcher(
    tmp_path, monkeypatch
):
    evidence_path = tmp_path / "evidence-0123456789abcdef0123456789abcdef.png"
    evidence_path.write_bytes(b"evidence")
    evidence = EvidenceResult(
        page_type_id="page_1",
        evidence_path=str(evidence_path),
        question_numbers=["1"],
        blank_question_numbers=[],
        diagnostics={
            "ownership": "confirmed_bbox_neighbor_clamped",
            "answer_source_complete": True,
            "accelerator": {
                "evidence_authority": "trusted_client_evidence",
                "server_source_transform_replayed": False,
            },
        },
        aligned_path=str(tmp_path / "must-not-be-read.png"),
    )
    handle = AcceleratorEvidence(
        evidence,
        ("job-1",),
        "job-1",
        state="ready",
    )
    calls = {"cpu": 0, "crop": 0, "finish": []}

    async def fake_try(**kwargs):
        assert kwargs["batch_id"] == "batch-1"
        assert kwargs["paper_id"] == "paper-1"
        return handle

    def fake_finish(value, *, fallback_succeeded, error_code=""):
        calls["finish"].append((value, fallback_succeeded, error_code))
        return 1

    monkeypatch.setattr(
        "services.accelerator_business.try_accelerated_evidence",
        fake_try,
    )
    monkeypatch.setattr(
        "services.accelerator_business.finish_business_preprocessing",
        fake_finish,
    )

    class MustNotRunMatcher:
        artifacts = {
            "page_1": {
                "question_crop_plan": [{"question_number": "1"}],
                "sha256": "0" * 64,
            },
        }

        def match_and_build(self, *_args, **_kwargs):
            calls["cpu"] += 1
            raise AssertionError("CPU fallback matcher must not run")

    def must_not_rebuild_crop(*_args, **_kwargs):
        calls["crop"] += 1
        raise AssertionError("client evidence must not be rebuilt on server CPU")

    monkeypatch.setattr(
        "services.template_evidence_matcher._load_rgb_image",
        must_not_rebuild_crop,
    )
    monkeypatch.setattr(
        "services.template_evidence_artifacts._load_rgb_image",
        must_not_rebuild_crop,
    )

    cache = PaperCache.from_confirmed_template_pages([{
        "page_index": 0,
        "questions": [{
            "question_no": "1",
            "question_text": "题干",
            "question_type": "choice",
        }],
    }])
    cache._template_evidence_matcher = MustNotRunMatcher()
    cache._require_local_evidence = True
    cache._accelerator_template_artifacts = {"page_1": {"verified": True}}
    cache._accelerator_template_dir = str(tmp_path)
    cache._accelerator_template_id = "template-1"

    async def fake_answers(path, label, teacher_id="", **kwargs):
        assert Path(path) == evidence_path
        assert kwargs["evidence_mode"] is True
        assert label == "page_1"
        return {
            "questions": [{"question_number": "1", "student_answer": "A"}],
            "_cache_type_label": label,
        }

    cache._recognize_answers_only = fake_answers
    result = asyncio.run(cache._recognize_with_local_evidence(
        str(tmp_path / "student.png"),
        teacher_id="1",
        batch_id="batch-1",
        paper_id="paper-1",
    ))

    assert result["_local_template_evidence"] is True
    assert result["_ocr_source"] == "client-evidence-v1"
    assert calls["cpu"] == 0
    assert calls["crop"] == 0
    assert len(calls["finish"]) == 1
    assert calls["finish"][0][0].used_job_id == "job-1"
    assert not evidence_path.exists()


def test_paper_cache_rejects_ready_server_replay_evidence(
    tmp_path, monkeypatch
):
    evidence_path = tmp_path / "evidence-fedcba9876543210fedcba9876543210.png"
    evidence_path.write_bytes(b"evidence")
    evidence = EvidenceResult(
        page_type_id="page_1",
        evidence_path=str(evidence_path),
        question_numbers=["1"],
        blank_question_numbers=[],
        diagnostics={
            "accelerator": {
                "evidence_authority": "server_cpu_replay",
                "server_source_transform_replayed": True,
            },
        },
    )
    calls = {"ocr": 0, "cpu": 0}

    async def fake_try(**_kwargs):
        return AcceleratorEvidence(evidence, state="ready")

    async def unexpected_ocr(*_args, **_kwargs):
        calls["ocr"] += 1
        raise AssertionError("server-replayed evidence must not reach Vision OCR")

    def unexpected_cpu(*_args, **_kwargs):
        calls["cpu"] += 1
        raise AssertionError("invalid evidence must not run server matcher")

    monkeypatch.setattr(
        "services.accelerator_business.try_accelerated_evidence",
        fake_try,
    )
    cache = PaperCache()
    cache._template_evidence_matcher = type("Matcher", (), {
        "match_and_build": staticmethod(unexpected_cpu),
    })()
    cache._accelerator_template_artifacts = {"page_1": {"verified": True}}
    cache._accelerator_template_dir = str(tmp_path)
    cache._accelerator_template_id = "template-1"
    cache._recognize_answers_only = unexpected_ocr

    result = asyncio.run(cache._recognize_with_local_evidence(
        str(tmp_path / "paper.png"),
        teacher_id="1",
        batch_id="batch-1",
        paper_id="paper-1",
    ))

    assert result == {
        "error": "客户端加速图片处理发生故障，已暂停自动批改该页",
        "_accelerator_state": "fault",
        "_accelerator_reason_code": "accelerator_evidence_contract_invalid",
    }
    assert calls == {"ocr": 0, "cpu": 0}
    assert not evidence_path.exists()


def _local_fallback_cache(tmp_path, matcher):
    cache = PaperCache.from_confirmed_template_pages([{
        "page_index": 0,
        "questions": [{
            "question_no": "1",
            "question_text": "题干",
            "question_type": "solve",
        }],
    }])
    cache._template_evidence_matcher = matcher
    cache._require_local_evidence = True
    cache._accelerator_template_artifacts = {"page_1": {"verified": True}}
    cache._accelerator_template_dir = str(tmp_path)
    cache._accelerator_template_id = "template-1"
    return cache


def test_paper_cache_pending_accelerator_does_not_run_cpu_fallback(
    tmp_path, monkeypatch
):
    calls = {"cpu": 0, "claim": 0, "finish": 0}

    async def fake_try(**_kwargs):
        return AcceleratorEvidence(
            None,
            ("job-1",),
            state="pending",
            reason_code="accelerator_wait_timeout",
        )

    def unexpected_claim(_value):
        calls["claim"] += 1
        raise AssertionError("pending work must remain remote")

    def unexpected_cpu(*_args, **_kwargs):
        calls["cpu"] += 1
        raise AssertionError("pending work must not run the local matcher")

    def unexpected_finish(*_args, **_kwargs):
        calls["finish"] += 1
        raise AssertionError("pending work must remain open")

    monkeypatch.setattr("services.accelerator_business.try_accelerated_evidence", fake_try)
    monkeypatch.setattr("services.accelerator_business.claim_cpu_fallback", unexpected_claim)
    monkeypatch.setattr(
        "services.accelerator_business.finish_business_preprocessing",
        unexpected_finish,
    )
    cache = _local_fallback_cache(
        tmp_path,
        type("Matcher", (), {"match_and_build": staticmethod(unexpected_cpu)})(),
    )

    result = asyncio.run(cache._recognize_with_local_evidence(
        str(tmp_path / "student.png"),
        teacher_id="1",
        batch_id="batch-1",
        paper_id="paper-1",
    ))

    assert result == {
        "error": "等待教师客户端加速节点完成图片处理",
        "_accelerator_state": "pending",
        "_accelerator_reason_code": "accelerator_wait_timeout",
    }
    assert calls == {"cpu": 0, "claim": 0, "finish": 0}


def test_paper_cache_unavailable_claims_one_server_cpu_fallback(
    tmp_path, monkeypatch
):
    calls = {"cpu": 0, "claim": [], "finish": []}
    matcher_paths = []

    async def fake_try(**_kwargs):
        return AcceleratorEvidence(
            None,
            ("job-1",),
            state="unavailable",
            reason_code="accelerator_node_unavailable",
        )

    def fake_claim(value):
        calls["claim"].append(value)
        assert value.state == "cpu_fallback"
        assert value.job_ids == ("job-1",)
        return AcceleratorEvidence(
            None,
            ("job-1",),
            state="cpu_fallback",
            reason_code=value.reason_code,
        )

    def fake_finish(value, *, fallback_succeeded, error_code=""):
        calls["finish"].append((value, fallback_succeeded, error_code))
        return 1

    class Matcher:
        def match_and_build(self, _image_path, output_dir, **kwargs):
            calls["cpu"] += 1
            assert kwargs["include_evidence"] is True
            evidence_path = Path(output_dir) / "evidence.png"
            evidence_path.write_bytes(b"server-rendered-evidence")
            matcher_paths.append(evidence_path)
            return EvidenceResult(
                page_type_id="page_1",
                evidence_path=str(evidence_path),
                evidence_paths=(str(evidence_path),),
                question_numbers=["1"],
                blank_question_numbers=[],
                diagnostics={
                    "ownership": "full_column_difference",
                    "answer_source_complete": True,
                },
            )

    async def fake_answers(path, label, teacher_id="", **kwargs):
        assert Path(path) == matcher_paths[0]
        assert label == "page_1"
        assert kwargs["evidence_mode"] is True
        assert kwargs["evidence_strategy"] == "full_column_difference"
        return {
            "questions": [{"question_number": "1", "student_answer": "x"}],
            "_cache_type_label": label,
        }

    monkeypatch.setattr("services.accelerator_business.try_accelerated_evidence", fake_try)
    monkeypatch.setattr("services.accelerator_business.claim_cpu_fallback", fake_claim)
    monkeypatch.setattr(
        "services.accelerator_business.finish_business_preprocessing",
        fake_finish,
    )
    cache = _local_fallback_cache(tmp_path, Matcher())
    cache._recognize_answers_only = fake_answers

    result = asyncio.run(cache._recognize_with_local_evidence(
        str(tmp_path / "student.png"),
        teacher_id="1",
        batch_id="batch-1",
        paper_id="paper-1",
    ))

    assert result["questions"][0]["student_answer"] == "x"
    assert result["_local_template_evidence"] is False
    assert result["_server_template_evidence"] is True
    assert result["_ocr_source"] == "client-evidence-columns-v1"
    diagnostics = result["_template_match_diagnostics"]
    assert diagnostics["accelerator"] == {
        "server_validated": True,
        "candidate_reused_for_acceptance": False,
        "evidence_authority": "server_rendered_full_columns",
        "server_source_transform_replayed": True,
        "server_evidence_rendered": True,
        "executor": "business_cpu_template_evidence",
    }
    assert calls["cpu"] == 1
    assert len(calls["claim"]) == 1
    assert len(calls["finish"]) == 1
    assert calls["finish"][0][1:] == (True, "")
    assert not matcher_paths[0].exists()


def test_paper_cache_failed_accelerator_claim_loss_is_fail_closed(
    tmp_path, monkeypatch
):
    calls = {"cpu": 0, "claim": 0}

    async def fake_try(**_kwargs):
        return AcceleratorEvidence(
            None,
            ("job-1",),
            state="failed",
            reason_code="accelerator_job_failed",
        )

    def claim_lost(_value):
        calls["claim"] += 1
        return None

    def unexpected_cpu(*_args, **_kwargs):
        calls["cpu"] += 1
        raise AssertionError("a lost fallback claim must not duplicate matching")

    monkeypatch.setattr("services.accelerator_business.try_accelerated_evidence", fake_try)
    monkeypatch.setattr("services.accelerator_business.claim_cpu_fallback", claim_lost)
    cache = _local_fallback_cache(
        tmp_path,
        type("Matcher", (), {"match_and_build": staticmethod(unexpected_cpu)})(),
    )

    result = asyncio.run(cache._recognize_with_local_evidence(
        str(tmp_path / "student.png"),
        teacher_id="1",
        batch_id="batch-1",
        paper_id="paper-1",
    ))

    assert result == {
        "error": "加速任务正在由其他处理者接管，已停止重复配准该页",
        "_accelerator_state": "fault",
        "_accelerator_reason_code": "accelerator_fallback_claim_lost",
    }
    assert calls == {"cpu": 0, "claim": 1}


def test_paper_cache_unavailable_without_remote_job_uses_local_matcher(
    tmp_path, monkeypatch
):
    calls = {"cpu": 0, "claim": 0}
    matcher_paths = []

    async def fake_try(**_kwargs):
        return AcceleratorEvidence(
            None,
            (),
            state="unavailable",
            reason_code="accelerator_enqueue_failed",
        )

    def unexpected_claim(_value):
        calls["claim"] += 1
        raise AssertionError("there is no remote work to claim")

    class Matcher:
        def match_and_build(self, _image_path, output_dir, **_kwargs):
            calls["cpu"] += 1
            evidence_path = Path(output_dir) / "evidence.png"
            evidence_path.write_bytes(b"evidence")
            matcher_paths.append(evidence_path)
            return EvidenceResult(
                page_type_id="page_1",
                evidence_path=str(evidence_path),
                question_numbers=["1"],
                blank_question_numbers=[],
                diagnostics={
                    "ownership": "full_column_difference",
                    "answer_source_complete": True,
                },
            )

    async def fake_answers(*_args, **_kwargs):
        return {"questions": [{"question_number": "1", "student_answer": "ok"}]}

    monkeypatch.setattr("services.accelerator_business.try_accelerated_evidence", fake_try)
    monkeypatch.setattr("services.accelerator_business.claim_cpu_fallback", unexpected_claim)
    cache = _local_fallback_cache(tmp_path, Matcher())
    cache._recognize_answers_only = fake_answers

    result = asyncio.run(cache._recognize_with_local_evidence(
        str(tmp_path / "student.png"),
        teacher_id="1",
        batch_id="batch-1",
        paper_id="paper-1",
    ))

    assert result["questions"][0]["student_answer"] == "ok"
    assert result["_server_template_evidence"] is True
    assert calls == {"cpu": 1, "claim": 0}
    assert not matcher_paths[0].exists()


def test_paper_cache_ready_without_evidence_fails_without_server_cpu(
    tmp_path, monkeypatch
):
    handle = AcceleratorEvidence(
        None,
        ("job-1",),
        "job-1",
        state="ready",
    )
    calls = {"cpu": 0, "claim": 0, "finish": 0, "ocr": 0}

    async def fake_try(**_kwargs):
        return handle

    def unexpected_claim(_value):
        calls["claim"] += 1
        raise AssertionError("ready handle must never enter CPU fallback")

    def unexpected_cpu(*_args, **_kwargs):
        calls["cpu"] += 1
        raise AssertionError("missing client evidence must not run server matcher")

    def fake_finish(*_args, **_kwargs):
        calls["finish"] += 1

    async def unexpected_ocr(*_args, **_kwargs):
        calls["ocr"] += 1
        raise AssertionError("empty evidence must not reach Vision OCR")

    monkeypatch.setattr(
        "services.accelerator_business.try_accelerated_evidence", fake_try
    )
    monkeypatch.setattr(
        "services.accelerator_business.claim_cpu_fallback", unexpected_claim
    )
    monkeypatch.setattr(
        "services.accelerator_business.finish_business_preprocessing",
        fake_finish,
    )

    cache = PaperCache()
    cache._template_evidence_matcher = type("Matcher", (), {
        "match_and_build": staticmethod(unexpected_cpu),
        "artifacts": {},
    })()
    cache._accelerator_template_artifacts = {"page_1": {"template_image": "t.png"}}
    cache._accelerator_template_dir = str(tmp_path)
    cache._accelerator_template_id = "template-1"
    cache._recognize_answers_only = unexpected_ocr

    result = asyncio.run(cache._recognize_with_local_evidence(
        str(tmp_path / "paper.png"),
        teacher_id="1",
        batch_id="batch-1",
        paper_id="paper-1",
    ))

    assert result == {
        "error": "客户端加速图片处理发生故障，已暂停自动批改该页",
        "_accelerator_state": "fault",
        "_accelerator_reason_code": "accelerator_evidence_missing",
    }
    assert calls == {"cpu": 0, "claim": 0, "finish": 1, "ocr": 0}
