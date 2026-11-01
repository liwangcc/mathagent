from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import timedelta
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models_accelerator  # noqa: F401
import models_user  # noqa: F401
from database import Base
from models_accelerator import AcceleratorJob
from models_user import User
from services import accelerator_artifacts as artifacts
from services import accelerator_business as business
from services import accelerator_jobs as jobs
from services import accelerator_validation as validation
from services.template_canonicalization_contract import (
    CANONICALIZATION_STAGE,
    CANONICAL_TEMPLATE_CONTRACT_VERSION,
)


def _session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'canonical-accelerator.db'}",
        connect_args={"check_same_thread": False},
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(engine)
    db = Session()
    db.add(User(id=1, name="teacher", role="teacher", status="active"))
    db.commit()
    db.close()
    return Session


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source.jpg"
    source.write_bytes(b"immutable-source-image")
    return source


def _enqueue(db, source: Path, *, operation_id: str = "canonical-op"):
    return jobs.enqueue_job(
        db,
        teacher_id=1,
        paper_id=operation_id,
        source_path=str(source),
        template_id=operation_id,
        template_path=str(source),
        layout_snapshot={"operation_id": operation_id},
        stage=CANONICALIZATION_STAGE,
        lane="teacher_desktop",
    )


def _node(db, *, free_slots: int = 1, contracts=None, stages=None):
    node, token = jobs.register_node(
        db,
        owner_user_id=1,
        name="canonical desktop",
        lane="teacher_desktop",
        device_id="canonical-device",
    )
    node = jobs.authenticate_node(db, node.id, token)
    jobs.heartbeat(
        db,
        node,
        capabilities={
            "stages": stages or [CANONICALIZATION_STAGE],
            "artifact_contracts": contracts or [
                CANONICAL_TEMPLATE_CONTRACT_VERSION
            ],
            "lease_slots": 1,
        },
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=free_slots,
    )
    return node


def _canonical_png(tmp_path: Path) -> Path:
    path = tmp_path / "canonical.png"
    Image.new("RGB", (2064, 2064), "white").save(path, format="PNG")
    return path


def _metadata(source_sha256: str, canonical_file: dict) -> dict:
    return {
        "version": "paper-canonical-v1",
        "algorithm": "opencv_page_quad_v1",
        "source_sha256": source_sha256,
        "source_format": "jpeg",
        "exif_orientation": 1,
        "encoded_width": 2000,
        "encoded_height": 2000,
        "source_width": 2000,
        "source_height": 2000,
        "source_coordinate_space": "exif_display_pixels_top_left",
        "canonical_coordinate_space": "canonical_template_v1",
        "source_to_canonical": [
            [1.0, 0.0, 32.0],
            [0.0, 1.0, 32.0],
            [0.0, 0.0, 1.0],
        ],
        "detected_page_quad": [
            [0.0, 0.0],
            [1999.0, 0.0],
            [1999.0, 1999.0],
            [0.0, 1999.0],
        ],
        "detection_method": "full_frame_scan",
        "confidence": 1.0,
        "page_margin_px": 32,
        "canonical_sha256": canonical_file["sha256"],
        "canonical_width": canonical_file["width"],
        "canonical_height": canonical_file["height"],
    }


def _result(source_sha256: str, canonical_file: dict) -> dict:
    return {
        "status": "succeeded",
        "artifact_manifest": {
            "contract_version": CANONICAL_TEMPLATE_CONTRACT_VERSION,
            "canonical_template": canonical_file,
        },
        "canonicalization": _metadata(source_sha256, canonical_file),
    }


def _stage_candidate(db, tmp_path: Path):
    source = _source(tmp_path)
    node = _node(db)
    job = _enqueue(db, source)
    leased, lease = jobs.lease_next_job(db, node)
    canonical = _canonical_png(tmp_path)
    canonical_file = artifacts.validate_uploaded_artifact_file(
        canonical,
        "canonical_template",
    )
    target = artifacts.candidate_artifact_path(
        source,
        job.id,
        "canonical_template",
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(canonical.read_bytes())
    received = jobs.submit_result(
        db,
        node,
        job_id=job.id,
        lease_secret=lease,
        result=_result(job.source_sha256, canonical_file),
    )
    return source, received, canonical_file


@pytest.mark.parametrize("mutation", ["truncate", "crc"])
def test_canonical_png_integrity_is_checked_without_image_decode(
    tmp_path,
    monkeypatch,
    mutation,
):
    path = _canonical_png(tmp_path)
    raw = bytearray(path.read_bytes())
    if mutation == "truncate":
        raw = raw[:-5]
    else:
        raw[-1] ^= 0x01
    path.write_bytes(raw)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("image decoder was invoked")

    monkeypatch.setattr(artifacts.cv2, "imdecode", forbidden)
    monkeypatch.setattr(artifacts.Image, "open", forbidden)
    with pytest.raises(ValueError, match="PNG"):
        artifacts.validate_uploaded_artifact_file(path, "canonical_template")


def test_canonical_result_rejects_non_finite_geometry(tmp_path):
    canonical = _canonical_png(tmp_path)
    canonical_file = artifacts.validate_uploaded_artifact_file(
        canonical,
        "canonical_template",
    )
    source_sha256 = "a" * 64
    result = _result(source_sha256, canonical_file)
    result["canonicalization"]["source_to_canonical"][0][0] = float("nan")

    with pytest.raises(ValueError, match="source_to_canonical"):
        jobs._validate_candidate_payload(
            result,
            stage=CANONICALIZATION_STAGE,
            source_sha256=source_sha256,
        )


def test_stage_contract_intersection_and_artifact_kind_are_strict(tmp_path):
    Session = _session_factory(tmp_path)
    db = Session()
    source = _source(tmp_path)
    registration_source = tmp_path / "registration-source.jpg"
    registration_template = tmp_path / "registration-template.jpg"
    registration_source.write_bytes(b"source")
    registration_template.write_bytes(b"template")
    registration = jobs.enqueue_job(
        db,
        teacher_id=1,
        paper_id="registration",
        source_path=str(registration_source),
        template_id="registration",
        template_path=str(registration_template),
        layout_snapshot={
            "page_type_id": "page_1",
            "answer_regions": [{
                "question_number": "1",
                "question_type": "solve",
                "answer_bbox": [0, 0, 100, 100],
            }],
        },
        lane="teacher_desktop",
    )
    canonical = _enqueue(db, source)
    node = _node(
        db,
        stages=["template_registration", CANONICALIZATION_STAGE],
        contracts=[CANONICAL_TEMPLATE_CONTRACT_VERSION],
    )

    leased, lease = jobs.lease_next_job(db, node)

    assert leased.id == canonical.id
    assert registration.status == "queued"
    try:
        jobs.authorize_candidate_artifact_upload(
            db,
            node,
            job_id=leased.id,
            lease_secret=lease,
            kind="evidence",
        )
    except ValueError as exc:
        assert "job stage" in str(exc)
    else:
        raise AssertionError("cross-stage artifact kind was accepted")
    db.close()


def test_canonical_validation_uses_no_image_decode_or_replay(
    tmp_path,
    monkeypatch,
):
    Session = _session_factory(tmp_path)
    db = Session()
    source, received, canonical_file = _stage_candidate(db, tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("server pixel processing was invoked")

    monkeypatch.setattr(validation, "_layout", forbidden)
    monkeypatch.setattr(artifacts, "replay_and_verify_artifacts", forbidden)
    monkeypatch.setattr(artifacts, "decode_uploaded_aligned_page", forbidden)
    monkeypatch.setattr(artifacts, "decode_uploaded_source_mask", forbidden)
    monkeypatch.setattr(artifacts.cv2, "imdecode", forbidden)
    monkeypatch.setattr(artifacts.Image, "open", forbidden)

    validated = validation.try_validate_candidate_job(db, received.id)

    assert validated.status == "validated"
    assert validated.server_validation_status == "accepted"
    assert validated.server_artifact_sha256 == canonical_file["sha256"]
    stable = Path(validated.server_artifact_path)
    assert stable.is_file()
    assert not artifacts.candidate_artifact_root(source, validated.id).exists()
    details = json.loads(validated.server_validation_json)
    assert details["server_image_decode_performed"] is False
    assert details["server_source_transform_replayed"] is False
    stable.unlink()
    db.close()


def test_canonical_failure_paths_never_enter_server_fallback(tmp_path, monkeypatch):
    Session = _session_factory(tmp_path)
    db = Session()
    _source_path, received, _canonical_file = _stage_candidate(db, tmp_path)
    monkeypatch.setattr(
        validation,
        "_validate_canonicalization_candidate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    failed = validation.try_validate_candidate_job(db, received.id)

    assert failed.status == "fallback_failed"
    assert failed.status != "server_fallback"
    assert failed.server_validation_status == "rejected"

    deadline_source = tmp_path / "deadline.jpg"
    deadline_source.write_bytes(b"deadline")
    deadline = _enqueue(db, deadline_source, operation_id="deadline")
    deadline.deadline_at = jobs.utcnow() - timedelta(seconds=1)
    db.commit()
    jobs.expire_leases(db)
    db.refresh(deadline)
    assert deadline.status == "fallback_failed"
    assert deadline.status != "server_fallback"

    node = db.query(models_accelerator.AcceleratorNode).one()
    retry_source = tmp_path / "retry.jpg"
    retry_source.write_bytes(b"retry")
    retry = _enqueue(db, retry_source, operation_id="retry")
    first, _first_lease = jobs.lease_next_job(db, node)
    assert first.id == retry.id
    first.lease_expires_at = jobs.utcnow() - timedelta(seconds=1)
    db.commit()
    jobs.expire_leases(db)
    db.refresh(retry)
    assert retry.status == "queued"
    second, _second_lease = jobs.lease_next_job(db, node)
    second.lease_expires_at = jobs.utcnow() - timedelta(seconds=1)
    db.commit()
    jobs.expire_leases(db)
    db.refresh(retry)
    assert retry.status == "fallback_failed"
    assert retry.status != "server_fallback"

    worker_source = tmp_path / "worker-fail.jpg"
    worker_source.write_bytes(b"worker-fail")
    worker_job = _enqueue(db, worker_source, operation_id="worker-fail")
    leased, lease = jobs.lease_next_job(db, node)
    leased.attempt_count = jobs.MAX_ATTEMPTS
    db.commit()
    worker_failed = jobs.fail_job(
        db,
        node,
        job_id=worker_job.id,
        lease_secret=lease,
        error_code="template_transform_invalid",
    )
    assert worker_failed.status == "fallback_failed"
    assert worker_failed.status != "server_fallback"
    db.close()


def test_business_helper_reports_saturated_online_node_as_pending(tmp_path, monkeypatch):
    Session = _session_factory(tmp_path)
    monkeypatch.setattr(business, "SessionLocal", Session)
    monkeypatch.setattr(business, "WAIT_SECONDS", 0.0)
    db = Session()
    _node(db, free_slots=0)
    db.close()
    source = _source(tmp_path)

    handle = asyncio.run(
        business.try_accelerated_template_canonicalization(
            teacher_id=1,
            operation_id="pending-op",
            source_path=str(source),
        )
    )

    assert handle.state == "pending"
    assert handle.job_id
    db = Session()
    stored = db.get(AcceleratorJob, handle.job_id)
    assert stored.stage == CANONICALIZATION_STAGE
    assert stored.status == "queued"
    db.close()


def test_business_helper_returns_ready_validated_canonical(tmp_path, monkeypatch):
    Session = _session_factory(tmp_path)
    monkeypatch.setattr(business, "SessionLocal", Session)
    db = Session()
    source, received, _canonical_file = _stage_candidate(db, tmp_path)
    validated = validation.try_validate_candidate_job(db, received.id)
    stable = Path(validated.server_artifact_path)
    db.close()

    handle = asyncio.run(
        business.try_accelerated_template_canonicalization(
            teacher_id=1,
            operation_id="canonical-op",
            source_path=str(source),
        )
    )

    assert handle.state == "ready"
    assert Path(handle.canonical_path) == stable.resolve()
    assert handle.canonicalization["version"] == "paper-canonical-v1"
    stable.unlink()


def test_adoption_is_streaming_adds_source_path_and_consumes_job(
    tmp_path,
    monkeypatch,
):
    Session = _session_factory(tmp_path)
    monkeypatch.setattr(business, "SessionLocal", Session)
    db = Session()
    source, received, _canonical_file = _stage_candidate(db, tmp_path)
    validated = validation.try_validate_candidate_job(db, received.id)
    stable = Path(validated.server_artifact_path)
    ready = business._validated_canonical_template(validated)
    db.close()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("image decoder was invoked during adoption")

    monkeypatch.setattr(artifacts.cv2, "imdecode", forbidden)
    monkeypatch.setattr(artifacts.Image, "open", forbidden)
    adopted = business.adopt_accelerated_template_canonicalization(
        ready,
        teacher_id=1,
        template_dir=tmp_path / "template-session",
        page_index=0,
    )

    assert Path(adopted.canonical_path).is_file()
    assert adopted.canonicalization["source_image"] == (
        "canonical/page_000/source.jpg"
    )
    assert (tmp_path / "template-session" / adopted.canonicalization["source_image"]).read_bytes() == source.read_bytes()
    assert not stable.exists()
    db = Session()
    stored = db.get(AcceleratorJob, adopted.job_id)
    assert stored.status == "consumed"
    assert stored.server_artifact_path is None
    assert stored.server_artifact_sha256 is None
    db.close()
