"""Real-socket proof that ordinary grading work uses the teacher desktop lane."""
from __future__ import annotations

import json
import hashlib
import shutil
import threading
import time
from pathlib import Path

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
from services import accelerator_business, accelerator_jobs
from services.template_anchor_crops import build_anchor_crop_plan
from worker import accelerator_client


def _image(path: Path, *, student: bool = False) -> None:
    image = Image.new("RGB", (800, 1000), "white")
    draw = ImageDraw.Draw(image)
    for y in range(80, 920, 80):
        draw.line((60, y, 740, y), fill="black", width=3)
    for x in range(80, 760, 80):
        draw.rectangle((x, 40, x + 25, 65), outline="black", width=2)
    draw.text((90, 110), "MATH AGENT TEMPLATE 2026", fill="black")
    if student:
        draw.line((120, 260, 500, 320), fill="black", width=8)
    image.save(path)


def test_ordinary_grading_job_round_trips_through_teacher_desktop_lane(
    tmp_path,
    monkeypatch,
):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'teacher-desktop-business.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    teacher = User(id=1, name="教师", role="teacher", status="active")
    db.add(teacher)
    db.commit()
    node, token = accelerator_jobs.register_node(
        db,
        owner_user_id=teacher.id,
        name="teacher desktop real HTTP",
        lane="teacher_desktop",
        device_id="teacher-desktop-real-http",
        token_ttl_days=90,
    )
    accelerator_jobs.heartbeat(
        db,
        node,
        capabilities={
            "backend": "opencv_cpu_reference",
            "stages": [accelerator_jobs.STUDENT_MULTI_EVIDENCE_STAGE],
            "artifact_contracts": [
                    "client-evidence-multi-v3"
            ],
            "lease_slots": 1,
        },
        algorithm_version=accelerator_jobs.ALGORITHM_VERSION,
        free_slots=1,
    )
    node_id = node.id
    db.close()

    template_dir = tmp_path / "template"
    template_dir.mkdir()
    template = template_dir / "page-1.png"
    source = tmp_path / "source.png"
    _image(template)
    _image(source, student=True)
    crop_plan = build_anchor_crop_plan(
        {
            "column_layout": {
                "column_count": 1,
                "columns": [
                    {"column_index": 1, "x1_1000": 0, "x2_1000": 1000}
                ],
            }
        },
        artifact_sha256=hashlib.sha256(template.read_bytes()).hexdigest(),
        expected_question_numbers=["1"],
    )
    monkeypatch.setattr(accelerator_business, "SessionLocal", Session)
    enqueued = accelerator_business._enqueue_template_pages(
        teacher_id=1,
        batch_id="batch-teacher-desktop",
        paper_id="paper-teacher-desktop",
        source_path=str(source),
        template_id="template-teacher-desktop",
        artifacts={
            "page_1": {
                "template_image": template.name,
                "answer_regions": [{
                    "question_number": "1",
                    "question_type": "solve",
                    "answer_bbox": [100, 180, 450, 420],
                }],
                "blank_detection_enabled": True,
                "question_crop_plan": crop_plan,
            }
        },
        template_dir=str(template_dir),
    )
    assert enqueued.node_available is True
    assert enqueued.lane == "teacher_desktop"
    job_ids = enqueued.job_ids
    assert len(job_ids) == 1
    verify = Session()
    queued = verify.get(AcceleratorJob, job_ids[0])
    assert queued.lane == "teacher_desktop"
    assert queued.status == "queued"
    verify.close()

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
        name="教师",
        role="teacher",
        status="active",
    )
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
    monkeypatch.setenv("MATHAGENT_ACCELERATOR_NODE_ID", node_id)
    monkeypatch.setenv("MATHAGENT_ACCELERATOR_TOKEN", token)
    args = type("Args", (), {
        "server": f"http://127.0.0.1:{port}",
        "algorithm_version": accelerator_jobs.ALGORITHM_VERSION,
        "backend": "opencv_cpu_reference",
        "poll_seconds": 0.01,
        "once": True,
    })()
    try:
        assert accelerator_client.run(args) == 0
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    verify = Session()
    stored = verify.get(AcceleratorJob, job_ids[0])
    assert stored.lane == "teacher_desktop"
    assert stored.assigned_node_id == node_id
    assert stored.lease_id_hash is not None
    assert stored.lease_expires_at is None
    assert stored.status == "validated"
    assert stored.server_validation_status == "accepted"
    assert stored.stage == accelerator_jobs.STUDENT_MULTI_EVIDENCE_STAGE
    assert json.loads(stored.candidate_json)["worker_metrics"]["backend"] == "opencv_cpu_reference"
    evidence = accelerator_business._validated_evidence(stored)
    assert evidence is not None
    assert evidence.diagnostics["accelerator"]["server_validated"] is True
    shutil.rmtree(Path(stored.server_artifact_path).parent)
    verify.close()
