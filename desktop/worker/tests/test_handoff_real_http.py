"""Real-socket E2E for the Windows Desktop handoff v2 artifact lane."""
from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from PIL import Image, ImageDraw
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models_accelerator  # noqa: F401
import models_user  # noqa: F401
from database import Base, get_db
from models_accelerator import AcceleratorJob
from models_user import User
from routers import accelerator
from routers.auth import _require_user
from services import accelerator_jobs as jobs
from services.template_anchor_crops import build_anchor_crop_plan
from mathagent_worker import handoff as desktop_handoff
from mathagent_worker.gpu_backend import (
    CPU_BACKEND,
    GPU_BACKEND,
    GPU_BACKEND_VERSION,
    GpuSelfTestResult,
)

ROOT = Path(__file__).resolve().parents[3]


def _image(path: Path, *, student: bool = False) -> None:
    image = Image.new("RGB", (800, 1000), "white")
    draw = ImageDraw.Draw(image)
    for y in range(80, 920, 80):
        draw.line((60, y, 740, y), fill="black", width=3)
    for x in range(80, 760, 80):
        draw.rectangle((x, 40, x + 25, 65), outline="black", width=2)
    generator = random.Random(20260801)
    for index in range(140):
        x = generator.randint(45, 745)
        y = generator.randint(40, 940)
        radius = 2 + index % 5
        draw.ellipse(
            (x - radius, y - radius, x + radius, y + radius),
            outline="black",
        )
        if index % 9 == 0:
            draw.text((x + 5, y - 5), f"Q{index}", fill="black")
    if student:
        draw.line((120, 260, 500, 320), fill="black", width=8)
    image.save(path)


def _cpu_only() -> GpuSelfTestResult:
    return GpuSelfTestResult(
        available=False,
        enabled=False,
        backend=CPU_BACKEND,
        backend_version=GPU_BACKEND_VERSION,
        device_name=None,
        driver_version=None,
        gpu_time_ms=None,
        cpu_time_ms=None,
        speedup=None,
        projection_max_px=None,
        fallback_reason="e2e_cpu_forced",
    )


def _session_factory(tmp_path: Path, name: str):
    engine = create_engine(
        f"sqlite:///{tmp_path / name}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _app(Session):
    def override_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()
    app.include_router(accelerator.router, prefix="/api")
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[_require_user] = lambda: User(
        id=1,
        name="管理员",
        role="admin",
        status="active",
    )
    return app


def _start_server(app):
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started and server.servers:
            break
        time.sleep(0.05)
    assert server.started
    port = next(iter(server.servers)).sockets[0].getsockname()[1]
    return server, thread, port


def _register_job(Session, *, source: Path, template: Path, paper_id: str, bbox):
    db = Session()
    db.add(User(id=1, name="管理员", role="admin", status="active"))
    db.commit()
    job = jobs.enqueue_job(
        db,
        teacher_id=1,
        paper_id=paper_id,
        source_path=str(source),
        template_id=f"template-{paper_id}",
        template_path=str(template),
        layout_snapshot={
            "page_type_id": "page_1",
            "answer_regions": [{
                "question_number": "1",
                "question_type": "solve",
                "answer_bbox": bbox,
            }],
        },
    )
    node, token = jobs.register_node(
        db,
        owner_user_id=1,
        name=f"node-{paper_id}",
    )
    values = job.id, node.id, token
    db.close()
    return values


def _lease(Session, port: int, node_id: str, token: str):
    config = desktop_handoff.HandoffConfig(
        server=f"http://127.0.0.1:{port}",
        node_id=node_id,
        token=token,
        poll_seconds=0.25,
    )
    transport = desktop_handoff.HttpTransport(config)
    transport.json_request(
        "/api/accelerator/nodes/heartbeat",
        {
            "capabilities": {
                "backend": "windows_nvidia_gpu_or_cpu",
                "stages": [
                    "template_registration",
                    "template_canonicalization",
                    "template_ocr_evidence",
                ],
                "artifact_contracts": [
                    "client-evidence-v1",
                    "client-canonical-template-v1",
                    "template-ocr-evidence-v2",
                ],
                "lease_slots": 6,
                "cpu_compute_slots": 3,
                "gpu_contexts": 1,
            },
            "algorithm_version": jobs.ALGORITHM_VERSION,
            "free_slots": 6,
            "queue_depth": 0,
        },
    )
    leased = transport.json_request("/api/accelerator/jobs/lease", {})["job"]
    assert leased is not None
    return config, transport, leased


def test_desktop_canonicalization_round_trip_is_source_only_and_decode_free(
    tmp_path,
    monkeypatch,
):
    Session = _session_factory(tmp_path, "desktop-canonical-e2e.db")
    source = tmp_path / "template-photo.png"
    _image(source)
    db = Session()
    db.add(User(id=1, name="teacher", role="teacher", status="active"))
    db.commit()
    job = jobs.enqueue_job(
        db,
        teacher_id=1,
        paper_id="canonical-real-http",
        source_path=str(source),
        template_id="canonical-real-http",
        template_path=str(source),
        layout_snapshot={"operation_id": "canonical-real-http"},
        stage="template_canonicalization",
    )
    node, token = jobs.register_node(
        db,
        owner_user_id=1,
        name="canonical-node",
    )
    job_id, node_id = job.id, node.id
    db.close()

    server, thread, port = _start_server(_app(Session))
    config, transport, leased = _lease(Session, port, node_id, token)
    assert leased["stage"] == "template_canonicalization"
    assert "template_url" not in leased
    runner = desktop_handoff.DesktopHandoffRunner(config, transport)
    stable_path = None
    try:
        runner._process_job(leased)
        snapshot = runner.snapshot()
        assert snapshot.completed == 1
        assert snapshot.failed == 0
        verify = Session()
        stored = verify.get(AcceleratorJob, job_id)
        assert stored.status == "validated"
        assert stored.server_validation_status == "accepted"
        stable_path = Path(stored.server_artifact_path)
        assert stable_path.is_file()
        candidate = json.loads(stored.candidate_json)
        assert set(candidate) == {
            "status",
            "artifact_manifest",
            "canonicalization",
        }
        assert candidate["artifact_manifest"]["contract_version"] == (
            "client-canonical-template-v1"
        )
        validation = json.loads(stored.server_validation_json)
        assert validation["validator"] == "client_canonical_template_v1"
        assert validation["server_image_decode_performed"] is False
        assert validation["server_source_transform_replayed"] is False
        assert not (tmp_path / ".accelerator-candidates" / job_id).exists()
        verify.close()
    finally:
        if stable_path is not None:
            stable_path.unlink(missing_ok=True)
        server.should_exit = True
        thread.join(timeout=5)


def test_desktop_handoff_uploads_final_evidence_over_real_socket(
    tmp_path,
    monkeypatch,
):
    Session = _session_factory(tmp_path, "desktop-e2e.db")
    source = tmp_path / "source.png"
    template = tmp_path / "template.png"
    _image(template)
    _image(source, student=True)
    job_id, node_id, token = _register_job(
        Session,
        source=source,
        template=template,
        paper_id="desktop-real-http",
        bbox=[100, 180, 800, 420],
    )
    server, thread, port = _start_server(_app(Session))
    config, transport, leased = _lease(Session, port, node_id, token)
    real_process_candidate = desktop_handoff.process_candidate

    def cpu_process(source_path, template_path, payload, **kwargs):
        return real_process_candidate(
            source_path,
            template_path,
            payload,
            gpu_self_test=_cpu_only(),
            **kwargs,
        )

    monkeypatch.setattr(desktop_handoff, "process_candidate", cpu_process)
    runner = desktop_handoff.DesktopHandoffRunner(config, transport)
    evidence_path = None
    try:
        runner._process_job(leased)
        snapshot = runner.snapshot()
        assert snapshot.completed == 1
        assert snapshot.failed == 0
        assert snapshot.backend == CPU_BACKEND
        verify = Session()
        stored = verify.get(AcceleratorJob, job_id)
        assert stored.status == "validated"
        assert stored.server_validation_status == "accepted"
        evidence_path = Path(stored.server_artifact_path)
        assert evidence_path.is_file()
        validation = json.loads(stored.server_validation_json)
        assert validation["validator"] == "trusted_client_evidence_v1"
        assert validation["server_feature_extraction_performed"] is False
        assert validation["server_source_transform_replayed"] is False
        assert validation["server_evidence_rendered"] is False
        candidate = json.loads(stored.candidate_json)
        assert candidate["artifact_manifest"]["contract_version"] == (
            "client-evidence-v1"
        )
        assert candidate["question_numbers"] == ["1"]
        assert not (tmp_path / ".accelerator-candidates" / job_id).exists()
        verify.close()
    finally:
        if evidence_path is not None:
            evidence_path.unlink(missing_ok=True)
        server.should_exit = True
        thread.join(timeout=5)


def test_desktop_template_ocr_evidence_round_trip_is_worker_rendered(
    tmp_path,
):
    Session = _session_factory(tmp_path, "desktop-template-ocr-evidence-e2e.db")
    canonical = tmp_path / "canonical.png"
    _image(canonical)
    canonical_sha256 = hashlib.sha256(canonical.read_bytes()).hexdigest()
    crop_plan = build_anchor_crop_plan(
        {
            "column_layout": {
                "column_count": 2,
                "columns": [
                    {"column_index": 1, "x1_1000": 0, "x2_1000": 500},
                    {"column_index": 2, "x1_1000": 500, "x2_1000": 1000},
                ],
            }
        },
        artifact_sha256=canonical_sha256,
    )
    db = Session()
    db.add(User(id=1, name="teacher", role="teacher", status="active"))
    db.commit()
    job = jobs.enqueue_job(
        db,
        teacher_id=1,
        paper_id="template-ocr-evidence-real-http",
        source_path=str(canonical),
        template_id="template-ocr-evidence-real-http",
        template_path=str(canonical),
        layout_snapshot={"question_crop_plan": crop_plan},
        stage="template_ocr_evidence",
    )
    node, token = jobs.register_node(
        db,
        owner_user_id=1,
        name="template-ocr-evidence-node",
    )
    job_id, node_id = job.id, node.id
    db.close()

    server, thread, port = _start_server(_app(Session))
    config, transport, leased = _lease(Session, port, node_id, token)
    assert leased["stage"] == "template_ocr_evidence"
    assert leased["source_url"] != leased["template_url"]
    runner = desktop_handoff.DesktopHandoffRunner(config, transport)
    stable_path = None
    try:
        downloaded_urls = []
        real_download = transport.download

        def tracked_download(path, lease, target, expected_sha):
            downloaded_urls.append(path)
            return real_download(path, lease, target, expected_sha)

        transport.download = tracked_download
        runner._process_job(leased)
        snapshot = runner.snapshot()
        assert snapshot.completed == 1
        assert snapshot.failed == 0
        assert snapshot.backend == "windows_cpu_pillow"
        assert downloaded_urls == [leased["source_url"]]
        verify = Session()
        stored = verify.get(AcceleratorJob, job_id)
        assert stored.status == "validated"
        assert stored.server_validation_status == "accepted"
        stable_path = Path(stored.server_artifact_path)
        assert stable_path.is_file()
        candidate = json.loads(stored.candidate_json)
        assert candidate["artifact_manifest"]["contract_version"] == (
            "template-ocr-evidence-v2"
        )
        assert candidate["input_identity"] == {
            "canonical_sha256": canonical_sha256,
            "layout_snapshot_sha256": stored.layout_snapshot_sha256,
        }
        validation = json.loads(stored.server_validation_json)
        assert validation["validator"] == "template_ocr_evidence_v2"
        assert validation["server_image_decode_performed"] is False
        assert validation["server_evidence_rendered"] is False
        assert not (tmp_path / ".accelerator-candidates" / job_id).exists()
        verify.close()
    finally:
        if stable_path is not None:
            stable_path.unlink(missing_ok=True)
        server.should_exit = True
        thread.join(timeout=5)


@pytest.mark.skipif(
    os.environ.get("MATHAGENT_RUN_REAL_GPU") != "1",
    reason="requires the qualified Windows VulkanSift runtime",
)
def test_desktop_resident_gpu_v2_real_http_round_trip(tmp_path):
    Session = _session_factory(tmp_path, "desktop-gpu-e2e.db")
    source = ROOT / ".cache" / "VulkanSift" / "capacity-student.jpg"
    template = ROOT / ".cache" / "VulkanSift" / "capacity-template.jpg"
    assert source.is_file() and template.is_file()
    job_id, node_id, token = _register_job(
        Session,
        source=source,
        template=template,
        paper_id="desktop-real-gpu",
        bbox=[300, 300, 700, 700],
    )
    server, thread, port = _start_server(_app(Session))
    config, transport, leased = _lease(Session, port, node_id, token)
    runner = desktop_handoff.DesktopHandoffRunner(config, transport)
    evidence_path = None
    try:
        runner._process_job(leased)
        snapshot = runner.snapshot()
        assert snapshot.completed == 1
        assert snapshot.failed == 0
        assert snapshot.backend == GPU_BACKEND
        verify = Session()
        stored = verify.get(AcceleratorJob, job_id)
        assert stored.status == "validated"
        assert stored.server_validation_status == "accepted"
        result = json.loads(stored.candidate_json)
        assert result["worker_metrics"]["source_gpu_runtime"] == "resident"
        validation = json.loads(stored.server_validation_json)
        assert validation["validator"] == "trusted_client_evidence_v1"
        assert validation["server_feature_extraction_performed"] is False
        assert validation["server_source_transform_replayed"] is False
        assert validation["server_evidence_rendered"] is False
        evidence_path = Path(stored.server_artifact_path)
        assert evidence_path.is_file()
        assert not (
            source.parent / ".accelerator-candidates" / job_id
        ).exists()
        verify.close()
    finally:
        if evidence_path is not None:
            evidence_path.unlink(missing_ok=True)
        server.should_exit = True
        thread.join(timeout=5)


@pytest.mark.skipif(
    os.environ.get("MATHAGENT_RUN_PACKAGED_GPU") != "1",
    reason="requires a freshly built packaged Windows sidecar",
)
def test_packaged_sidecar_resident_gpu_real_http_round_trip(tmp_path):
    binary = (
        ROOT
        / "desktop"
        / "src-tauri"
        / "binaries"
        / "mathagent-sidecar-x86_64-pc-windows-msvc.exe"
    )
    assert binary.is_file()
    Session = _session_factory(tmp_path, "packaged-gpu-e2e.db")
    source = ROOT / ".cache" / "VulkanSift" / "capacity-student.jpg"
    template = ROOT / ".cache" / "VulkanSift" / "capacity-template.jpg"
    if not source.is_file() or not template.is_file():
        source = tmp_path / "capacity-student.png"
        template = tmp_path / "capacity-template.png"
        _image(template)
        _image(source, student=True)
    job_id, node_id, token = _register_job(
        Session,
        source=source,
        template=template,
        paper_id="packaged-real-gpu",
        bbox=[300, 300, 700, 700],
    )
    server, thread, port = _start_server(_app(Session))
    environment = dict(os.environ)
    environment.update({
        "MATHAGENT_ACCELERATOR_SERVER": f"http://127.0.0.1:{port}",
        "MATHAGENT_ACCELERATOR_NODE_ID": node_id,
        "MATHAGENT_ACCELERATOR_TOKEN": token,
        "MATHAGENT_ACCELERATOR_ALLOW_HTTP": "1",
        "MATHAGENT_ACCELERATOR_POLL_SECONDS": "0.25",
    })
    process = subprocess.Popen(
        [str(binary), "--serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        cwd=str(tmp_path),
        env=environment,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    evidence_path = None
    try:
        assert process.stdout is not None and process.stdin is not None
        health = json.loads(process.stdout.readline())
        assert health["service"] == "mathagent-desktop-sidecar"
        started = time.monotonic()
        deadline = started + 70.0
        stored_status = None
        diagnostic = None
        final_detail = None
        while time.monotonic() < deadline:
            verify = Session()
            stored = verify.get(AcceleratorJob, job_id)
            stored_status = stored.status
            if stored.status == "validated":
                result = json.loads(stored.candidate_json)
                assert result["worker_metrics"]["source_gpu_runtime"] == "resident"
                evidence_path = Path(stored.server_artifact_path)
                assert evidence_path.is_file()
                verify.close()
                break
            if stored.status in {"server_fallback", "consumed", "superseded"}:
                final_detail = {
                    "status": stored.status,
                    "error_code": stored.error_code,
                    "server_validation_status": stored.server_validation_status,
                    "server_validation_json": stored.server_validation_json,
                }
                verify.close()
                break
            verify.close()
            if diagnostic is None and time.monotonic() - started >= 20.0:
                process.stdin.write(
                    '{"command":"get_worker_status","requestId":"packaged-diag"}\n'
                )
                process.stdin.flush()
                diagnostic = json.loads(process.stdout.readline())
            time.sleep(0.1)
        assert stored_status == "validated", {
            "diagnostic": diagnostic,
            "final": final_detail,
        }
        process.stdin.write('{"command":"shutdown"}\n')
        process.stdin.flush()
        stopping = json.loads(process.stdout.readline())
        assert stopping["status"] == "stopping"
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if evidence_path is not None:
            evidence_path.unlink(missing_ok=True)
        server.should_exit = True
        thread.join(timeout=5)
