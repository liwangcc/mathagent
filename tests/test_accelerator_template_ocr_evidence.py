"""Protocol tests for offloaded template OCR montage rendering."""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
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
from routers import accelerator as accelerator_router
from services import accelerator_artifacts as artifacts
from services import accelerator_jobs as jobs
from services import accelerator_validation as validation
from services.template_anchor_crops import build_anchor_crop_plan


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'template-ocr-accelerator.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    db.add_all([
        User(id=1, name="teacher-1", role="teacher", status="active"),
        User(id=2, name="teacher-2", role="teacher", status="active"),
    ])
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


def _rgb_png(path: Path, size: tuple[int, int] = (64, 64)) -> Path:
    Image.new("RGB", size, "white").save(path, format="PNG")
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _layout(canonical_sha256: str) -> dict:
    return {
        "question_crop_plan": build_anchor_crop_plan(
            {
                "column_layout": {
                    "column_count": 3,
                    "columns": [
                        {"column_index": 1, "x1_1000": 0, "x2_1000": 330},
                        {"column_index": 2, "x1_1000": 330, "x2_1000": 665},
                        {"column_index": 3, "x1_1000": 665, "x2_1000": 1000},
                    ],
                }
            },
            artifact_sha256=canonical_sha256,
            expected_question_numbers=["1", "2"],
        ),
        "operation_id": "template-cut-op",
        "page_index": 0,
    }


def _enqueue(
    db,
    canonical: Path,
    *,
    teacher_id: int = 1,
    paper_id: str = "template-ocr-page-1",
    lane: str = "web_gpu",
):
    return jobs.enqueue_job(
        db,
        teacher_id=teacher_id,
        paper_id=paper_id,
        source_path=str(canonical),
        template_id=paper_id,
        template_path=str(canonical),
        layout_snapshot=_layout(_sha256(canonical)),
        stage=jobs.TEMPLATE_OCR_EVIDENCE_STAGE,
        lane=lane,
    )


def _ready_node(
    db,
    *,
    owner_user_id: int,
    name: str,
    lane: str = "web_gpu",
    access_scope: str = "tenant",
    free_slots: int = 1,
):
    node, token = jobs.register_node(
        db,
        owner_user_id=owner_user_id,
        name=name,
        access_scope=access_scope,
        lane=lane,
        device_id=(f"desktop-{owner_user_id}" if lane == "teacher_desktop" else None),
    )
    node = jobs.authenticate_node(db, node.id, token)
    jobs.heartbeat(
        db,
        node,
        capabilities={
            "stages": [jobs.TEMPLATE_OCR_EVIDENCE_STAGE],
            "artifact_contracts": [
                artifacts.TEMPLATE_OCR_EVIDENCE_CONTRACT_VERSION
            ],
            "lease_slots": free_slots,
        },
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=free_slots,
    )
    return node


def _result(job: AcceleratorJob, artifact_file: dict) -> dict:
    return {
        "status": "succeeded",
        "artifact_manifest": {
            "contract_version": artifacts.TEMPLATE_OCR_EVIDENCE_CONTRACT_VERSION,
            artifacts.TEMPLATE_OCR_EVIDENCE_KIND: artifact_file,
        },
        "input_identity": {
            "canonical_sha256": job.source_sha256,
            "layout_snapshot_sha256": job.layout_snapshot_sha256,
        },
    }


def test_template_ocr_png_and_manifest_are_validated_without_decode(
    tmp_path,
    monkeypatch,
):
    montage = _rgb_png(tmp_path / "montage.png", (120, 80))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("image decoder was invoked")

    monkeypatch.setattr(artifacts.cv2, "imdecode", forbidden)
    monkeypatch.setattr(artifacts.Image, "open", forbidden)
    file_manifest = artifacts.validate_uploaded_artifact_file(
        montage,
        artifacts.TEMPLATE_OCR_EVIDENCE_KIND,
    )
    manifest = artifacts.validate_artifact_manifest({
        "contract_version": artifacts.TEMPLATE_OCR_EVIDENCE_CONTRACT_VERSION,
        artifacts.TEMPLATE_OCR_EVIDENCE_KIND: file_manifest,
    })

    assert manifest[artifacts.TEMPLATE_OCR_EVIDENCE_KIND]["width"] == 120
    assert manifest[artifacts.TEMPLATE_OCR_EVIDENCE_KIND]["height"] == 80

    damaged = bytearray(montage.read_bytes())
    damaged[-1] ^= 0x01
    montage.write_bytes(damaged)
    with pytest.raises(ValueError, match="PNG checksum"):
        artifacts.validate_uploaded_artifact_file(
            montage,
            artifacts.TEMPLATE_OCR_EVIDENCE_KIND,
        )


def test_enqueue_and_result_bind_canonical_and_layout_identity(
    db,
    tmp_path,
    monkeypatch,
):
    canonical = _rgb_png(tmp_path / "canonical.png")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("image decoder was invoked")

    monkeypatch.setattr(artifacts.cv2, "imdecode", forbidden)
    monkeypatch.setattr(artifacts.Image, "open", forbidden)
    job = _enqueue(db, canonical)
    layout = json.loads(job.layout_snapshot_json)
    artifact_file = {
        "sha256": "a" * 64,
        "byte_size": 100,
        "width": 120,
        "height": 80,
        "channels": 3,
        "format": "png",
    }
    result = _result(job, artifact_file)
    normalized = jobs._validate_candidate_payload(
        result,
        stage=jobs.TEMPLATE_OCR_EVIDENCE_STAGE,
        source_sha256=job.source_sha256,
        layout_snapshot_sha256=job.layout_snapshot_sha256,
    )

    assert Path(job.source_path) == Path(job.template_path)
    assert job.source_sha256 == job.template_sha256 == _sha256(canonical)
    assert layout["question_crop_plan"]["source_artifact_sha256"] == job.source_sha256
    assert normalized["input_identity"] == {
        "canonical_sha256": job.source_sha256,
        "layout_snapshot_sha256": job.layout_snapshot_sha256,
    }

    result["input_identity"]["layout_snapshot_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="input identity"):
        jobs._validate_candidate_payload(
            result,
            stage=jobs.TEMPLATE_OCR_EVIDENCE_STAGE,
            source_sha256=job.source_sha256,
            layout_snapshot_sha256=job.layout_snapshot_sha256,
        )


def test_lease_payload_exposes_canonical_crop_plan_and_input_identity(db, tmp_path):
    canonical = _rgb_png(tmp_path / "canonical.png")
    job = _enqueue(db, canonical)
    node = _ready_node(
        db,
        owner_user_id=1,
        name="teacher desktop",
        lane="teacher_desktop",
    )

    payload = accelerator_router.api_lease(db=db, node=node)["job"]

    assert payload["job_id"] == job.id
    assert payload["stage"] == jobs.TEMPLATE_OCR_EVIDENCE_STAGE
    assert payload["source_url"] == payload["canonical_url"]
    assert payload["template_url"].endswith("/assets/template")
    assert payload["layout_snapshot"]["question_crop_plan"][
        "source_artifact_sha256"
    ] == job.source_sha256
    assert payload["input_identity"] == {
        "canonical_sha256": job.source_sha256,
        "layout_snapshot_sha256": job.layout_snapshot_sha256,
    }
    assert jobs.allowed_artifact_kinds_for_stage(job.stage) == frozenset({
        artifacts.TEMPLATE_OCR_EVIDENCE_KIND
    })


def test_v1_template_ocr_worker_cannot_lease_v2_job(db, tmp_path):
    canonical = _rgb_png(tmp_path / "canonical.png")
    job = _enqueue(db, canonical)
    node, token = jobs.register_node(
        db,
        owner_user_id=1,
        name="stale template OCR worker",
        lane="teacher_desktop",
        device_id="stale-template-ocr-worker",
    )
    node = jobs.authenticate_node(db, node.id, token)
    jobs.heartbeat(
        db,
        node,
        capabilities={
            "stages": [jobs.TEMPLATE_OCR_EVIDENCE_STAGE],
            "artifact_contracts": ["template-ocr-evidence-v1"],
            "lease_slots": 1,
        },
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )

    assert jobs.lease_next_job(db, node) is None
    db.refresh(job)
    assert job.status == "queued"


def test_http_upload_whitelist_accepts_template_ocr_artifact(db, tmp_path):
    canonical = _rgb_png(tmp_path / "canonical.png")
    job = _enqueue(db, canonical)
    node = _ready_node(
        db,
        owner_user_id=1,
        name="teacher desktop",
        lane="teacher_desktop",
    )
    leased, lease = jobs.lease_next_job(db, node)
    raw = _rgb_png(tmp_path / "upload.png", (120, 80)).read_bytes()

    class Request:
        async def stream(self):
            yield raw[:17]
            yield raw[17:]

    response = asyncio.run(
        accelerator_router.api_upload_artifact(
            job_id=leased.id,
            kind=artifacts.TEMPLATE_OCR_EVIDENCE_KIND,
            request=Request(),
            lease_id=lease,
            content_length=len(raw),
            content_type="image/png",
            db=db,
            node=node,
        )
    )

    assert response["kind"] == artifacts.TEMPLATE_OCR_EVIDENCE_KIND
    assert response["sha256"] == hashlib.sha256(raw).hexdigest()
    assert response["width"] == 120
    assert response["height"] == 80
    assert artifacts.candidate_artifact_path(
        job.source_path,
        job.id,
        artifacts.TEMPLATE_OCR_EVIDENCE_KIND,
    ).read_bytes() == raw


def test_template_ocr_validation_publishes_exact_png_without_decode(
    db,
    tmp_path,
    monkeypatch,
):
    canonical = _rgb_png(tmp_path / "canonical.png")
    montage = _rgb_png(tmp_path / "montage.png", (120, 80))
    expected_bytes = montage.read_bytes()
    artifact_file = artifacts.validate_uploaded_artifact_file(
        montage,
        artifacts.TEMPLATE_OCR_EVIDENCE_KIND,
    )
    job = _enqueue(db, canonical)
    node = _ready_node(
        db,
        owner_user_id=1,
        name="teacher desktop",
        lane="teacher_desktop",
    )
    leased, lease = jobs.lease_next_job(db, node)
    target = artifacts.candidate_artifact_path(
        job.source_path,
        job.id,
        artifacts.TEMPLATE_OCR_EVIDENCE_KIND,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(expected_bytes)
    received = jobs.submit_result(
        db,
        node,
        job_id=leased.id,
        lease_secret=lease,
        result=_result(job, artifact_file),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("server pixel processing was invoked")

    monkeypatch.setattr(artifacts.cv2, "imdecode", forbidden)
    monkeypatch.setattr(artifacts.Image, "open", forbidden)
    validated = validation.validate_candidate_job(db, received.id)

    assert validated.status == "validated"
    assert validated.server_validation_status == "accepted"
    assert validated.server_artifact_sha256 == artifact_file["sha256"]
    stable = Path(validated.server_artifact_path)
    assert stable.read_bytes() == expected_bytes
    details = json.loads(validated.server_validation_json)
    assert details["canonical_hash_matches"] is True
    assert details["layout_hash_matches"] is True
    assert details["worker_input_identity_matches"] is True
    assert details["server_image_decode_performed"] is False
    assert details["server_evidence_rendered"] is False
    assert not artifacts.candidate_artifact_root(job.source_path, job.id).exists()
    stable.unlink()


def test_template_ocr_failure_never_enters_server_cpu_fallback(db, tmp_path):
    canonical = _rgb_png(tmp_path / "canonical.png")
    montage = _rgb_png(tmp_path / "montage.png", (120, 80))
    artifact_file = artifacts.validate_uploaded_artifact_file(
        montage,
        artifacts.TEMPLATE_OCR_EVIDENCE_KIND,
    )
    job = _enqueue(db, canonical)
    node = _ready_node(
        db,
        owner_user_id=1,
        name="teacher desktop",
        lane="teacher_desktop",
    )
    leased, lease = jobs.lease_next_job(db, node)
    target = artifacts.candidate_artifact_path(
        job.source_path,
        job.id,
        artifacts.TEMPLATE_OCR_EVIDENCE_KIND,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(montage.read_bytes())
    jobs.submit_result(
        db,
        node,
        job_id=leased.id,
        lease_secret=lease,
        result=_result(job, artifact_file),
    )
    target.write_bytes(b"changed")

    rejected = validation.validate_candidate_job(db, job.id)

    assert rejected.status == "fallback_failed"
    assert rejected.status != "server_fallback"
    assert rejected.server_validation_status == "rejected"

    deadline = _enqueue(
        db,
        canonical,
        paper_id="template-ocr-deadline",
    )
    deadline.deadline_at = jobs.utcnow() - timedelta(seconds=1)
    db.commit()
    jobs.expire_leases(db)
    db.refresh(deadline)
    assert deadline.status == "fallback_failed"


def test_tenant_desktop_only_claims_own_web_gpu_job(db, tmp_path):
    canonical_one = _rgb_png(tmp_path / "canonical-one.png")
    canonical_two = _rgb_png(tmp_path / "canonical-two.png")
    other = _enqueue(
        db,
        canonical_two,
        teacher_id=2,
        paper_id="teacher-2-job",
    )
    own = _enqueue(
        db,
        canonical_one,
        teacher_id=1,
        paper_id="teacher-1-job",
    )
    node = _ready_node(
        db,
        owner_user_id=1,
        name="teacher-1 desktop",
        lane="teacher_desktop",
    )

    leased, _lease = jobs.lease_next_job(db, node)

    assert leased.id == own.id
    assert leased.teacher_id == 1
    db.refresh(other)
    assert other.status == "queued"


def test_platform_and_desktop_race_creates_only_one_cross_lane_lease(
    session_factory,
    tmp_path,
):
    seed = session_factory()
    canonical = _rgb_png(tmp_path / "canonical.png")
    job = _enqueue(seed, canonical)
    platform = _ready_node(
        seed,
        owner_user_id=1,
        name="platform pool",
        access_scope="platform",
    )
    desktop = _ready_node(
        seed,
        owner_user_id=1,
        name="teacher desktop",
        lane="teacher_desktop",
    )
    job_id = job.id
    node_ids = [platform.id, desktop.id]
    seed.close()

    barrier = threading.Barrier(2)
    results: list[str | None] = []
    errors: list[Exception] = []

    def claim(node_id: str):
        session = session_factory()
        try:
            node = session.get(models_accelerator.AcceleratorNode, node_id)
            barrier.wait(timeout=5)
            leased = jobs.lease_next_job(session, node)
            results.append(leased[0].id if leased else None)
        except Exception as exc:  # pragma: no cover - assertion reports it
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=claim, args=(node_id,)) for node_id in node_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert results.count(job_id) == 1
    assert results.count(None) == 1
    verify = session_factory()
    stored = verify.get(AcceleratorJob, job_id)
    assert stored.status == "leased"
    assert stored.assigned_node_id in node_ids
    assert verify.query(AcceleratorJob).filter_by(status="leased").count() == 1
    verify.close()


def test_platform_node_may_claim_another_teachers_web_gpu_job(db, tmp_path):
    canonical = _rgb_png(tmp_path / "canonical-platform.png")
    job = _enqueue(
        db,
        canonical,
        teacher_id=2,
        paper_id="teacher-2-platform-job",
    )
    platform = _ready_node(
        db,
        owner_user_id=1,
        name="platform pool",
        access_scope="platform",
    )

    leased, _lease = jobs.lease_next_job(db, platform)

    assert leased.id == job.id
    assert leased.teacher_id == 2
