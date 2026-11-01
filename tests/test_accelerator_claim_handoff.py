"""SQLite CPU-claim refusal must preserve a live owner's pending handoff."""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models_accelerator import AcceleratorJob, AcceleratorNode
from models_user import User
from services import accelerator_business as business
from services import accelerator_jobs as jobs
from services.paper_cache import PaperCache


@pytest.fixture
def handoff(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'handoff.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(business, "SessionLocal", Session)
    source = tmp_path / "source.png"
    source.write_bytes(b"synthetic-source")
    with Session() as db:
        db.add(User(id=1, name="Synthetic teacher", role="teacher", status="active"))
        db.commit()
        node, token = jobs.register_node(
            db, owner_user_id=1, name="handoff-test", lane="web_gpu",
        )
        node = jobs.authenticate_node(db, node.id, token)
        jobs.heartbeat(
            db, node,
            capabilities={
                "stages": ["template_registration", jobs.STUDENT_MULTI_EVIDENCE_STAGE],
                "artifact_contracts": [
                    "client-evidence-v1", business.CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION,
                    business.CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION,
                ],
            },
            algorithm_version=jobs.ALGORITHM_VERSION, free_slots=1,
        )
        node_id = node.id
    artifacts = []

    def add_job(job_id, *, status="queued", stage="template_registration"):
        artifact = tmp_path / f"artifact-{job_id}.png"
        artifact.write_bytes(b"synthetic-artifact")
        artifacts.append(artifact)
        with Session() as db:
            db.add(AcceleratorJob(
                id=job_id, teacher_id=1, batch_id="batch-1", paper_id="paper-1",
                source_path=str(source),
                source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                template_id=f"template-1:{job_id}", template_path=str(source),
                template_sha256="b" * 64,
                layout_snapshot_json=json.dumps({"page_type_id": job_id}),
                layout_snapshot_sha256=hashlib.sha256(job_id.encode()).hexdigest(),
                stage=stage, lane="web_gpu", algorithm_version=jobs.ALGORITHM_VERSION,
                status=status, deadline_at=jobs.utcnow() + timedelta(minutes=10),
                server_validation_status="not_started",
                server_artifact_path=str(artifact),
                server_artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
            ))
            db.commit()

    def remote_owner(status):
        with Session() as db:
            node = db.get(AcceleratorNode, node_id)
            leased = jobs.lease_next_job(db, node)
            assert leased is not None
            row, _secret = leased
            assert row.id == "page_1"
            row.status = status
            if status != "leased":
                row.candidate_received_at = jobs.utcnow()
                row.server_validation_status = "running" if status == "server_validating" else "pending"
            db.commit()

    def snapshot():
        with Session() as db:
            return {
                row.id: {column.name: getattr(row, column.name)
                         for column in AcceleratorJob.__table__.columns}
                for row in db.query(AcceleratorJob).all()
            }

    yield SimpleNamespace(
        Session=Session, add_job=add_job, remote_owner=remote_owner,
        snapshot=snapshot, artifacts=artifacts, source=source, root=tmp_path,
    )
    engine.dispose()


def run_cache(handoff, monkeypatch, job_ids, observed_state, before_claim=None):
    calls = {"claim": 0, "cpu": 0, "ocr": 0, "finish": 0}
    real_claim = business.claim_cpu_fallback

    async def stale_observation(**_kwargs):
        return business.AcceleratorEvidence(
            None, job_ids, state=observed_state, reason_code="stale_observation",
        )

    def claim(request):
        calls["claim"] += 1
        if before_claim:
            before_claim()
        result = real_claim(request)
        assert result is None, "the real SQLite claim must refuse ownership"
        return result

    def forbidden(kind):
        def call(*_args, **_kwargs):
            calls[kind] += 1
            raise AssertionError(f"claim loser must not run {kind}")
        return call

    cache = PaperCache.from_confirmed_template_pages([{
        "page_index": index,
        "questions": [{"question_no": str(index + 1), "question_text": "Synthetic",
                       "question_type": "solve"}],
    } for index in range(len(job_ids))])
    cache._template_evidence_matcher = SimpleNamespace(match_and_build=forbidden("cpu"))
    cache._require_local_evidence = True
    cache._accelerator_template_artifacts = {key: {"verified": True} for key in job_ids}
    cache._accelerator_template_dir = str(handoff.root)
    cache._accelerator_template_id = "template-1"
    cache._recognize_answers_only = forbidden("ocr")
    monkeypatch.setattr(business, "try_accelerated_evidence", stale_observation)
    monkeypatch.setattr(business, "claim_cpu_fallback", claim)
    monkeypatch.setattr(business, "finish_business_preprocessing", forbidden("finish"))
    result = asyncio.run(cache._recognize_with_local_evidence(
        str(handoff.source), teacher_id="1", batch_id="batch-1", paper_id="paper-1",
    ))
    assert calls == {"claim": 1, "cpu": 0, "ocr": 0, "finish": 0}
    for artifact in handoff.artifacts:
        assert artifact.read_bytes() == b"synthetic-artifact"
    return result


@pytest.mark.parametrize("observed_state", ["unavailable", "failed"])
@pytest.mark.parametrize("sibling", [None, "queued", "fallback_failed"])
@pytest.mark.parametrize("owner", ["leased", "candidate_received", "server_validating", "fallback_running"])
@pytest.mark.parametrize("stage", ["template_registration", jobs.STUDENT_MULTI_EVIDENCE_STAGE])
def test_live_owner_is_pending_after_real_claim_refusal(
    handoff, monkeypatch, observed_state, sibling, owner, stage,
):
    handoff.add_job("page_1", stage=stage)
    if owner == "fallback_running":
        # A different worker really claims CPU, rather than faking its result.
        request = business.AcceleratorEvidence(None, ("page_1",))
        assert business.claim_cpu_fallback(request) == request
        # CPU owns any newly published in-flight sidecar; a waiter must not delete it.
        handoff.artifacts[0].write_bytes(b"synthetic-artifact")
        with handoff.Session() as db:
            row = db.get(AcceleratorJob, "page_1")
            row.server_artifact_path = str(handoff.artifacts[0])
            row.server_artifact_sha256 = hashlib.sha256(b"synthetic-artifact").hexdigest()
            db.commit()
    else:
        handoff.remote_owner(owner)
    ids = ("page_1",)
    if sibling:
        handoff.add_job("page_2", status=sibling, stage=stage)
        ids += ("page_2",)
    before = handoff.snapshot()
    result = run_cache(handoff, monkeypatch, ids, observed_state)
    assert handoff.snapshot() == before
    assert result["_accelerator_state"] == "pending"
    assert result["_accelerator_reason_code"] == "accelerator_fallback_claim_lost"
    assert not result.get("questions")


@pytest.mark.parametrize("owner", ["leased", "candidate_received", "server_validating"])
@pytest.mark.parametrize("observed_state", ["unavailable", "failed"])
def test_remote_wins_between_select_and_cas_stays_pending(
    handoff, monkeypatch, owner, observed_state,
):
    handoff.add_job("page_1")
    handoff.add_job("page_2")
    real_update = business.update
    snapshot = {}

    def race_update(model):
        if not snapshot:
            handoff.remote_owner(owner)
            snapshot.update(handoff.snapshot())
        return real_update(model)

    monkeypatch.setattr(business, "update", race_update)
    result = run_cache(handoff, monkeypatch, ("page_1", "page_2"), observed_state)
    assert snapshot and handoff.snapshot() == snapshot
    assert result["_accelerator_state"] == "pending"


@pytest.mark.parametrize("observed_state", ["unavailable", "failed"])
@pytest.mark.parametrize("damage", [
    "missing", "unknown", "validated_without_receipt", "bad_layout",
    "mixed_identity", "wrong_stage", "missing_lease", "wrong_paper",
])
def test_unproven_or_corrupt_group_remains_fail_closed(
    handoff, monkeypatch, observed_state, damage,
):
    handoff.add_job("page_1")
    handoff.remote_owner("server_validating")
    handoff.add_job("page_2")
    with handoff.Session() as db:
        row = db.get(AcceleratorJob, "page_2")
        if damage == "missing":
            db.delete(row)
        elif damage == "unknown":
            row.status = "unrecognized_status"
        elif damage == "validated_without_receipt":
            row.status = "validated"
            row.server_validation_status = "accepted"
            row.server_validation_json = "{}"
        elif damage == "bad_layout":
            row.layout_snapshot_json = "{broken"
        elif damage == "mixed_identity":
            row.source_sha256 = "c" * 64
        elif damage == "wrong_stage":
            row.stage = "template_canonicalization"
        elif damage == "missing_lease":
            db.get(AcceleratorJob, "page_1").lease_id_hash = None
        elif damage == "wrong_paper":
            for item in db.query(AcceleratorJob).all():
                item.paper_id = "different-paper"
        db.commit()
    before = handoff.snapshot()
    result = run_cache(handoff, monkeypatch, ("page_1", "page_2"), observed_state)
    assert handoff.snapshot() == before
    assert result["_accelerator_state"] == (
        "unavailable" if observed_state == "unavailable" else "fault"
    )
    assert result["_accelerator_reason_code"] == "accelerator_fallback_claim_lost"
