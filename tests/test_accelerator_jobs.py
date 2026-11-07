"""Protocol tests for the opportunistic remote preprocessing worker."""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models_accelerator  # noqa: F401
import models_user  # noqa: F401
from database import Base
from models_accelerator import AcceleratorJob
from models_user import User
from services import accelerator_jobs as jobs
from services import accelerator_jobs_base
from services import accelerator_jobs_queue


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'accelerator-test.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    db.add(User(id=1, name="管理员", role="admin", status="active"))
    db.commit()
    db.close()
    return Session


@pytest.fixture
def db(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def _files(tmp_path):
    source = tmp_path / "source.jpg"
    template = tmp_path / "template.jpg"
    source.write_bytes(b"source-bytes")
    template.write_bytes(b"template-bytes")
    return source, template


def _layout(question="1"):
    return {
        "page_type_id": "page_1",
        "answer_regions": [{
            "question_number": question,
            "question_type": "solve",
            "answer_bbox": [0, 0, 1000, 1000],
        }],
    }


def _enqueue(db, tmp_path, **kwargs):
    source, template = _files(tmp_path)
    return jobs.enqueue_job(
        db,
        teacher_id=1,
        paper_id=kwargs.pop("paper_id", "paper-1"),
        source_path=str(source),
        template_id=kwargs.pop("template_id", "template-1"),
        template_path=str(template),
        layout_snapshot=kwargs.pop("layout_snapshot", _layout()),
        **kwargs,
    )


def _aligned_result():
    return {
        "status": "succeeded",
        "page_type_id": "page_1",
        "homography": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        "good_matches": 100,
        "inliers": 80,
        "inlier_ratio": 0.8,
        "reprojection_rmse": 1.2,
        "coverage": 0.95,
        "answer_source_complete": True,
        "incomplete_answer_regions": [],
        "artifact_sha256": hashlib.sha256(b"artifact").hexdigest(),
        "artifact_manifest": {
            "contract_version": "aligned-page-v1",
            "coordinate_space": "raw_source_pixel_to_template_pixel",
            "transform_graph": "global_homography_only_v1",
            "renderer": "opencv_warp_v1",
            "source_width": 800,
            "source_height": 1000,
            "template_width": 800,
            "template_height": 1000,
            "aligned_page": {
                "sha256": "c" * 64,
                "byte_size": 100,
                "width": 800,
                "height": 1000,
                "channels": 3,
                "format": "png",
            },
            "source_mask": {
                "sha256": "d" * 64,
                "byte_size": 100,
                "width": 800,
                "height": 1000,
                "channels": 1,
                "format": "png",
            },
        },
    }


def _client_evidence_result():
    from services.accelerator_artifacts import (
        CLIENT_EVIDENCE_CONTRACT_VERSION,
        CLIENT_EVIDENCE_COORDINATE_SPACE,
        CLIENT_EVIDENCE_RENDERER,
        TRANSFORM_GRAPH,
    )

    result = _aligned_result()
    result["artifact_manifest"] = {
        "contract_version": CLIENT_EVIDENCE_CONTRACT_VERSION,
        "coordinate_space": CLIENT_EVIDENCE_COORDINATE_SPACE,
        "transform_graph": TRANSFORM_GRAPH,
        "renderer": CLIENT_EVIDENCE_RENDERER,
        "source_width": 800,
        "source_height": 1000,
        "template_width": 800,
        "template_height": 1000,
        "evidence": {
            "sha256": "e" * 64,
            "byte_size": 100,
            "width": 800,
            "height": 1000,
            "channels": 3,
            "format": "png",
        },
    }
    result["answer_source_complete"] = True
    result["incomplete_answer_regions"] = []
    result["question_numbers"] = ["1"]
    result["blank_question_numbers"] = []
    result["evidence_diagnostics"] = {
        "ownership": "confirmed_bbox_neighbor_clamped",
        "answer_source_coverage": [{
            "question_number": "1",
            "covered_pixels": 800_000,
            "total_pixels": 800_000,
            "coverage": 1.0,
            "complete": True,
            "crop_box": [0, 0, 800, 1000],
        }],
        "incomplete_answer_regions": [],
    }
    return result


def _result():
    return _client_evidence_result()


def _client_capabilities(**values):
    return {
        "artifact_contracts": ["client-evidence-v1"],
        **values,
    }


def _ready_node(db):
    node, token = jobs.register_node(db, owner_user_id=1, name="test node")
    authenticated = jobs.authenticate_node(db, node.id, token)
    assert authenticated is not None
    jobs.heartbeat(
        db,
        authenticated,
        capabilities=_client_capabilities(gpu="RTX 4060", batch_size=8),
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
        queue_depth=0,
        estimated_finish_ms=500,
    )
    return authenticated, token


def test_client_evidence_result_contract_is_normalized():
    payload = jobs._validate_candidate_payload(_client_evidence_result())

    assert payload["question_numbers"] == ["1"]
    assert payload["blank_question_numbers"] == []
    assert payload["answer_source_complete"] is True
    assert payload["evidence_diagnostics"]["answer_source_coverage"][0] == {
        "question_number": "1",
        "covered_pixels": 800_000,
        "total_pixels": 800_000,
        "coverage": 1.0,
        "complete": True,
        "crop_box": [0, 0, 800, 1000],
    }


def test_aligned_page_result_cannot_smuggle_client_evidence_fields():
    result = _aligned_result()
    result.update({
        "question_numbers": ["1"],
        "blank_question_numbers": [],
        "evidence_diagnostics": {},
    })

    with pytest.raises(ValueError, match="requires client-evidence-v1"):
        jobs._validate_candidate_payload(result)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda value: value.pop("blank_question_numbers"),
            "fields are incomplete",
        ),
        (
            lambda value: value.__setitem__("question_numbers", ["x" * 65]),
            "question_numbers is invalid",
        ),
        (
            lambda value: value.__setitem__(
                "question_numbers",
                [str(index) for index in range(201)],
            ),
            "question_numbers is invalid",
        ),
        (
            lambda value: value.__setitem__("blank_question_numbers", ["2"]),
            "blank_question_numbers is invalid",
        ),
        (
            lambda value: value["evidence_diagnostics"].__setitem__(
                "unexpected", True
            ),
            "evidence_diagnostics is invalid",
        ),
        (
            lambda value: value["evidence_diagnostics"][
                "answer_source_coverage"
            ][0].__setitem__("coverage", 0.5),
            "evidence_diagnostics is invalid",
        ),
    ],
)
def test_client_evidence_result_rejects_malformed_claims(mutate, message):
    result = _client_evidence_result()
    mutate(result)

    with pytest.raises(ValueError, match=message):
        jobs._validate_candidate_payload(result)


def test_client_evidence_diagnostics_have_a_hard_size_limit():
    result = _client_evidence_result()
    questions = [str(index) for index in range(200)]
    result["question_numbers"] = questions
    result["evidence_diagnostics"]["answer_source_coverage"] = [
        {
            "question_number": question,
            "covered_pixels": 800_000,
            "total_pixels": 800_000,
            "coverage": 1.0,
            "complete": True,
            "crop_box": [0, 0, 800, 1000],
        }
        for question in questions
    ]

    with pytest.raises(ValueError, match="evidence_diagnostics is too large"):
        jobs._validate_candidate_payload(result)


def test_explicit_stage_capability_controls_job_leases(db, tmp_path):
    node, token = jobs.register_node(db, owner_user_id=1, name="stage-aware")
    node = jobs.authenticate_node(db, node.id, token)
    jobs.heartbeat(
        db,
        node,
        capabilities=_client_capabilities(
            stages=["document_render"],
            lease_slots=1,
        ),
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )
    job = _enqueue(db, tmp_path)

    assert jobs.node_supported_stages(node) == frozenset()
    assert jobs.lease_next_job(db, node) is None
    db.refresh(job)
    assert job.status == "queued"

    jobs.heartbeat(
        db,
        node,
        capabilities=_client_capabilities(
            stages=["template_registration"],
            lease_slots=1,
        ),
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )
    leased, _secret = jobs.lease_next_job(db, node)
    assert leased.id == job.id


def test_legacy_artifact_node_cannot_lease_client_evidence_work(db, tmp_path):
    node, token = jobs.register_node(db, owner_user_id=1, name="legacy-artifacts")
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
    job = _enqueue(db, tmp_path, paper_id="legacy-artifacts")

    assert jobs.lease_next_job(db, node) is None
    db.refresh(job)
    assert job.status == "queued"

    jobs.heartbeat(
        db,
        node,
        capabilities=_client_capabilities(
            stages=["template_registration"],
            lease_slots=1,
        ),
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )
    leased, _secret = jobs.lease_next_job(db, node)
    assert leased.id == job.id


@pytest.mark.parametrize(
    "stages",
    ["template_registration", ["bad stage"], [True]],
)
def test_heartbeat_rejects_malformed_stage_capabilities(db, stages):
    node, token = jobs.register_node(db, owner_user_id=1, name="malformed")
    node = jobs.authenticate_node(db, node.id, token)

    with pytest.raises(ValueError, match="capability stages"):
        jobs.heartbeat(
            db,
            node,
            capabilities={"stages": stages},
            algorithm_version=jobs.ALGORITHM_VERSION,
            free_slots=1,
        )


def test_heartbeat_rejects_malformed_artifact_contract_capability(db):
    node, token = jobs.register_node(db, owner_user_id=1, name="bad-contracts")
    node = jobs.authenticate_node(db, node.id, token)

    with pytest.raises(ValueError, match="artifact_contracts"):
        jobs.heartbeat(
            db,
            node,
            capabilities={"artifact_contracts": "client-evidence-v1"},
            algorithm_version=jobs.ALGORITHM_VERSION,
            free_slots=1,
        )


def test_hardware_readiness_is_a_hard_image_lease_gate(db, tmp_path):
    node, token = jobs.register_node(db, owner_user_id=1, name="hardware-gated")
    node = jobs.authenticate_node(db, node.id, token)
    jobs.heartbeat(
        db,
        node,
        capabilities=_client_capabilities(
            stages=["template_registration"],
            lease_slots=1,
            hardware_ready=False,
        ),
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )
    job = _enqueue(db, tmp_path, paper_id="hardware-gated")

    assert jobs.lease_next_job(db, node) is None
    db.refresh(job)
    assert job.status == "queued"
    assert jobs._node_public(node)["health_state"] == "hardware_unavailable"
    assert jobs._node_public(node)["lease_eligible"] is False

    with pytest.raises(ValueError, match="hardware readiness"):
        jobs.heartbeat(
            db,
            node,
            capabilities=_client_capabilities(
                stages=["template_registration"],
                lease_slots=1,
                hardware_ready=True,
                hardwareReady=False,
            ),
            algorithm_version=jobs.ALGORITHM_VERSION,
            free_slots=1,
        )

    jobs.heartbeat(
        db,
        node,
        capabilities=_client_capabilities(
            stages=["template_registration"],
            lease_slots=1,
            hardwareReady=True,
        ),
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )
    leased, _secret = jobs.lease_next_job(db, node)
    assert leased.id == job.id


def test_node_public_state_marks_missing_and_stale_heartbeats(db):
    node, _token = jobs.register_node(db, owner_user_id=1, name="health-state")
    assert jobs._node_public(node)["health_state"] == "never_seen"

    node.last_seen_at = jobs.utcnow() - timedelta(
        seconds=jobs.NODE_STALE_SECONDS + 1
    )
    db.commit()
    state = jobs._node_public(node)
    assert state["health_state"] == "stale"
    assert state["healthy"] is False


def test_stale_node_capabilities_cannot_claim_after_heartbeat_change(
    session_factory,
    tmp_path,
):
    seed = session_factory()
    node, token = jobs.register_node(seed, owner_user_id=1, name="capability-race")
    node = jobs.authenticate_node(seed, node.id, token)
    jobs.heartbeat(
        seed,
        node,
        capabilities=_client_capabilities(
            stages=["template_registration"],
            lease_slots=1,
        ),
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )
    job = _enqueue(seed, tmp_path, paper_id="capability-race")
    node_id = node.id
    job_id = job.id
    seed.expunge(node)
    seed.close()

    current = session_factory()
    current_node = current.get(models_accelerator.AcceleratorNode, node_id)
    jobs.heartbeat(
        current,
        current_node,
        capabilities=_client_capabilities(stages=[], lease_slots=1),
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )
    current.close()

    stale = session_factory()
    assert jobs.lease_next_job(stale, node) is None
    stale.close()
    verify = session_factory()
    stored = verify.get(AcceleratorJob, job_id)
    assert stored.status == "queued"
    assert stored.assigned_node_id is None
    verify.close()


def test_lease_route_reports_when_node_supports_no_stage(db, tmp_path):
    from routers.accelerator import api_lease

    node, token = jobs.register_node(db, owner_user_id=1, name="no-stage")
    node = jobs.authenticate_node(db, node.id, token)
    jobs.heartbeat(
        db,
        node,
        capabilities=_client_capabilities(stages=[], lease_slots=1),
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )
    queued = _enqueue(db, tmp_path, paper_id="no-stage-route")

    response = api_lease(db=db, node=node)

    assert response == {
        "job": None,
        "reason": "no_supported_stage",
        "supported_stages": [],
        "supported_artifact_contracts": ["client-evidence-v1"],
    }
    db.refresh(queued)
    assert queued.status == "queued"


def test_multi_evidence_lease_includes_template_asset(db, tmp_path):
    from routers.accelerator import api_lease
    from services.template_anchor_crops import build_anchor_crop_plan

    source, template = _files(tmp_path)
    template_sha256 = hashlib.sha256(template.read_bytes()).hexdigest()
    layout = _layout()
    layout["template_handwriting_regions"] = [{
        "question_number": "1",
        "regions": [[100, 100, 200, 200]],
    }]
    layout["question_crop_plan"] = build_anchor_crop_plan(
        {
            "column_layout": {
                "column_count": 1,
                "columns": [
                    {"column_index": 1, "x1_1000": 0, "x2_1000": 1000}
                ],
            }
        },
        artifact_sha256=template_sha256,
        expected_question_numbers=["1"],
    )
    queued = jobs.enqueue_job(
        db,
        teacher_id=1,
        paper_id="multi-evidence-lease",
        source_path=str(source),
        template_id="multi-evidence-template",
        template_path=str(template),
        layout_snapshot=layout,
        stage=jobs.STUDENT_MULTI_EVIDENCE_STAGE,
    )
    node, token = jobs.register_node(db, owner_user_id=1, name="multi-node")
    node = jobs.authenticate_node(db, node.id, token)
    jobs.heartbeat(
        db,
        node,
        capabilities={
            "stages": [jobs.STUDENT_MULTI_EVIDENCE_STAGE],
            "artifact_contracts": ["client-evidence-multi-v1"],
            "lease_slots": 1,
        },
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )
    assert api_lease(db=db, node=node)["job"] is None
    db.refresh(queued)
    assert queued.status == "queued"

    jobs.heartbeat(
        db,
        node,
        capabilities={
            "stages": [jobs.STUDENT_MULTI_EVIDENCE_STAGE],
            "artifact_contracts": ["client-evidence-multi-v3"],
            "lease_slots": 1,
        },
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )

    payload = api_lease(db=db, node=node)["job"]

    assert payload["job_id"] == queued.id
    assert payload["source_url"].endswith("/assets/source")
    assert payload["template_url"].endswith("/assets/template")
    assert payload["layout_snapshot"]["question_crop_plan"] == (
        layout["question_crop_plan"]
    )
    assert payload["layout_snapshot"]["template_handwriting_regions"] == (
        layout["template_handwriting_regions"]
    )


def test_empty_handwriting_contract_never_blocks_enqueue(db, tmp_path):
    """An empty exemption list is a valid legacy state, not a schema conflict.

    Regression: a blank template's empty handwriting list failed layout
    validation ("template handwriting regions do not match question schema"),
    which silently disabled the accelerator path for the whole batch.
    """
    from services.template_anchor_crops import build_anchor_crop_plan

    source, template = _files(tmp_path)
    template_sha256 = hashlib.sha256(template.read_bytes()).hexdigest()
    layout = _layout()
    layout["template_handwriting_regions"] = []
    layout["question_crop_plan"] = build_anchor_crop_plan(
        {
            "column_layout": {
                "column_count": 1,
                "columns": [
                    {"column_index": 1, "x1_1000": 0, "x2_1000": 1000}
                ],
            }
        },
        artifact_sha256=template_sha256,
        expected_question_numbers=["1"],
    )

    queued = jobs.enqueue_job(
        db,
        teacher_id=1,
        paper_id="empty-handwriting",
        source_path=str(source),
        template_id="empty-handwriting-template",
        template_path=str(template),
        layout_snapshot=layout,
        stage=jobs.STUDENT_MULTI_EVIDENCE_STAGE,
    )

    stored = json.loads(queued.layout_snapshot_json)
    assert stored["template_handwriting_regions"] == []


def test_partial_handwriting_contract_still_fails_closed(db, tmp_path):
    """Any non-empty exemption contract must still name every question."""
    from services.template_anchor_crops import build_anchor_crop_plan

    source, template = _files(tmp_path)
    template_sha256 = hashlib.sha256(template.read_bytes()).hexdigest()
    layout = _layout()
    layout["answer_regions"] = [
        {"question_number": number, "question_type": "solve", "answer_bbox": bbox}
        for number, bbox in (("1", [10, 10, 500, 300]), ("2", [510, 310, 900, 600]))
    ]
    layout["template_handwriting_regions"] = [
        {"question_number": "1", "regions": [[100, 100, 200, 200]]}
    ]
    layout["question_crop_plan"] = build_anchor_crop_plan(
        {
            "column_layout": {
                "column_count": 1,
                "columns": [
                    {"column_index": 1, "x1_1000": 0, "x2_1000": 1000}
                ],
            }
        },
        artifact_sha256=template_sha256,
        expected_question_numbers=["1", "2"],
    )

    with pytest.raises(ValueError, match="do not match question schema"):
        jobs.enqueue_job(
            db,
            teacher_id=1,
            paper_id="partial-handwriting",
            source_path=str(source),
            template_id="partial-handwriting-template",
            template_path=str(template),
            layout_snapshot=layout,
            stage=jobs.STUDENT_MULTI_EVIDENCE_STAGE,
        )


def test_heartbeat_route_maps_bad_stage_capability_to_422(db):
    from fastapi import HTTPException
    from routers.accelerator import HeartbeatRequest, api_heartbeat

    node, token = jobs.register_node(db, owner_user_id=1, name="bad-heartbeat")
    node = jobs.authenticate_node(db, node.id, token)
    request = HeartbeatRequest(
        capabilities={"stages": "template_registration"},
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )

    with pytest.raises(HTTPException) as captured:
        api_heartbeat(request, db=db, node=node)

    assert captured.value.status_code == 422
    assert "capability stages" in str(captured.value.detail)


def test_node_token_is_hashed_and_contract_idempotency_includes_layout(db, tmp_path):
    node, token = jobs.register_node(db, owner_user_id=1, name="node")
    assert node.token_hash != token
    assert jobs.authenticate_node(db, node.id, token).id == node.id
    assert jobs.authenticate_node(db, node.id, "wrong") is None

    first = _enqueue(db, tmp_path)
    second = _enqueue(db, tmp_path)
    changed_layout = _enqueue(db, tmp_path, layout_snapshot=_layout("2"))
    assert first.id == second.id
    assert changed_layout.id != first.id
    assert db.query(AcceleratorJob).count() == 2


def test_teacher_desktop_node_can_recover_own_shared_lane(db, tmp_path):
    node, token = jobs.register_node(
        db,
        owner_user_id=1,
        name="teacher desktop",
        lane="teacher_desktop",
        device_id="device-1",
        token_ttl_days=90,
    )
    authenticated = jobs.authenticate_node(db, node.id, token)
    jobs.heartbeat(
        db,
        authenticated,
        capabilities=_client_capabilities(platform="windows"),
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )
    web_job = _enqueue(db, tmp_path, paper_id="paper-web", lane="web_gpu")
    desktop_job = _enqueue(
        db,
        tmp_path,
        paper_id="paper-desktop",
        lane="teacher_desktop",
    )
    leased, _secret = jobs.lease_next_job(db, authenticated)
    assert leased.id == web_job.id
    assert leased.lane == "web_gpu"
    db.refresh(desktop_job)
    assert desktop_job.status == "queued"


def test_platform_node_can_recover_desktop_preferred_lane(db, tmp_path):
    desktop_job = _enqueue(
        db,
        tmp_path,
        paper_id="paper-desktop-recovery",
        lane="teacher_desktop",
    )
    node, token = jobs.register_node(
        db,
        owner_user_id=1,
        name="platform node",
        access_scope="platform",
    )
    authenticated = jobs.authenticate_node(db, node.id, token)
    jobs.heartbeat(
        db,
        authenticated,
        capabilities=_client_capabilities(platform="windows"),
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )

    leased, _secret = jobs.lease_next_job(db, authenticated)
    assert leased.id == desktop_job.id
    assert leased.lane == "teacher_desktop"


def test_same_contract_is_idempotent_across_preferred_lanes(db, tmp_path):
    shared = _enqueue(db, tmp_path, lane="web_gpu")
    desktop_retry = _enqueue(db, tmp_path, lane="teacher_desktop")

    assert desktop_retry.id == shared.id
    assert desktop_retry.lane == "web_gpu"


def test_explicit_retry_revives_same_offload_job_id(db, tmp_path):
    source = tmp_path / "canonical-retry.jpg"
    source.write_bytes(b"canonical-retry-source")
    job = jobs.enqueue_job(
        db,
        teacher_id=1,
        paper_id="canonical-retry",
        source_path=str(source),
        template_id="canonical-retry",
        template_path=str(source),
        layout_snapshot={"operation_id": "canonical-retry"},
        stage=jobs.CANONICALIZATION_STAGE,
        lane="web_gpu",
    )
    candidate_root = jobs.candidate_artifact_path(
        job.source_path,
        job.id,
        "canonical_template",
    ).parent
    candidate_root.mkdir(parents=True, exist_ok=True)
    (candidate_root / "canonical_template.png").write_bytes(b"stale")
    old_deadline = jobs.utcnow() - timedelta(seconds=1)
    job.status = "fallback_failed"
    job.attempt_count = jobs.MAX_ATTEMPTS
    job.error_code = "job_deadline_exceeded"
    job.deadline_at = old_deadline
    job.candidate_json = "{}"
    job.candidate_sha256 = "a" * 64
    job.server_validation_status = "timed_out"
    job.server_validation_json = "{}"
    job.candidate_received_at = old_deadline
    job.validated_at = old_deadline
    job.fallback_at = old_deadline
    db.commit()

    resumed = jobs.enqueue_job(
        db,
        teacher_id=1,
        paper_id="canonical-retry",
        source_path=str(source),
        template_id="canonical-retry",
        template_path=str(source),
        layout_snapshot={"operation_id": "canonical-retry"},
        stage=jobs.CANONICALIZATION_STAGE,
        lane="teacher_desktop",
    )

    assert resumed.id == job.id
    assert resumed.lane == "web_gpu"
    assert resumed.status == "queued"
    assert resumed.attempt_count == 0
    assert resumed.error_code is None
    assert resumed.deadline_at > jobs.utcnow()
    assert resumed.candidate_json is None
    assert resumed.candidate_sha256 is None
    assert resumed.server_validation_status == "not_started"
    assert resumed.server_validation_json is None
    assert resumed.candidate_received_at is None
    assert resumed.validated_at is None
    assert resumed.fallback_at is None
    assert not candidate_root.exists()


@pytest.mark.parametrize(
    ("status", "error_code"),
    [
        ("fallback_failed", "server_validation_failed"),
        ("superseded", "job_deadline_exceeded"),
        ("consumed", None),
    ],
)
def test_permanent_offload_terminal_is_not_revived(
    db,
    tmp_path,
    status,
    error_code,
):
    source = tmp_path / f"canonical-{status}.jpg"
    source.write_bytes(b"canonical-permanent-source")
    job = jobs.enqueue_job(
        db,
        teacher_id=1,
        paper_id=f"canonical-{status}",
        source_path=str(source),
        template_id=f"canonical-{status}",
        template_path=str(source),
        layout_snapshot={"operation_id": f"canonical-{status}"},
        stage=jobs.CANONICALIZATION_STAGE,
        lane="web_gpu",
    )
    job.status = status
    job.error_code = error_code
    job.attempt_count = jobs.MAX_ATTEMPTS
    job.deadline_at = jobs.utcnow() - timedelta(seconds=1)
    db.commit()

    repeated = jobs.enqueue_job(
        db,
        teacher_id=1,
        paper_id=f"canonical-{status}",
        source_path=str(source),
        template_id=f"canonical-{status}",
        template_path=str(source),
        layout_snapshot={"operation_id": f"canonical-{status}"},
        stage=jobs.CANONICALIZATION_STAGE,
        lane="teacher_desktop",
    )

    assert repeated.id == job.id
    assert repeated.status == status
    assert repeated.attempt_count == jobs.MAX_ATTEMPTS
    assert repeated.error_code == error_code


def test_rotating_desktop_token_invalidates_old_token_and_requeues_lease(db, tmp_path):
    node, old_token = jobs.register_node(
        db,
        owner_user_id=1,
        name="teacher desktop",
        lane="teacher_desktop",
        device_id="device-rotate",
        token_ttl_days=90,
    )
    authenticated = jobs.authenticate_node(db, node.id, old_token)
    jobs.heartbeat(
        db,
        authenticated,
        capabilities=_client_capabilities(),
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )
    job = _enqueue(db, tmp_path, lane="teacher_desktop")
    jobs.lease_next_job(db, authenticated)
    rotated, new_token = jobs.rotate_node_token(
        db,
        node.id,
        owner_user_id=1,
        token_ttl_days=30,
    )
    assert new_token != old_token
    assert jobs.authenticate_node(db, node.id, old_token) is None
    assert jobs.authenticate_node(db, node.id, new_token).id == node.id
    db.refresh(job)
    assert job.status == "queued"
    assert job.error_code == "node_token_rotated_requeued"


def test_candidate_receipt_is_idempotent_but_not_validated(db, tmp_path):
    node, _token = _ready_node(db)
    job = _enqueue(db, tmp_path)
    leased_job, lease_id = jobs.lease_next_job(db, node)
    assert leased_job.id == job.id

    received = jobs.submit_result(db, node, job_id=job.id, lease_secret=lease_id, result=_result())
    assert received.status == "candidate_received"
    assert received.server_validation_status == "pending"
    assert received.lease_id_hash != lease_id
    replayed = jobs.submit_result(db, node, job_id=job.id, lease_secret=lease_id, result=_result())
    assert replayed.id == received.id
    assert replayed.candidate_sha256 == received.candidate_sha256

    conflict = _result()
    conflict["coverage"] = 0.9
    with pytest.raises(ValueError, match="conflicts"):
        jobs.submit_result(db, node, job_id=job.id, lease_secret=lease_id, result=conflict)

    validating = jobs.begin_server_validation(db, job.id)
    assert validating.status == "server_validating"
    validated = jobs.finish_server_validation(db, job.id, accepted=True, validation={"source_mask_replayed": True})
    assert validated.status == "validated"
    assert validated.server_validation_status == "accepted"


def test_worker_claims_remain_untrusted_until_server_validation(db, tmp_path):
    node, _token = _ready_node(db)
    job = _enqueue(db, tmp_path)
    _leased, lease_id = jobs.lease_next_job(db, node)
    result = _result()
    result["answer_source_complete"] = False
    result["incomplete_answer_regions"] = ["1"]
    result["evidence_diagnostics"]["answer_source_coverage"][0].update({
        "covered_pixels": 799_999,
        "coverage": 0.999999,
        "complete": False,
    })
    result["evidence_diagnostics"]["incomplete_answer_regions"] = ["1"]
    received = jobs.submit_result(db, node, job_id=job.id, lease_secret=lease_id, result=result)
    payload = json.loads(received.candidate_json)
    assert received.status == "candidate_received"
    assert payload["answer_source_complete"] is False
    assert received.server_validation_status == "pending"

    jobs.begin_server_validation(db, job.id)
    rejected = jobs.finish_server_validation(db, job.id, accepted=False, validation={"reason": "source_mask_incomplete"})
    assert rejected.status == "fallback_failed"
    assert rejected.server_validation_status == "rejected"


def test_expired_lease_requeues_then_falls_back_after_attempt_limit(db, tmp_path, monkeypatch):
    monkeypatch.setattr(accelerator_jobs_queue, "MAX_ATTEMPTS", 2)
    monkeypatch.setattr(accelerator_jobs_base, "MAX_ATTEMPTS", 2)
    monkeypatch.setattr(jobs, "MAX_ATTEMPTS", 2)
    node, _token = _ready_node(db)
    job = _enqueue(db, tmp_path)
    first, _lease = jobs.lease_next_job(db, node)
    first.lease_expires_at = jobs.utcnow() - timedelta(seconds=1)
    db.commit()
    second, _lease2 = jobs.lease_next_job(db, node)
    assert second.id == job.id and second.attempt_count == 2
    candidate_root = jobs.candidate_artifact_path(
        second.source_path,
        second.id,
        "aligned_page",
    ).parent
    candidate_root.mkdir(parents=True, exist_ok=True)
    (candidate_root / "aligned_page.png").write_bytes(b"stale-aligned")
    (candidate_root / "source_mask.png").write_bytes(b"stale-mask")
    second.lease_expires_at = jobs.utcnow() - timedelta(seconds=1)
    db.commit()
    assert jobs.lease_next_job(db, node) is None
    db.refresh(job)
    assert job.status == "fallback_failed"
    assert job.error_code == "lease_attempts_exhausted"
    assert not candidate_root.exists()


def test_deadline_stops_registration_without_server_fallback(db, tmp_path):
    job = _enqueue(db, tmp_path)
    job.deadline_at = jobs.utcnow() - timedelta(seconds=1)
    db.commit()
    assert jobs.expire_leases(db) == 1
    db.refresh(job)
    assert job.status == "fallback_failed"
    assert job.error_code == "job_deadline_exceeded"


@pytest.mark.parametrize("status", ["candidate_received", "server_validating"])
def test_deadline_recovers_abandoned_server_validation(
    db, tmp_path, status
):
    job = _enqueue(db, tmp_path)
    candidate_root = jobs.candidate_artifact_path(
        job.source_path,
        job.id,
        "aligned_page",
    ).parent
    candidate_root.mkdir(parents=True, exist_ok=True)
    (candidate_root / "aligned_page.png").write_bytes(b"stale")
    job.status = status
    job.server_validation_status = (
        "pending" if status == "candidate_received" else "running"
    )
    job.deadline_at = jobs.utcnow() - timedelta(seconds=1)
    db.commit()

    assert jobs.expire_leases(db) == 1
    db.refresh(job)
    assert job.status == "fallback_failed"
    assert job.server_validation_status == "timed_out"
    assert job.error_code == "server_validation_deadline_exceeded"
    assert not candidate_root.exists()


def test_reaper_abandons_only_stale_cpu_fallback_owner(db, tmp_path):
    now = jobs.utcnow()
    stale = _enqueue(db, tmp_path, paper_id="paper-stale")
    active = _enqueue(db, tmp_path, paper_id="paper-active")
    stale.status = "fallback_running"
    stale.server_validation_status = "cpu_running"
    stale.fallback_at = now - timedelta(
        seconds=jobs.CPU_FALLBACK_STALE_SECONDS + 1
    )
    active.status = "fallback_running"
    active.server_validation_status = "cpu_running"
    active.fallback_at = now
    db.commit()

    assert jobs.expire_leases(db, now) == 1
    db.refresh(stale)
    db.refresh(active)
    assert stale.status == "fallback_failed"
    assert stale.server_validation_status == "cpu_abandoned"
    assert stale.error_code == "cpu_fallback_owner_lost"
    assert active.status == "fallback_running"
    assert jobs.expire_leases(db, now) == 0


def test_worker_result_rejects_non_finite_or_malformed_geometry(db, tmp_path):
    node, _token = _ready_node(db)
    job = _enqueue(db, tmp_path)
    _leased, lease_id = jobs.lease_next_job(db, node)
    result = _result()
    result["homography"] = [[1, 0, 0], [0, float("nan"), 0], [0, 0, 1]]
    with pytest.raises(ValueError, match="non-finite"):
        jobs.submit_result(db, node, job_id=job.id, lease_secret=lease_id, result=result)


def test_two_independent_sessions_only_create_one_live_lease(session_factory, tmp_path):
    seed = session_factory()
    _ready_node(seed)
    node_id = seed.query(models_accelerator.AcceleratorNode.id).scalar()
    _enqueue(seed, tmp_path)
    seed.close()
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def claim():
        session = session_factory()
        try:
            node = session.query(models_accelerator.AcceleratorNode).filter_by(id=node_id).one()
            barrier.wait(timeout=5)
            leased = jobs.lease_next_job(session, node)
            results.append(leased[0].id if leased else None)
        except Exception as exc:  # pragma: no cover - assertion below reports it
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=claim), threading.Thread(target=claim)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors
    assert len([value for value in results if value]) == 1
    verify = session_factory()
    assert verify.query(AcceleratorJob).filter_by(status="leased").count() == 1
    verify.close()


def test_revoking_node_invalidates_token_and_requeues_live_job(db, tmp_path):
    node, token = _ready_node(db)
    job = _enqueue(db, tmp_path)
    jobs.lease_next_job(db, node)
    revoked = jobs.revoke_node(db, node.id)
    assert not revoked.enabled and revoked.revoked_at is not None
    assert jobs.authenticate_node(db, node.id, token) is None
    db.refresh(job)
    assert job.status == "queued"
    assert job.error_code == "node_revoked_requeued"


def test_stale_authenticated_node_cannot_claim_after_concurrent_revoke(
    session_factory,
    tmp_path,
):
    seed = session_factory()
    node, _token = _ready_node(seed)
    job = _enqueue(seed, tmp_path, paper_id="paper-revoke-race")
    seed.refresh(node)
    node_id = node.id
    job_id = job.id
    seed.expunge(node)
    seed.close()

    revoker = session_factory()
    jobs.revoke_node(revoker, node_id)
    revoker.close()

    stale_request = session_factory()
    assert jobs.lease_next_job(stale_request, node) is None
    stale_request.close()

    verify = session_factory()
    stored = verify.get(AcceleratorJob, job_id)
    assert stored.status == "queued"
    assert stored.assigned_node_id is None
    verify.close()


def test_stale_authenticated_node_cannot_claim_after_token_rotation(
    session_factory,
    tmp_path,
):
    seed = session_factory()
    node, old_token = jobs.register_node(
        seed,
        owner_user_id=1,
        name="desktop",
        lane="teacher_desktop",
        device_id="rotation-race",
        token_ttl_days=90,
    )
    node = jobs.authenticate_node(seed, node.id, old_token)
    jobs.heartbeat(
        seed,
        node,
        capabilities=_client_capabilities(lease_slots=1),
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )
    job = _enqueue(
        seed,
        tmp_path,
        paper_id="paper-rotation-race",
        lane="teacher_desktop",
    )
    node_id = node.id
    job_id = job.id
    seed.expunge(node)
    seed.close()

    rotator = session_factory()
    jobs.rotate_node_token(
        rotator,
        node_id,
        owner_user_id=1,
        token_ttl_days=90,
    )
    rotator.close()

    stale_request = session_factory()
    assert jobs.lease_next_job(stale_request, node) is None
    stale_request.close()

    verify = session_factory()
    stored = verify.get(AcceleratorJob, job_id)
    assert stored.status == "queued"
    assert stored.assigned_node_id is None
    verify.close()


def test_idempotent_candidate_replay_survives_validation_boundary(db, tmp_path):
    node, _token = _ready_node(db)
    job = _enqueue(db, tmp_path)
    _leased, lease_id = jobs.lease_next_job(db, node)
    jobs.submit_result(db, node, job_id=job.id, lease_secret=lease_id, result=_result())
    jobs.begin_server_validation(db, job.id)
    jobs.finish_server_validation(db, job.id, accepted=True, validation={"replayed": True})
    replayed = jobs.submit_result(db, node, job_id=job.id, lease_secret=lease_id, result=_result())
    assert replayed.status == "validated"
    assert replayed.server_validation_status == "accepted"


def test_foreign_node_cannot_replay_received_candidate(db, tmp_path):
    node, _token = _ready_node(db)
    foreign, _foreign_token = _ready_node(db)
    job = _enqueue(db, tmp_path)
    _leased, lease_id = jobs.lease_next_job(db, node)
    jobs.submit_result(db, node, job_id=job.id, lease_secret=lease_id, result=_result())
    with pytest.raises(PermissionError, match="foreign"):
        jobs.submit_result(db, foreign, job_id=job.id, lease_secret=lease_id, result=_result())


def test_node_active_lease_count_is_bounded_by_advertised_slots(db, tmp_path):
    node, _token = _ready_node(db)
    first = _enqueue(db, tmp_path, paper_id="paper-1")
    second = _enqueue(db, tmp_path, paper_id="paper-2")
    leased = jobs.lease_next_job(db, node)[0]
    assert leased.id in {first.id, second.id}
    assert jobs.lease_next_job(db, node) is None
    queued = second if leased.id == first.id else first
    db.refresh(queued)
    assert queued.status == "queued"


def test_remaining_slots_are_separate_from_total_lease_capacity(db, tmp_path):
    node, _token = _ready_node(db)
    jobs.heartbeat(
        db,
        node,
        capabilities=_client_capabilities(lease_slots=2),
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=2,
    )
    first = _enqueue(db, tmp_path, paper_id="paper-capacity-1")
    second = _enqueue(db, tmp_path, paper_id="paper-capacity-2")
    third = _enqueue(db, tmp_path, paper_id="paper-capacity-3")

    assert jobs.lease_next_job(db, node)[0].id == first.id
    jobs.heartbeat(
        db,
        node,
        capabilities=_client_capabilities(lease_slots=2),
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )
    assert jobs.lease_next_job(db, node)[0].id == second.id
    assert jobs.lease_next_job(db, node) is None
    db.refresh(third)
    assert third.status == "queued"


def test_asset_hash_change_is_rejected(db, tmp_path):
    node, _token = _ready_node(db)
    job = _enqueue(db, tmp_path)
    leased, _lease = jobs.lease_next_job(db, node)
    source = Path(job.source_path)
    source.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="changed"):
        jobs.verify_job_asset(leased, "source")


def test_tenant_node_cannot_claim_or_download_another_teacher_job(db, tmp_path):
    db.add(User(id=2, name="其他教师", role="teacher", status="active"))
    db.commit()
    foreign = _enqueue(db, tmp_path, paper_id="paper-foreign")
    foreign.teacher_id = 2
    db.commit()

    node, _token = _ready_node(db)
    assert node.owner_user_id == 1
    assert node.access_scope == "tenant"
    assert jobs.lease_next_job(db, node) is None
    db.refresh(foreign)
    assert foreign.status == "queued"
    assert foreign.assigned_node_id is None


def test_explicit_platform_node_can_claim_cross_tenant_job(db, tmp_path):
    db.add(User(id=2, name="其他教师", role="teacher", status="active"))
    db.commit()
    foreign = _enqueue(db, tmp_path, paper_id="paper-platform")
    foreign.teacher_id = 2
    db.commit()

    node, token = jobs.register_node(
        db,
        owner_user_id=1,
        name="platform node",
        access_scope="platform",
    )
    node = jobs.authenticate_node(db, node.id, token)
    jobs.heartbeat(
        db,
        node,
        capabilities=_client_capabilities(gpu="RTX 4060", batch_size=8),
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )
    leased, _lease = jobs.lease_next_job(db, node)
    assert leased.id == foreign.id
    assert leased.teacher_id == 2


def test_platform_node_balances_live_leases_across_teachers(db, tmp_path):
    db.add(User(id=2, name="其他教师", role="teacher", status="active"))
    db.commit()
    first = _enqueue(db, tmp_path, paper_id="teacher-one-first")
    _enqueue(db, tmp_path, paper_id="teacher-one-second")
    other = _enqueue(db, tmp_path, paper_id="teacher-two-first")
    other.teacher_id = 2
    db.commit()

    node, token = jobs.register_node(
        db,
        owner_user_id=1,
        name="shared platform",
        access_scope="platform",
    )
    node = jobs.authenticate_node(db, node.id, token)
    jobs.heartbeat(
        db,
        node,
        capabilities=_client_capabilities(lease_slots=2),
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=2,
    )

    first_lease, _ = jobs.lease_next_job(db, node)
    second_lease, _ = jobs.lease_next_job(db, node)

    assert first_lease.id == first.id
    assert second_lease.id == other.id
    assert {first_lease.teacher_id, second_lease.teacher_id} == {1, 2}


def test_platform_fairness_skips_teachers_with_unsupported_provider_profile(
    db,
    tmp_path,
):
    db.add(User(id=2, name="其他教师", role="teacher", status="active"))
    db.commit()
    unsupported = _enqueue(db, tmp_path, paper_id="teacher-one-retry-profile")
    unsupported.stage = jobs.VISION_MODEL_STAGE
    unsupported.template_id = "retry"
    unsupported.layout_snapshot_json = json.dumps(
        {"model": "gpt-5.6-sol"},
        separators=(",", ":"),
    )
    supported = _enqueue(db, tmp_path, paper_id="teacher-two-primary-profile")
    supported.teacher_id = 2
    supported.stage = jobs.VISION_MODEL_STAGE
    supported.template_id = "primary"
    supported.layout_snapshot_json = json.dumps(
        {"model": "gpt-5.6-sol"},
        separators=(",", ":"),
    )
    db.commit()

    node, token = jobs.register_node(
        db,
        owner_user_id=1,
        name="primary-only-platform",
        access_scope="platform",
    )
    node = jobs.authenticate_node(db, node.id, token)
    jobs.heartbeat(
        db,
        node,
        capabilities={
            "backend": "provider_api",
            "stages": [jobs.VISION_MODEL_STAGE],
            "artifact_contracts": [jobs.VISION_MODEL_RESPONSE_CONTRACT_VERSION],
            "provider_profiles": ["primary"],
            "provider_models": {"primary": "gpt-5.6-sol"},
            "stage_slots": {
                "provider_api": {
                    "stages": [jobs.VISION_MODEL_STAGE],
                    "capacity": 1,
                }
            },
        },
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )

    leased, _secret = jobs.lease_next_job(
        db,
        node,
        requested_stages=[jobs.VISION_MODEL_STAGE],
    )

    assert leased.id == supported.id
    assert leased.teacher_id == 2


def test_platform_nodes_concurrently_lease_different_teachers(
    session_factory,
    tmp_path,
    monkeypatch,
):
    seed = session_factory()
    seed.add(User(id=2, name="other teacher", role="teacher", status="active"))
    seed.commit()
    _enqueue(seed, tmp_path, paper_id="teacher-one-first")
    _enqueue(seed, tmp_path, paper_id="teacher-one-second")
    for paper_id in ("teacher-two-first", "teacher-two-second"):
        row = _enqueue(seed, tmp_path, paper_id=paper_id)
        row.teacher_id = 2
    node_ids = []
    for name in ("platform-a", "platform-b"):
        node, token = jobs.register_node(
            seed,
            owner_user_id=1,
            name=name,
            access_scope="platform",
        )
        node = jobs.authenticate_node(seed, node.id, token)
        jobs.heartbeat(
            seed,
            node,
            capabilities=_client_capabilities(lease_slots=1),
            algorithm_version=jobs.ALGORITHM_VERSION,
            free_slots=1,
        )
        node_ids.append(node.id)
    seed.commit()
    seed.close()

    # Both threads pause immediately before the atomic candidate UPDATE so they
    # enter the allocation statement from the same pre-lease teacher state.
    barrier = threading.Barrier(2)
    local = threading.local()
    real_token_urlsafe = jobs.secrets.token_urlsafe

    def synchronized_token_urlsafe(size):
        if not getattr(local, "candidate_selected", False):
            local.candidate_selected = True
            barrier.wait(timeout=5)
        return real_token_urlsafe(size)

    monkeypatch.setattr(jobs.secrets, "token_urlsafe", synchronized_token_urlsafe)
    teacher_ids = []
    errors = []

    def claim(node_id):
        session = session_factory()
        try:
            node = session.get(models_accelerator.AcceleratorNode, node_id)
            leased = jobs.lease_next_job(session, node)
            teacher_ids.append(leased[0].teacher_id if leased else None)
        except Exception as exc:  # pragma: no cover - assertion below reports it
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=claim, args=(node_id,)) for node_id in node_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert sorted(teacher_ids) == [1, 2]
    verify = session_factory()
    leased_teachers = {
        row[0]
        for row in verify.query(AcceleratorJob.teacher_id).filter(
            AcceleratorJob.status == "leased"
        ).all()
    }
    assert leased_teachers == {1, 2}
    verify.close()


def test_stale_session_cannot_submit_after_other_session_releases_lease(
    session_factory,
    tmp_path,
):
    stale = session_factory()
    node, _token = _ready_node(stale)
    job = _enqueue(stale, tmp_path)
    job_id = job.id
    _leased, lease_id = jobs.lease_next_job(stale, node)
    stale.get(AcceleratorJob, job_id)

    authoritative = session_factory()
    authoritative_job = authoritative.get(AcceleratorJob, job_id)
    authoritative_job.lease_expires_at = jobs.utcnow() - timedelta(seconds=1)
    authoritative.commit()
    assert jobs.expire_leases(authoritative) == 1
    authoritative.close()

    with pytest.raises(PermissionError, match="expired"):
        jobs.submit_result(
            stale,
            node,
            job_id=job_id,
            lease_secret=lease_id,
            result=_result(),
        )
    stale.rollback()
    stale.close()

    verify = session_factory()
    stored = verify.get(AcceleratorJob, job_id)
    assert stored.status == "queued"
    assert stored.candidate_json is None
    verify.close()


def test_reaper_recovers_expired_lease_without_another_claim(
    session_factory,
    tmp_path,
    monkeypatch,
):
    seed = session_factory()
    node, _token = _ready_node(seed)
    job = _enqueue(seed, tmp_path)
    leased, _lease = jobs.lease_next_job(seed, node)
    leased.lease_expires_at = jobs.utcnow() - timedelta(seconds=1)
    seed.commit()
    job_id = job.id
    seed.close()

    from services import accelerator_reaper

    monkeypatch.setattr(accelerator_reaper, "SessionLocal", session_factory)
    assert accelerator_reaper.reap_expired_accelerator_jobs_once() == 1

    verify = session_factory()
    stored = verify.get(AcceleratorJob, job_id)
    assert stored.status == "queued"
    assert stored.error_code == "lease_expired_requeued"
    verify.close()


def test_stale_old_lease_cannot_submit_after_job_is_released_to_new_node(
    session_factory,
    tmp_path,
):
    seed = session_factory()
    old_node, _ = _ready_node(seed)
    job = _enqueue(seed, tmp_path)
    job_id = job.id
    _, old_lease = jobs.lease_next_job(seed, old_node)
    old_node_id = old_node.id

    new_node, new_token = jobs.register_node(seed, owner_user_id=1, name="new node")
    new_node = jobs.authenticate_node(seed, new_node.id, new_token)
    jobs.heartbeat(
        seed,
        new_node,
        capabilities=_client_capabilities(),
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )
    new_node_id = new_node.id
    seed.close()

    authoritative = session_factory()
    current = authoritative.get(AcceleratorJob, job_id)
    current.lease_expires_at = jobs.utcnow() - timedelta(seconds=1)
    authoritative.commit()
    assert jobs.expire_leases(authoritative) == 1
    current_new_node = authoritative.get(models_accelerator.AcceleratorNode, new_node_id)
    leased_to_new, _new_lease = jobs.lease_next_job(authoritative, current_new_node)
    assert leased_to_new.assigned_node_id == new_node_id
    authoritative.close()

    stale = session_factory()
    current_old_node = stale.get(models_accelerator.AcceleratorNode, old_node_id)
    with pytest.raises(PermissionError, match="expired"):
        jobs.submit_result(
            stale,
            current_old_node,
            job_id=job_id,
            lease_secret=old_lease,
            result=_result(),
        )
    stale.rollback()
    stale.close()

    verify = session_factory()
    stored = verify.get(AcceleratorJob, job_id)
    assert stored.status == "leased"
    assert stored.assigned_node_id == new_node_id
    assert stored.candidate_json is None
    verify.close()
