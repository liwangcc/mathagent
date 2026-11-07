import asyncio
import copy
import io
import hashlib
import json
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw
from fastapi import FastAPI, UploadFile
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models  # noqa: F401
import models_rate_limit  # noqa: F401
import models_user  # noqa: F401
from database import Base, get_db
from models import GradingCorrection, GradingResult, Student
from models_rate_limit import TeacherClass
from models_user import User
from routers.auth import _require_user
from services.answer_sheet_session import AnswerSheetSession, answer_sheet_operation_lock
from services.template_evidence import canonicalize_template_image
from routers import answer_sheet_availability
from routers import answer_sheet_common
from routers import answer_sheet_assets
from routers import answer_sheet_sessions
from routers import answer_sheet_source
from routers import answer_sheet_layout
from routers import answer_sheet_student_pages
from routers import answer_sheet_student_upload
from routers import answer_sheet_recognition
from routers import answer_sheet_recognize_page
from routers import answer_sheet_student_files
from routers import answer_sheet_grading
from routers import answer_sheet_layout_freeze


def _question(number="1", qtype="choice"):
    return {
        "question_no": number,
        "question_text": f"第{number}题题干 A.1 B.2 C.3 D.4",
        "question_type": qtype,
        "choice_mode": "single" if qtype == "choice" else "unknown",
        "standard_answer": "A" if qtype == "choice" else "1",
        "max_score": 5,
    }


def _png_bytes():
    stream = io.BytesIO()
    image = Image.new("RGB", (1000, 1400), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((35, 35, 964, 1364), outline="black", width=4)
    draw.text((80, 70), "MATH ANSWER SHEET", fill="black")
    for row in range(10):
        top = 170 + row * 105
        draw.line((90, top, 910, top), fill=(60, 60, 60), width=3)
        draw.rectangle((620, top + 18, 880, top + 75), outline=(80, 80, 80), width=3)
    image.save(stream, format="PNG")
    stream.seek(0)
    return stream


def _sse_events(response):
    events = []
    for block in response.text.replace("\r\n", "\n").split("\n\n"):
        event = "message"
        data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = json.loads(line.split(":", 1)[1].strip())
        if data is not None:
            events.append((event, data))
    return events


def _canonical_submission(tmp_path, raw: bytes, name: str = "page") -> tuple[bytes, dict]:
    source = tmp_path / "desktop-source" / f"{name}.png"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(raw)
    canonical = canonicalize_template_image(
        source,
        tmp_path / "desktop-output" / name,
        0,
    )
    metadata = dict(canonical.canonicalization)
    metadata.pop("source_image")
    return Path(canonical.image_path).read_bytes(), metadata


def _legacy_session_id(upload_dir: str, teacher_id: int = 1, class_id: int = 10) -> str:
    """Historical draft session: the source/answer-confirm flow only serves these now."""
    from services.teaching_context import make_teaching_context

    session = AnswerSheetSession(
        uuid.uuid4().hex[:16], upload_dir, teacher_id=teacher_id, class_id=class_id,
        teaching_context=make_teaching_context("senior"),
    )
    session.save()
    return session.session_id


def _layout_ready_session(client, upload_dir: str) -> str:
    session_id = _legacy_session_id(upload_dir)
    session = AnswerSheetSession.load(session_id, upload_dir)
    session.status = "source_recognized"
    session.source_question_drafts = [_question()]
    session.save()
    assert client.post(
        f"/api/answer-sheet/sessions/{session_id}/answers/confirm",
        json={"questions": [_question()]},
    ).status_code == 200
    return session_id


@pytest.fixture
def client_env(tmp_path, monkeypatch):
    monkeypatch.setattr("config.ANSWER_SHEET_GRADING_ENABLED", True)

    async def available(_teacher_id):
        return {"status": "available", "label": "可用"}

    async def ocr_ready():
        return {"provider": "ocr", "configured": True, "ready": True}

    monkeypatch.setattr(
        "services.ocr_teacher_fairness.public_teacher_availability", available
    )
    monkeypatch.setattr("services.ocr_service.get_ocr_readiness", ocr_ready)
    monkeypatch.setattr(
        "services.flash_grading.get_flash_readiness",
        lambda: {"provider": "flash", "configured": True, "ready": True},
    )
    monkeypatch.setattr(
        "services.model_concurrency.get_model_limiter",
        lambda: object(),
    )
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    db.add_all([
        User(id=1, name="老师一", role="teacher", education_stage="senior"),
        User(id=2, name="老师二", role="teacher", education_stage="senior"),
        TeacherClass(id=10, teacher_id=1, class_name="一班"),
        TeacherClass(id=20, teacher_id=2, class_name="二班"),
        Student(id=101, teacher_id=1, class_id=10, name="测试学生", student_no="001"),
    ])
    db.commit()
    db.close()

    from routers import answer_sheet
    upload_dir = str(tmp_path / "uploads")
    monkeypatch.setattr(answer_sheet_sessions, "UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(answer_sheet_common, "UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(answer_sheet_availability, "UPLOAD_DIR", upload_dir)

    async def fake_column_layout(*_args, **_kwargs):
        assert _kwargs.get("column_mode") == "triple"
        return {
            "column_layout": {
                "column_count": 2,
                "columns": [
                    {"column_index": 1, "x1_1000": 0, "x2_1000": 500},
                    {"column_index": 2, "x1_1000": 500, "x2_1000": 1000},
                ],
            },
            "candidates": [],
        }

    monkeypatch.setattr(
        answer_sheet_layout_freeze,
        "recognize_column_layout",
        fake_column_layout,
    )

    async def storage_ready(*_args, **_kwargs):
        return {"ready": True, "status": "available", "code": "ready"}

    monkeypatch.setattr(
        answer_sheet_student_upload,
        "get_upload_storage_readiness_async",
        storage_ready,
    )
    monkeypatch.setattr(
        answer_sheet_availability,
        "get_upload_storage_readiness_async",
        storage_ready,
    )
    monkeypatch.setattr(
        answer_sheet_assets,
        "get_upload_storage_readiness_async",
        storage_ready,
    )

    async def hardware_ready(_teacher_id):
        return {"status": "ready", "message": "hardware:ready"}

    monkeypatch.setattr(
        answer_sheet_availability,
        "get_public_hardware_readiness",
        hardware_ready,
    )

    canonical_sources = {}

    async def canonicalization_ready(*, source_path, **_kwargs):
        job_id = f"test-canonical-{len(canonical_sources) + 1}"
        canonical_sources[job_id] = source_path
        return SimpleNamespace(
            state="ready",
            canonical_path=None,
            canonicalization=None,
            job_id=job_id,
            reason_code="",
        )

    def adopt_test_canonicalization(
        handle,
        *,
        template_dir,
        page_index,
        storage_namespace,
        **_kwargs,
    ):
        canonical = answer_sheet.canonicalize_template_image(
            canonical_sources[handle.job_id],
            template_dir,
            page_index,
            storage_namespace=storage_namespace,
        )
        return SimpleNamespace(
            state="ready",
            canonical_path=canonical.image_path,
            canonicalization=canonical.canonicalization,
            job_id=handle.job_id,
            reason_code="",
        )

    monkeypatch.setattr(
        answer_sheet_layout,
        "try_accelerated_template_canonicalization",
        canonicalization_ready,
    )
    monkeypatch.setattr(
        answer_sheet_layout,
        "adopt_accelerated_template_canonicalization",
        adopt_test_canonicalization,
    )

    def override_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()
    app.include_router(answer_sheet.router, prefix="/api")
    app.dependency_overrides[get_db] = override_db

    def client(teacher_id):
        # Batches inherit the teacher's global current class (slice 1).
        current_class_id = {1: 10, 2: 20}.get(teacher_id)
        app.dependency_overrides[_require_user] = lambda: User(
            id=teacher_id, name="t", role="teacher", education_stage="senior",
            current_class_id=current_class_id,
        )
        result = TestClient(app)
        result.session_factory = Session
        return result

    return client, upload_dir


def test_session_is_owned_and_paths_are_not_exposed(client_env):
    make_client, upload_dir = client_env
    session_id = _legacy_session_id(upload_dir)
    created = make_client(1).get(f"/api/answer-sheet/sessions/{session_id}").json()
    assert created["status"] == "draft"
    assert make_client(2).get(f"/api/answer-sheet/sessions/{session_id}").status_code == 404

    uploaded = make_client(1).post(
        f"/api/answer-sheet/sessions/{session_id}/source/upload",
        data={"answer_source": "embedded"},
        files=[("source_files", ("source.png", _png_bytes(), "image/png"))],
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["status"] == "source_uploaded"
    assert "path" not in uploaded.json()["source_exam_pages"][0]


def test_session_creation_rejects_removed_review_category(client_env):
    make_client, _ = client_env
    response = make_client(1).post(
        "/api/answer-sheet/sessions",
        json={"class_id": 10, "category": "review"},
    )
    assert response.status_code == 422


def test_source_upload_rejects_corrupt_image_without_mutating_session(client_env):
    make_client, upload_dir = client_env
    client = make_client(1)
    session_id = _legacy_session_id(upload_dir)

    response = client.post(
        f"/api/answer-sheet/sessions/{session_id}/source/upload",
        data={"answer_source": "embedded"},
        files=[("source_files", ("broken.png", b"not-an-image", "image/png"))],
    )

    assert response.status_code == 400
    restored = AnswerSheetSession.load(session_id, upload_dir)
    assert restored.status == "draft"
    assert restored.source_exam_pages == []


def test_answer_sheet_availability_is_fail_closed_until_real_crop_gate_passes(
    client_env, monkeypatch
):
    make_client, _ = client_env
    monkeypatch.setattr("config.ANSWER_SHEET_GRADING_ENABLED", False)
    response = make_client(1).get("/api/answer-sheet/availability")
    assert response.status_code == 200
    assert response.json() == {
        "status": "unavailable",
        "label": "暂不可用",
        "message": "答题卡批阅暂时不可用：真实学生答题卡裁剪验收尚未通过。",
        "hardware": {
            "status": "ready",
            "message": "hardware:ready",
        },
    }
    assert {"active_teachers", "queue_length", "free_slots", "position"}.isdisjoint(
        response.json()
    )


def test_answer_sheet_availability_requires_the_complete_grading_chain(client_env, monkeypatch):
    monkeypatch.setattr("services.flash_grading.get_flash_readiness", lambda: {
        "configured": True, "ready": True, "reason": "",
    })
    make_client, _ = client_env
    response = make_client(1).get("/api/answer-sheet/availability")

    assert response.status_code == 200
    assert response.json() == {
        "status": "available",
        "label": "可用",
        "message": "视觉识别、数学批改和任务协调链路已就绪。",
        "hardware": {
            "status": "ready",
            "message": "hardware:ready",
        },
    }
    assert {"active_teachers", "queue_length", "free_slots", "position"}.isdisjoint(
        response.json()
    )


@pytest.mark.parametrize(
    ("hardware_status", "expected_status"),
    [
        ("waiting_accelerator", "queued"),
        ("unavailable", "unavailable"),
        ("fault", "fault"),
    ],
)
def test_answer_sheet_availability_applies_hardware_state(
    client_env,
    monkeypatch,
    hardware_status,
    expected_status,
):
    make_client, _ = client_env

    async def hardware(_teacher_id):
        return {
            "status": hardware_status,
            "message": f"hardware:{hardware_status}",
        }

    monkeypatch.setattr(
        "routers.answer_sheet_availability.get_public_hardware_readiness",
        hardware,
    )
    response = make_client(1).get("/api/answer-sheet/availability")

    assert response.status_code == 200
    state = response.json()
    assert state["hardware"] == {
        "status": hardware_status,
        "message": f"hardware:{hardware_status}",
    }
    assert state["status"] == expected_status
    if hardware_status == "waiting_accelerator":
        assert state["label"] == "需排队"
    else:
        assert state["status"] not in {"available", "queued"}


def test_answer_sheet_availability_blocks_when_ocr_is_not_ready(
    client_env, monkeypatch
):
    make_client, _ = client_env

    async def unavailable_ocr():
        return {"provider": "ocr", "configured": True, "ready": False}

    monkeypatch.setattr("services.ocr_service.get_ocr_readiness", unavailable_ocr)
    response = make_client(1).get("/api/answer-sheet/availability")

    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert response.json()["label"] == "暂不可用"


def test_answer_sheet_availability_blocks_when_selected_grader_is_not_ready(
    client_env, monkeypatch,
):
    make_client, _ = client_env
    monkeypatch.setattr(
        "services.flash_grading.get_flash_readiness",
        lambda: {"provider": "flash", "configured": True, "ready": False},
    )
    response = make_client(1).get("/api/answer-sheet/availability")

    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert response.json()["label"] == "暂不可用"


def test_answer_sheet_availability_has_one_bounded_readiness_budget(
    client_env,
    monkeypatch,
):
    make_client, _ = client_env

    async def stalled_ocr():
        await asyncio.Event().wait()

    monkeypatch.setattr("services.ocr_service.get_ocr_readiness", stalled_ocr)
    monkeypatch.setattr(
        "routers.answer_sheet_availability.READINESS_IO_TIMEOUT_SECONDS",
        0.01,
    )

    response = make_client(1).get("/api/answer-sheet/availability")

    assert response.status_code == 200
    assert response.json()["status"] == "fault"


def test_answer_sheet_redis_fault_blocks_new_session(client_env, monkeypatch):
    make_client, _ = client_env
    from services import model_concurrency

    class BrokenRedisLimiter:
        async def ping(self):
            raise ConnectionError("redis unavailable")

    monkeypatch.setattr(
        model_concurrency,
        "RedisModelLimiter",
        BrokenRedisLimiter,
    )
    monkeypatch.setattr(
        model_concurrency,
        "get_model_limiter",
        lambda: BrokenRedisLimiter(),
    )

    availability = make_client(1).get("/api/answer-sheet/availability")
    assert availability.status_code == 200
    assert availability.json()["status"] == "fault"
    assert availability.json()["label"] == "链路故障"

    creation = make_client(1).post(
        "/api/answer-sheet/sessions",
        json={"paper_template_code": "PT-0000-0000-0000-0000"},
    )
    assert creation.status_code == 503
    assert "协调服务发生故障" in creation.json()["detail"]


def test_answer_sheet_session_creation_is_blocked_while_unavailable(
    client_env, monkeypatch
):
    make_client, _ = client_env
    monkeypatch.setattr("config.ANSWER_SHEET_GRADING_ENABLED", False)
    response = make_client(1).post(
        "/api/answer-sheet/sessions",
        json={"paper_template_code": "PT-0000-0000-0000-0000"},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == (
        "答题卡批阅暂时不可用：真实学生答题卡裁剪验收尚未通过。"
    )


def test_answer_sheet_session_creation_is_blocked_when_runtime_is_unavailable(
    client_env, monkeypatch
):
    make_client, _ = client_env

    async def unavailable(_teacher_id):
        return {"status": "unavailable", "label": "暂不可用"}

    monkeypatch.setattr(
        "services.ocr_teacher_fairness.public_teacher_availability", unavailable
    )
    response = make_client(1).post(
        "/api/answer-sheet/sessions",
        json={"paper_template_code": "PT-0000-0000-0000-0000"},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "答题卡批阅服务当前暂不可用，请稍后重试。"


def test_existing_session_is_read_only_while_unavailable(client_env, monkeypatch):
    make_client, upload_dir = client_env
    client = make_client(1)
    session_id = _legacy_session_id(upload_dir)
    monkeypatch.setattr("config.ANSWER_SHEET_GRADING_ENABLED", False)

    assert client.get(f"/api/answer-sheet/sessions/{session_id}").status_code == 200
    blocked = client.post(
        f"/api/answer-sheet/sessions/{session_id}/source/upload",
        files={"source_files": ("paper.png", _png_bytes(), "image/png")},
        data={"answer_source": "embedded"},
    )
    assert blocked.status_code == 503
    assert AnswerSheetSession.load(session_id, upload_dir).status == "draft"
    assert client.delete(f"/api/answer-sheet/sessions/{session_id}").status_code == 200


def test_confirm_layout_freeze_and_reload(client_env, monkeypatch):
    make_client, upload_dir = client_env
    client = make_client(1)
    session_id = _legacy_session_id(upload_dir)
    client.post(
        f"/api/answer-sheet/sessions/{session_id}/source/upload",
        data={"answer_source": "ai_generated"},
        files=[("source_files", ("source.png", _png_bytes(), "image/png"))],
    )
    session = AnswerSheetSession.load(session_id, upload_dir)
    session.status = "source_recognized"
    session.save()
    confirmed = client.post(
        f"/api/answer-sheet/sessions/{session_id}/answers/confirm",
        json={"questions": [_question()]},
    )
    assert confirmed.status_code == 200
    assert len(confirmed.json()["answer_spec_hash"]) == 64
    from routers import answer_sheet

    server_canonicalize = answer_sheet.canonicalize_template_image
    fallback_calls = []

    def tracked_server_canonicalize(*args, **kwargs):
        fallback_calls.append(args[0])
        return server_canonicalize(*args, **kwargs)

    monkeypatch.setattr(answer_sheet, "canonicalize_template_image", tracked_server_canonicalize)
    uploaded = client.post(
        f"/api/answer-sheet/sessions/{session_id}/layout/upload",
        files=[("files", ("layout.png", _png_bytes(), "image/png"))],
    )
    assert uploaded.status_code == 200
    public_page = uploaded.json()["answer_sheet_pages"][0]
    assert "path" not in public_page
    assert "working_path" not in public_page
    assert "source_image" not in public_page["canonicalization"]
    assert public_page["canonicalization"]["version"] == "paper-canonical-v1"
    assert public_page["working_sha256"] == public_page["canonicalization"][
        "canonical_sha256"
    ]
    assert len(fallback_calls) == 1
    image_response = client.get(
        f"/api/answer-sheet/sessions/{session_id}/layout/pages/page_1/image"
    )
    assert image_response.status_code == 200
    with Image.open(io.BytesIO(image_response.content)) as canonical_image:
        assert max(canonical_image.size) == 2064

    layout = {
        "answer_regions": [{
            "region_id": "q1", "page_id": "page_1", "question_no": "1",
            "bbox": [100, 200, 900, 500], "region_type": "choice_mark",
            "teacher_confirmed": True,
        }],
        "identity_regions": [{
            "region_id": "name", "page_id": "page_1", "bbox": [20, 20, 400, 120],
            "identity_type": "name", "teacher_confirmed": True,
        }],
    }
    saved = client.put(f"/api/answer-sheet/sessions/{session_id}/layout", json=layout)
    assert saved.status_code == 200
    assert saved.json()["summary"]["can_freeze"] is True
    monkeypatch.setattr(
        answer_sheet,
        "canonicalize_template_image",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("freeze must not canonicalize the layout again")
        ),
    )
    frozen = client.post(f"/api/answer-sheet/sessions/{session_id}/layout/freeze")
    assert frozen.status_code == 200
    assert frozen.json()["status"] == "frozen"
    assert len(frozen.json()["layout_hash"]) == 64
    public_artifact = frozen.json()["layout_artifacts"]["page_1"]
    assert "template_image" not in public_artifact
    assert "source_image" not in public_artifact["canonicalization"]

    reloaded = AnswerSheetSession.load(session_id, upload_dir)
    assert reloaded.answer_spec_hash == frozen.json()["answer_spec_hash"]
    assert reloaded.layout_hash == frozen.json()["layout_hash"]
    assert reloaded.layout_artifacts["page_1"]["canonicalization"] == (
        reloaded.answer_sheet_pages[0]["canonicalization"]
    )
    assert reloaded.layout_artifacts["page_1"]["sha256"] == (
        reloaded.answer_sheet_pages[0]["working_sha256"]
    )
    assert reloaded.layout_artifacts["page_1"]["question_crop_plan"][
        "parameters"
    ]["column_count"] == 2
    assert client.delete(f"/api/answer-sheet/sessions/{session_id}").status_code == 409


def test_answer_sheet_acceleration_preserves_frozen_question_crop_plan():
    from services.answer_sheet_acceleration import build_answer_sheet_acceleration_plan
    from services.template_anchor_crops import build_anchor_crop_plan

    artifact_sha256 = "a" * 64
    question_crop_plan = build_anchor_crop_plan(
        {
            "column_layout": {
                "column_count": 2,
                "columns": [
                    {"column_index": 1, "x1_1000": 0, "x2_1000": 500},
                    {"column_index": 2, "x1_1000": 500, "x2_1000": 1000},
                ],
            },
        },
        artifact_sha256=artifact_sha256,
        expected_question_numbers=["1"],
    )
    accelerated, region_plans = build_answer_sheet_acceleration_plan(
        {
            "page_1": {
                "sha256": artifact_sha256,
                "answer_regions": [],
                "blank_detection_enabled": True,
                "question_crop_plan": question_crop_plan,
            },
        },
        [{
            "page_id": "page_1",
            "region_id": "name",
            "identity_type": "name",
            "bbox": [20, 20, 400, 120],
        }],
        [{
            "page_id": "page_1",
            "region_id": "q1",
            "question_no": "1",
            "region_type": "choice_mark",
            "allow_multiple": False,
            "bbox": [600, 200, 900, 500],
        }],
    )

    assert accelerated["page_1"]["question_crop_plan"] == question_crop_plan
    assert accelerated["page_1"]["blank_detection_enabled"] is False
    assert region_plans["page_1"][-1]["question_no"] == "1"
    assert region_plans["page_1"][-1]["image_index"] == 2
    assert region_plans["page_1"][-1]["answer_bbox_1000"] == [200, 200, 800, 500]


def test_freeze_layout_persists_column_layout_crop_plan(client_env):
    make_client, upload_dir = client_env
    client = make_client(1)
    session_id = _layout_ready_session(client, upload_dir)
    session = AnswerSheetSession.load(session_id, upload_dir)
    template_path = Path(session.session_dir) / "layout-template.png"
    template_path.parent.mkdir(parents=True, exist_ok=True)
    template_path.write_bytes(_png_bytes().getvalue())
    session.answer_sheet_pages = [{
        "page_id": "page_1",
        "path": str(template_path),
    }]
    session.save()

    response = client.put(
        f"/api/answer-sheet/sessions/{session_id}/layout",
        json={
            "answer_regions": [{
                "region_id": "q1",
                "page_id": "page_1",
                "question_no": "1",
                "bbox": [100, 200, 900, 500],
                "region_type": "choice_mark",
                "teacher_confirmed": True,
            }],
            "identity_regions": [{
                "region_id": "name",
                "page_id": "page_1",
                "bbox": [20, 20, 400, 120],
                "identity_type": "name",
                "teacher_confirmed": True,
            }],
        },
    )
    assert response.status_code == 200

    frozen = client.post(f"/api/answer-sheet/sessions/{session_id}/layout/freeze")
    assert frozen.status_code == 200
    plan = frozen.json()["layout_artifacts"]["page_1"]["question_crop_plan"]
    assert plan["strategy"] == "full_height_columns"
    assert plan["parameters"]["column_count"] == 2
    assert plan["expected_question_numbers"] == ["1"]


def test_multiple_choice_layout_requires_allow_multiple_to_match_frozen_contract(client_env):
    make_client, upload_dir = client_env
    client = make_client(1)
    session_id = _layout_ready_session(client, upload_dir)
    session = AnswerSheetSession.load(session_id, upload_dir)
    session.answer_spec_snapshot[0]["choice_mode"] = "multiple"
    session.answer_spec_snapshot[0]["standard_answer"] = "AC"
    session.status = "answers_confirmed"
    session.answer_sheet_pages = [{"page_id": "page_1"}]
    session.save()
    region = {
        "region_id": "q1", "page_id": "page_1", "question_no": "1",
        "bbox": [100, 200, 900, 500], "region_type": "choice_mark",
        "teacher_confirmed": True,
    }

    rejected = client.put(
        f"/api/answer-sheet/sessions/{session_id}/layout",
        json={"answer_regions": [region], "identity_regions": []},
    )
    assert rejected.status_code == 409
    assert "单选/多选设置" in rejected.json()["detail"]

    accepted = client.put(
        f"/api/answer-sheet/sessions/{session_id}/layout",
        json={
            "answer_regions": [{**region, "allow_multiple": True}],
            "identity_regions": [],
        },
    )
    assert accepted.status_code == 200
    assert accepted.json()["session"]["answer_regions"][0]["allow_multiple"] is True


def test_layout_upload_accepts_desktop_canonical_for_preview_and_freeze(
    client_env,
    monkeypatch,
):
    make_client, upload_dir = client_env
    client = make_client(1)
    session_id = _layout_ready_session(client, upload_dir)
    raw = _png_bytes().getvalue()
    canonical_bytes, metadata = _canonical_submission(
        Path(upload_dir).parent,
        raw,
        "answer-sheet",
    )
    from routers import answer_sheet

    monkeypatch.setattr(
        answer_sheet,
        "canonicalize_template_image",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("submitted candidate must not use server fallback")
        ),
    )
    uploaded = client.post(
        f"/api/answer-sheet/sessions/{session_id}/layout/upload",
        data={"canonical_metadata": json.dumps([metadata])},
        files=[
            ("files", ("raw.png", raw, "image/png")),
            ("canonical_files", ("canonical.png", canonical_bytes, "image/png")),
        ],
    )

    assert uploaded.status_code == 200
    preview = client.get(
        f"/api/answer-sheet/sessions/{session_id}/layout/pages/page_1/image"
    )
    assert preview.content == canonical_bytes
    page = uploaded.json()["answer_sheet_pages"][0]
    assert "source_image" not in page["canonicalization"]
    stored_page = AnswerSheetSession.load(session_id, upload_dir).answer_sheet_pages[0]
    assert stored_page["canonicalization"]["source_image"].startswith("layout_canonical_")
    layout = {
        "answer_regions": [{
            "region_id": "q1",
            "page_id": "page_1",
            "question_no": "1",
            "bbox": [100, 200, 900, 500],
            "region_type": "choice_mark",
            "teacher_confirmed": True,
        }],
        "identity_regions": [{
            "region_id": "name",
            "page_id": "page_1",
            "bbox": [20, 20, 400, 120],
            "identity_type": "name",
            "teacher_confirmed": True,
        }],
    }
    assert client.put(
        f"/api/answer-sheet/sessions/{session_id}/layout",
        json=layout,
    ).status_code == 200
    assert client.post(
        f"/api/answer-sheet/sessions/{session_id}/layout/freeze"
    ).status_code == 200
    session = AnswerSheetSession.load(session_id, upload_dir)
    artifact = session.layout_artifacts["page_1"]
    artifact_path = Path(session.session_dir) / artifact["template_image"]
    assert artifact_path.read_bytes() == canonical_bytes
    assert artifact["sha256"] == metadata["canonical_sha256"]


def test_successful_layout_reupload_removes_previous_sensitive_assets(client_env):
    make_client, upload_dir = client_env
    client = make_client(1)
    session_id = _layout_ready_session(client, upload_dir)
    raw = _png_bytes().getvalue()

    first = client.post(
        f"/api/answer-sheet/sessions/{session_id}/layout/upload",
        files=[("files", ("first.png", raw, "image/png"))],
    )
    assert first.status_code == 200
    first_session = AnswerSheetSession.load(session_id, upload_dir)
    first_page = first_session.answer_sheet_pages[0]
    old_paths = {
        Path(first_page["path"]),
        Path(first_page["working_path"]),
        Path(first_session.session_dir)
        / first_page["canonicalization"]["source_image"],
    }
    assert all(path.is_file() for path in old_paths)

    second = client.post(
        f"/api/answer-sheet/sessions/{session_id}/layout/upload",
        files=[("files", ("second.png", raw, "image/png"))],
    )
    assert second.status_code == 200
    assert all(not path.exists() for path in old_paths)

    second_session = AnswerSheetSession.load(session_id, upload_dir)
    second_page = second_session.answer_sheet_pages[0]
    assert Path(second_page["path"]).is_file()
    assert Path(second_page["working_path"]).is_file()
    assert (
        Path(second_session.session_dir)
        / second_page["canonicalization"]["source_image"]
    ).is_file()


def test_layout_upload_rejects_desktop_hash_mismatch_without_fallback(
    client_env,
    monkeypatch,
):
    make_client, upload_dir = client_env
    client = make_client(1)
    session_id = _layout_ready_session(client, upload_dir)
    raw = _png_bytes().getvalue()
    canonical_bytes, metadata = _canonical_submission(
        Path(upload_dir).parent,
        raw,
        "answer-hash",
    )
    metadata["source_sha256"] = "0" * 64
    from routers import answer_sheet

    monkeypatch.setattr(
        answer_sheet,
        "canonicalize_template_image",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid submitted candidate must not fall back")
        ),
    )
    response = client.post(
        f"/api/answer-sheet/sessions/{session_id}/layout/upload",
        data={"canonical_metadata": json.dumps([metadata])},
        files=[
            ("files", ("raw.png", raw, "image/png")),
            ("canonical_files", ("canonical.png", canonical_bytes, "image/png")),
        ],
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "客户端标准模板候选校验失败"
    restored = AnswerSheetSession.load(session_id, upload_dir)
    assert restored.status == "answers_confirmed"
    assert restored.answer_sheet_pages == []


def test_layout_upload_rejects_malformed_desktop_candidate_pair(client_env, monkeypatch):
    make_client, upload_dir = client_env
    client = make_client(1)
    session_id = _layout_ready_session(client, upload_dir)
    raw = _png_bytes().getvalue()
    response = client.post(
        f"/api/answer-sheet/sessions/{session_id}/layout/upload",
        data={"canonical_metadata": "not-json"},
        files=[
            ("files", ("raw.png", raw, "image/png")),
            ("canonical_files", ("canonical.png", raw, "image/png")),
        ],
    )

    assert response.status_code == 422
    assert "malformed" in response.json()["detail"]

    count_mismatch = client.post(
        f"/api/answer-sheet/sessions/{session_id}/layout/upload",
        data={"canonical_metadata": "[]"},
        files=[
            ("files", ("raw-1.png", raw, "image/png")),
            ("files", ("raw-2.png", raw, "image/png")),
            ("canonical_files", ("canonical.png", raw, "image/png")),
        ],
    )
    assert count_mismatch.status_code == 422
    assert "数量" in count_mismatch.json()["detail"]

    from routers import answer_sheet

    monkeypatch.setattr(answer_sheet_source, "MAX_TEMPLATE_PAGES", 1)
    monkeypatch.setattr(answer_sheet_layout, "MAX_TEMPLATE_PAGES", 1)
    count_limit = client.post(
        f"/api/answer-sheet/sessions/{session_id}/layout/upload",
        data={"canonical_metadata": "[]"},
        files=[
            ("files", ("raw.png", raw, "image/png")),
            ("canonical_files", ("canonical.png", raw, "image/png")),
        ],
    )
    assert count_limit.status_code == 422
    assert "count must match" in count_limit.json()["detail"]

    two_raw_files = client.post(
        f"/api/answer-sheet/sessions/{session_id}/layout/upload",
        files=[
            ("files", ("raw-1.png", raw, "image/png")),
            ("files", ("raw-2.png", raw, "image/png")),
        ],
    )
    assert two_raw_files.status_code == 413
    assert "最多上传 1 页原图" in two_raw_files.json()["detail"]


def test_layout_canonicalization_failure_keeps_existing_draft(client_env):
    make_client, upload_dir = client_env
    client = make_client(1)
    session_id = _legacy_session_id(upload_dir)
    session = AnswerSheetSession.load(session_id, upload_dir)
    session.status = "source_recognized"
    session.source_question_drafts = [_question()]
    session.save()
    assert client.post(
        f"/api/answer-sheet/sessions/{session_id}/answers/confirm",
        json={"questions": [_question()]},
    ).status_code == 200
    first = client.post(
        f"/api/answer-sheet/sessions/{session_id}/layout/upload",
        files=[("files", ("layout.png", _png_bytes(), "image/png"))],
    )
    assert first.status_code == 200
    before = AnswerSheetSession.load(session_id, upload_dir)
    original_path = before.answer_sheet_pages[0]["path"]
    original_working_path = before.answer_sheet_pages[0]["working_path"]

    blank = io.BytesIO()
    Image.new("RGB", (1000, 1400), "white").save(blank, format="PNG")
    rejected = client.post(
        f"/api/answer-sheet/sessions/{session_id}/layout/upload",
        files=[("files", ("blank.png", blank.getvalue(), "image/png"))],
    )

    assert rejected.status_code == 422
    restored = AnswerSheetSession.load(session_id, upload_dir)
    assert restored.status == "layout_draft"
    assert restored.answer_sheet_pages[0]["path"] == original_path
    assert restored.answer_sheet_pages[0]["working_path"] == original_working_path
    assert os.path.isfile(original_path)
    assert os.path.isfile(original_working_path)


def test_pending_layout_upload_preserves_active_layout_until_accelerator_ready(
    client_env,
    monkeypatch,
):
    make_client, upload_dir = client_env
    client = make_client(1)
    session_id = _layout_ready_session(client, upload_dir)
    session = AnswerSheetSession.load(session_id, upload_dir)
    session.status = "layout_draft"
    old_source = Path(session.session_dir) / "old-source.png"
    old_working = Path(session.session_dir) / "old-working.png"
    old_artifact = Path(session.session_dir) / "old-artifact.png"
    for path in (old_source, old_working, old_artifact):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"old-layout-asset")
    old_pages = [{
        "page_id": "page_1",
        "path": str(old_source),
        "working_path": str(old_working),
        "sha256": "old-source-sha",
        "working_sha256": "old-working-sha",
        "canonicalization": {
            "canonical_sha256": "old-working-sha",
            "source_image": old_source.name,
        },
    }]
    old_regions = [{
        "region_id": "q1",
        "page_id": "page_1",
        "question_no": "1",
        "bbox": [10, 10, 100, 100],
        "region_type": "choice_mark",
        "teacher_confirmed": True,
    }]
    old_artifacts = {
        "page_1": {
            "template_image": old_artifact.name,
            "sha256": "old-artifact-sha",
            "canonicalization": {"source_image": old_source.name},
        }
    }
    session.answer_sheet_pages = old_pages
    session.answer_regions = old_regions
    session.layout_hash = "old-layout-hash"
    session.layout_artifacts = old_artifacts
    session.save()

    from routers import answer_sheet

    async def waiting(*_args, **_kwargs):
        return SimpleNamespace(
            state="pending",
            reason_code="accelerator_wait_timeout",
            job_id="canonical-job-1",
        )

    monkeypatch.setattr(
        answer_sheet_layout,
        "try_accelerated_template_canonicalization",
        waiting,
    )
    response = client.post(
        f"/api/answer-sheet/sessions/{session_id}/layout/upload",
        files=[("files", ("replacement.png", _png_bytes(), "image/png"))],
    )

    assert response.status_code == 202
    assert "pending_layout_pages" not in response.json()
    restored = AnswerSheetSession.load(session_id, upload_dir)
    assert restored.status == "layout_draft"
    assert restored.answer_sheet_pages == old_pages
    assert restored.answer_regions == old_regions
    assert restored.layout_hash == "old-layout-hash"
    assert restored.layout_artifacts == old_artifacts
    assert len(restored.pending_layout_pages) == 1
    pending_path = Path(restored.pending_layout_pages[0]["path"])
    assert pending_path.is_file()
    assert old_source.is_file()
    assert old_working.is_file()
    assert old_artifact.is_file()

    canonical_path = Path(session.session_dir) / "replacement-working.png"
    canonical_path.write_bytes(b"replacement-working")

    async def ready(*_args, **_kwargs):
        return SimpleNamespace(
            state="ready",
            reason_code="",
            job_id="canonical-job-1",
            canonical_path=str(canonical_path),
            canonicalization={
                "canonical_sha256": "replacement-working-sha",
                "version": "paper-canonical-v1",
            },
        )

    def adopt(*_args, **_kwargs):
        return SimpleNamespace(
            canonical_path=str(canonical_path),
            canonicalization={
                "canonical_sha256": "replacement-working-sha",
                "version": "paper-canonical-v1",
            },
        )

    monkeypatch.setattr(
        answer_sheet_layout,
        "resume_accelerated_template_canonicalization",
        ready,
    )
    monkeypatch.setattr(
        answer_sheet_layout,
        "adopt_accelerated_template_canonicalization",
        adopt,
    )
    processed = client.post(
        f"/api/answer-sheet/sessions/{session_id}/layout/process",
    )

    assert processed.status_code == 200
    swapped = AnswerSheetSession.load(session_id, upload_dir)
    assert swapped.pending_layout_pages == []
    assert swapped.status == "layout_draft"
    assert swapped.answer_sheet_pages[0]["path"] == str(pending_path)
    assert swapped.answer_sheet_pages[0]["working_path"] == str(canonical_path)
    assert swapped.answer_regions == []
    assert swapped.layout_hash == ""
    assert swapped.layout_artifacts == {}
    assert not old_source.exists()
    assert not old_working.exists()
    assert not old_artifact.exists()
    assert pending_path.is_file()
    assert canonical_path.is_file()


def test_pending_layout_fault_keeps_active_layout_and_checkpoint(
    client_env,
    monkeypatch,
):
    make_client, upload_dir = client_env
    client = make_client(1)
    session_id = _layout_ready_session(client, upload_dir)
    session = AnswerSheetSession.load(session_id, upload_dir)
    session.status = "layout_draft"
    old_source = Path(session.session_dir) / "old-source.png"
    old_working = Path(session.session_dir) / "old-working.png"
    old_source.write_bytes(b"old-source")
    old_working.write_bytes(b"old-working")
    session.answer_sheet_pages = [{
        "page_id": "page_1",
        "path": str(old_source),
        "working_path": str(old_working),
        "sha256": "old-source-sha",
        "canonicalization": {
            "canonical_sha256": "old-working-sha",
            "source_image": old_source.name,
        },
    }]
    session.answer_regions = [{"region_id": "q1"}]
    session.layout_hash = "old-layout-hash"
    session.layout_artifacts = {"page_1": {"sha256": "old-artifact-sha"}}
    session.save()

    from routers import answer_sheet

    async def waiting(*_args, **_kwargs):
        return SimpleNamespace(
            state="pending",
            reason_code="accelerator_wait_timeout",
            job_id="canonical-job-2",
        )

    monkeypatch.setattr(
        answer_sheet_layout,
        "try_accelerated_template_canonicalization",
        waiting,
    )
    uploaded = client.post(
        f"/api/answer-sheet/sessions/{session_id}/layout/upload",
        files=[("files", ("replacement.png", _png_bytes(), "image/png"))],
    )
    assert uploaded.status_code == 202
    pending_before = AnswerSheetSession.load(session_id, upload_dir)
    pending_path = Path(pending_before.pending_layout_pages[0]["path"])

    async def failed(*_args, **_kwargs):
        return SimpleNamespace(
            state="fault",
            reason_code="accelerator_job_failed",
            job_id="canonical-job-2",
        )

    monkeypatch.setattr(
        answer_sheet_layout,
        "resume_accelerated_template_canonicalization",
        failed,
    )
    resumed = client.post(
        f"/api/answer-sheet/sessions/{session_id}/layout/process",
    )

    assert resumed.status_code == 202
    restored = AnswerSheetSession.load(session_id, upload_dir)
    assert restored.status == "layout_draft"
    assert restored.answer_sheet_pages == pending_before.answer_sheet_pages
    assert restored.answer_regions == pending_before.answer_regions
    assert restored.layout_hash == pending_before.layout_hash
    assert restored.layout_artifacts == pending_before.layout_artifacts
    assert len(restored.pending_layout_pages) == 1
    assert restored.pending_layout_pages[0]["canonicalization_accelerator"][
        "state"
    ] == "fault"
    assert pending_path.is_file()
    assert old_source.is_file()
    assert old_working.is_file()


def test_layout_fail_closed_for_missing_binding_overlap_and_unconfirmed_identity(client_env):
    make_client, upload_dir = client_env
    session = AnswerSheetSession("a" * 16, upload_dir, teacher_id=1, class_id=10)
    session.status = "source_recognized"
    session.category = "exam"
    session.confirm_answers([_question("1"), _question("2")])
    session.answer_sheet_pages = [{"page_id": "page_1", "path": os.path.join(session.session_dir, "x.png")}]
    session.save()
    session.save_layout(
        [{"region_id": "q1", "page_id": "page_1", "question_no": "1", "bbox": [0, 0, 600, 600], "region_type": "choice_mark", "teacher_confirmed": True}],
        [{"region_id": "name", "page_id": "page_1", "bbox": [0, 0, 100, 100], "identity_type": "name", "teacher_confirmed": False}],
    )
    summary = session.layout_summary()
    assert summary["missing_questions"] == ["2"]
    assert summary["can_freeze"] is False
    with pytest.raises(ValueError):
        session.freeze_layout()


def test_layout_rejects_crop_memory_amplification(tmp_path):
    session = AnswerSheetSession(
        "d" * 16,
        str(tmp_path / "uploads"),
        teacher_id=1,
        class_id=10,
    )
    session.status = "source_recognized"
    session.confirm_answers([_question("1", "solve")])
    session.answer_sheet_pages = [{"page_id": "page_1"}]

    def answer(region_id, bbox):
        return {
            "region_id": region_id,
            "page_id": "page_1",
            "question_no": "1",
            "bbox": bbox,
            "region_type": "solve",
            "teacher_confirmed": True,
        }

    session.save_layout([answer("whole-page", [0, 0, 1000, 1000])], [])

    with pytest.raises(ValueError, match="大面积重叠"):
        session.save_layout([
            answer("first", [0, 0, 400, 400]),
            answer("second", [299, 0, 699, 400]),
        ], [])

    with pytest.raises(ValueError, match="单页答案区域数量过多"):
        session.save_layout([
            answer(f"tiny-{index}", [index, 0, index + 1, 1])
            for index in range(129)
        ], [])


def test_student_region_extraction_decodes_page_once_and_uses_safe_crop_names(
    tmp_path,
    monkeypatch,
):
    from routers import answer_sheet

    source = tmp_path / "aligned.png"
    Image.new("RGB", (400, 400), "white").save(source)
    real_loader = answer_sheet.load_rgb_image
    calls = []

    def tracked_loader(path):
        calls.append(path)
        return real_loader(path)

    monkeypatch.setattr(answer_sheet_recognize_page, "load_rgb_image", tracked_loader)
    identity, answers, reasons = answer_sheet._extract_student_page_regions(
        str(source),
        str(tmp_path / "evidence"),
        "../../paper",
        "page_1",
        [{
            "page_id": "page_1",
            "bbox": [0, 0, 200, 100],
            "identity_type": "name",
        }],
        [
            {
                "page_id": "page_1",
                "region_id": "../first",
                "question_no": "1",
                "bbox": [0, 200, 400, 400],
                "region_type": "choice_mark",
                "choice_options": ["A", "B", "C", "D"],
            },
            {
                "page_id": "page_1",
                "region_id": "../second",
                "question_no": "2",
                "bbox": [600, 200, 1000, 400],
                "region_type": "choice_mark",
                "choice_options": ["A", "B", "C", "D"],
            },
        ],
    )

    assert calls == [str(source)]
    assert len(identity) == 1
    assert len(answers) == 2
    assert len(reasons) == 2
    for item in answers:
        assert item["evidence_file"].startswith("crop-")
        assert "/" not in item["evidence_file"]
        assert "\\" not in item["evidence_file"]
        assert (tmp_path / "evidence" / item["evidence_file"]).is_file()


def test_student_recognition_source_never_reuses_aligned_output(tmp_path):
    from routers import answer_sheet

    source = tmp_path / "student.png"
    aligned = tmp_path / f"aligned-{'a' * 32}.png"
    oriented = tmp_path / "teacher-oriented-student-source-q1.png"

    assert answer_sheet._student_recognition_source_path({
        "path": str(source),
        "working_path": str(aligned),
    }) == str(source)
    assert answer_sheet._student_recognition_source_path({
        "path": str(source),
        "working_path": str(aligned),
        "orientation_working_path": str(aligned),
    }) == str(source)
    assert answer_sheet._student_recognition_source_path({
        "path": str(source),
        "working_path": str(aligned),
        "orientation_working_path": str(oriented),
    }) == str(oriented)


def test_student_region_extraction_removes_partial_crops_on_failure(
    tmp_path,
    monkeypatch,
):
    from routers import answer_sheet

    source = tmp_path / "aligned.png"
    Image.new("RGB", (400, 400), "white").save(source)
    evidence_root = tmp_path / "evidence"

    def fail_after_crop_write(*_args, **_kwargs):
        raise RuntimeError("simulated decoder failure")

    monkeypatch.setattr(answer_sheet_recognize_page, "read_choice_marks", fail_after_crop_write)
    with pytest.raises(RuntimeError, match="simulated decoder failure"):
        answer_sheet._extract_student_page_regions(
            str(source),
            str(evidence_root),
            "student_1",
            "page_1",
            [],
            [{
                "page_id": "page_1",
                "region_id": "answer_1",
                "question_no": "1",
                "bbox": [0, 0, 500, 500],
                "region_type": "choice_mark",
                "choice_options": ["A", "B", "C", "D"],
            }],
        )

    assert list(evidence_root.glob("crop-*.png")) == []
    assert list(evidence_root.glob(".*.tmp")) == []


def _student_asset_replacement_session(upload_dir: str, session_id: str):
    session = AnswerSheetSession(
        session_id,
        upload_dir,
        teacher_id=1,
        class_id=10,
    )
    session.status = "recognizing"
    session.layout_hash = "layout-v1"
    root = Path(session.session_dir)
    evidence_root = root / "evidence"
    oriented_root = root / "oriented"
    evidence_root.mkdir(parents=True, exist_ok=True)
    oriented_root.mkdir(parents=True, exist_ok=True)
    source = root / "student.png"
    oriented = oriented_root / "teacher-oriented-student-source-q1.png"
    old_aligned = evidence_root / f"aligned-{'1' * 32}.png"
    old_crop = evidence_root / f"crop-{'1' * 24}.png"
    for path, payload in (
        (source, b"immutable-source"),
        (oriented, b"teacher-oriented-source"),
        (old_aligned, b"old-aligned"),
        (old_crop, b"old-crop"),
    ):
        path.write_bytes(payload)
    session.student_pages = [{
        "paper_id": "student_1",
        "path": str(source),
        "sha256": "source-hash",
        "orientation_status": "teacher_confirmed",
        "orientation_working_path": str(oriented),
        "orientation_working_sha256": "oriented-hash",
        "working_path": str(old_aligned),
        "review_reasons": ["old-review"],
        "answers": [{
            "region_id": "answer_1",
            "question_no": "1",
            "evidence_file": old_crop.name,
        }],
    }]
    session.save()
    return session, source, oriented, old_aligned, old_crop


def _install_student_asset_matcher(monkeypatch, calls: list[str]):
    from routers import answer_sheet

    aligned_name = f"aligned-{'2' * 32}.png"

    class Matcher:
        def match_and_build(self, source_path, output_dir, *, include_evidence):
            calls.append(str(source_path))
            assert include_evidence is False
            aligned = Path(output_dir) / aligned_name
            aligned.write_bytes(b"new-aligned")

            class Result:
                page_type_id = "page_1"
                diagnostics = {
                    "aligned_image_path": str(aligned.resolve()),
                    "answer_source_complete": True,
                }

            return Result()

    monkeypatch.setattr(
        answer_sheet_student_upload,
        "TemplateEvidenceMatcher",
        lambda *_args, **_kwargs: Matcher(),
    )
    monkeypatch.setattr(
        answer_sheet_recognize_page,
        "TemplateEvidenceMatcher",
        lambda *_args, **_kwargs: Matcher(),
    )
    monkeypatch.setattr(
        answer_sheet_recognition,
        "TemplateEvidenceMatcher",
        lambda *_args, **_kwargs: Matcher(),
    )
    return aligned_name


def _successful_student_asset_extraction(
    _aligned_path,
    output_dir,
    _paper_id,
    _page_id,
    _identity_regions,
    _answer_regions,
    created_files,
):
    crop_name = f"crop-{'2' * 24}.png"
    crop_path = Path(output_dir) / crop_name
    crop_path.write_bytes(b"new-crop")
    created_files.append(str(crop_path.resolve()))
    return [], [{
        "region_id": "answer_1",
        "question_no": "1",
        "region_type": "choice_mark",
        "evidence_file": crop_name,
        "answer": "A",
        "status": "recognized",
    }], []


def test_student_recognition_checkpoint_is_saved_before_remote_consumption(
    tmp_path,
    monkeypatch,
):
    from routers import answer_sheet

    session, _source, _oriented, _old_aligned, _old_crop = (
        _student_asset_replacement_session(
            str(tmp_path / "uploads"),
            "4" * 16,
        )
    )
    completed_page = copy.deepcopy(session.student_pages[0])
    completed_page["recognition_checkpoint"] = {
        "signature": "layout-v1:oriented-hash",
        "state": "completed",
    }
    accelerator_handle = object()
    consumed = []

    def finish(handle, *, fallback_succeeded):
        restored = AnswerSheetSession.load(
            session.session_id,
            str(tmp_path / "uploads"),
        )
        assert restored.student_pages[0]["recognition_checkpoint"]["state"] == "completed"
        consumed.append((handle, fallback_succeeded))
        return 1

    monkeypatch.setattr(answer_sheet_student_files, "finish_business_preprocessing", finish)

    asyncio.run(answer_sheet._persist_student_recognition_result(session, {
        "page_index": 0,
        "page": completed_page,
        "replacement": {"accelerator_handle": accelerator_handle},
    }))

    assert consumed == [(accelerator_handle, True)]
    assert session.student_pages[0] is completed_page


@pytest.mark.parametrize("replacement_kind", ["local", "accelerator"])
def test_failed_student_recognition_checkpoint_rolls_back_without_consuming_remote(
    tmp_path,
    monkeypatch,
    replacement_kind,
):
    from routers import answer_sheet

    session, source, oriented, old_aligned, old_crop = (
        _student_asset_replacement_session(
            str(tmp_path / "uploads"),
            "5" * 16,
        )
    )
    before_meta = Path(session.meta_path).read_bytes()
    previous_page = session.student_pages[0]
    completed_page = copy.deepcopy(previous_page)
    completed_page["recognition_checkpoint"] = {
        "signature": "layout-v1:oriented-hash",
        "state": "completed",
    }
    evidence_root = Path(session.session_dir) / "evidence"
    new_aligned = evidence_root / f"aligned-{'2' * 32}.png"
    new_crop = evidence_root / f"crop-{'2' * 24}.png"
    if replacement_kind == "local":
        new_aligned.write_bytes(b"new-aligned")
        new_crop.write_bytes(b"new-crop")
        completed_page["working_path"] = str(new_aligned.resolve())
        completed_page["answers"] = [{"evidence_file": new_crop.name}]
        replacement = {
            "page": completed_page,
            "previous_page": copy.deepcopy(previous_page),
            "aligned_path": str(new_aligned.resolve()),
            "crop_paths": [str(new_crop.resolve())],
            "answers": completed_page["answers"],
        }
    else:
        replacement = {"accelerator_handle": object()}

    consumed = []

    def fail_save():
        raise OSError("simulated metadata write failure")

    monkeypatch.setattr(session, "save", fail_save)
    monkeypatch.setattr(
        answer_sheet_student_files,
        "finish_business_preprocessing",
        lambda *args, **kwargs: consumed.append((args, kwargs)),
    )

    with pytest.raises(OSError, match="simulated metadata write failure"):
        asyncio.run(answer_sheet._persist_student_recognition_result(session, {
            "page_index": 0,
            "page": completed_page,
            "replacement": replacement,
        }))

    assert session.student_pages[0] is previous_page
    assert Path(session.meta_path).read_bytes() == before_meta
    assert source.is_file()
    assert oriented.is_file()
    assert old_aligned.is_file()
    assert old_crop.is_file()
    assert consumed == []
    if replacement_kind == "local":
        assert not new_aligned.exists()
        assert not new_crop.exists()


def test_successful_student_checkpoint_removes_only_superseded_generated_assets(
    tmp_path,
):
    from routers import answer_sheet

    session, source, oriented, old_aligned, old_crop = (
        _student_asset_replacement_session(
            str(tmp_path / "uploads"),
            "6" * 16,
        )
    )
    evidence_root = Path(session.session_dir) / "evidence"
    previous_page = copy.deepcopy(session.student_pages[0])
    new_aligned = evidence_root / f"aligned-{'3' * 32}.png"
    new_crop = evidence_root / f"crop-{'3' * 24}.png"
    new_aligned.write_bytes(b"new-aligned")
    new_crop.write_bytes(b"new-crop")
    completed_page = copy.deepcopy(previous_page)
    completed_page["working_path"] = str(new_aligned.resolve())
    completed_page["answers"] = [{"evidence_file": new_crop.name}]
    completed_page["recognition_checkpoint"] = {
        "signature": "layout-v1:oriented-hash",
        "state": "completed",
    }

    asyncio.run(answer_sheet._persist_student_recognition_result(session, {
        "page_index": 0,
        "page": completed_page,
        "replacement": {
            "page": completed_page,
            "previous_page": previous_page,
            "aligned_path": str(new_aligned.resolve()),
            "crop_paths": [str(new_crop.resolve())],
            "answers": completed_page["answers"],
        },
    }))

    restored = AnswerSheetSession.load(
        session.session_id,
        str(tmp_path / "uploads"),
    )
    assert restored.student_pages[0]["working_path"] == str(new_aligned.resolve())
    assert source.is_file()
    assert oriented.is_file()
    assert new_aligned.is_file()
    assert new_crop.is_file()
    assert not old_aligned.exists()
    assert not old_crop.exists()


def test_answer_sheet_image_work_respects_shared_preprocess_gate(monkeypatch):
    from routers import answer_sheet

    state_lock = threading.Lock()
    active = 0
    peak = 0

    def slow_image_work(value):
        nonlocal active, peak
        with state_lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.05)
            return value
        finally:
            with state_lock:
                active -= 1

    async def run():
        shared_gate = asyncio.Semaphore(1)
        monkeypatch.setattr(
            answer_sheet_common,
            "_get_local_ocr_preprocess_semaphore",
            lambda: shared_gate,
        )
        return await asyncio.gather(
            answer_sheet._run_answer_sheet_image_work(slow_image_work, "first"),
            answer_sheet._run_answer_sheet_image_work(slow_image_work, "second"),
        )

    assert asyncio.run(run()) == ["first", "second"]
    assert peak == 1


def test_cancelled_answer_sheet_image_work_holds_gate_until_thread_settles(
    monkeypatch,
):
    from routers import answer_sheet

    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()

    def blocked_image_work(call_number):
        if call_number == 1:
            first_started.set()
            assert release_first.wait(timeout=2)
        else:
            second_started.set()
        return call_number

    async def wait_for_thread_event(event):
        async with asyncio.timeout(1):
            while not event.is_set():
                await asyncio.sleep(0.005)

    async def run():
        gate = asyncio.Semaphore(1)
        monkeypatch.setattr(
            answer_sheet_common,
            "_get_local_ocr_preprocess_semaphore",
            lambda: gate,
        )
        first = asyncio.create_task(
            answer_sheet._run_answer_sheet_image_work(blocked_image_work, 1)
        )
        await wait_for_thread_event(first_started)
        first.cancel()
        second = asyncio.create_task(
            answer_sheet._run_answer_sheet_image_work(blocked_image_work, 2)
        )
        try:
            for _ in range(100):
                if first.done():
                    break
                await asyncio.sleep(0.01)
            assert first.done() is True
            assert second_started.is_set() is False
        finally:
            release_first.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert await second == 2

    asyncio.run(run())


def test_stalled_answer_sheet_image_work_fails_readiness_until_settled(
    monkeypatch,
):
    from routers import answer_sheet
    from services import ocr_service

    started = threading.Event()
    release = threading.Event()

    def blocked_image_work():
        started.set()
        assert release.wait(timeout=2)

    async def wait_for_thread_event(event):
        async with asyncio.timeout(1):
            while not event.is_set():
                await asyncio.sleep(0.005)

    async def run():
        gate = asyncio.Semaphore(1)
        monkeypatch.setattr(
            answer_sheet_common,
            "_get_local_ocr_preprocess_semaphore",
            lambda: gate,
        )
        task = asyncio.create_task(
            answer_sheet._run_answer_sheet_image_work(blocked_image_work)
        )
        await wait_for_thread_event(started)
        try:
            await asyncio.sleep(0.02)
            stalled = ocr_service.get_local_image_readiness()
        finally:
            release.set()
        await task
        recovered = ocr_service.get_local_image_readiness()
        return stalled, recovered

    monkeypatch.setattr(ocr_service, "_LOCAL_IMAGE_WORK_STALL_SECONDS", 0.01)
    stalled, recovered = asyncio.run(run())

    assert stalled["ready"] is False
    assert stalled["reason"] == "local_image_pipeline_stalled"
    assert stalled["active_components"] == 1
    assert recovered["ready"] is True
    assert recovered["active_components"] == 0


def test_save_images_streams_to_disk_before_path_validation_and_hashing(
    tmp_path,
    monkeypatch,
):
    from routers import answer_sheet

    class RecordingUpload:
        filename = "student.png"

        def __init__(self, raw):
            self.stream = io.BytesIO(raw)
            self.read_sizes = []

        async def read(self, size=-1):
            self.read_sizes.append(size)
            return self.stream.read(size)

    stages = []
    raw = b"a" * (1024 * 1024 + 17)

    async def storage_ready(*_args, **_kwargs):
        return {"ready": True, "status": "available", "code": "ready"}

    async def run_image_work(function, *args, **kwargs):
        kwargs.pop("settle_callbacks", ())
        if function is answer_sheet.validate_upload_path:
            stages.append("validate")
            assert args[0] == "student.png"
            assert Path(args[1]).read_bytes() == raw
            return {"safe": True}
        assert function is answer_sheet.sha256_file
        stages.append("hash")
        return function(*args, **kwargs)

    monkeypatch.setattr(
        answer_sheet_student_upload,
        "get_upload_storage_readiness_async",
        storage_ready,
    )
    monkeypatch.setattr(
        answer_sheet_availability,
        "get_upload_storage_readiness_async",
        storage_ready,
    )
    monkeypatch.setattr(
        answer_sheet_assets,
        "get_upload_storage_readiness_async",
        storage_ready,
    )
    monkeypatch.setattr(answer_sheet_student_upload, "_run_answer_sheet_image_work", run_image_work)
    monkeypatch.setattr(answer_sheet_student_pages, "_run_answer_sheet_image_work", run_image_work)
    monkeypatch.setattr(answer_sheet_recognize_page, "_run_answer_sheet_image_work", run_image_work)
    monkeypatch.setattr(answer_sheet_layout, "_run_answer_sheet_image_work", run_image_work)
    monkeypatch.setattr(answer_sheet_assets, "_run_answer_sheet_image_work", run_image_work)
    upload = RecordingUpload(raw)

    saved = asyncio.run(
        answer_sheet._save_images([upload], str(tmp_path), "student")
    )

    assert upload.read_sizes == [1024 * 1024, 1024 * 1024, 1024 * 1024]
    assert stages == ["validate", "hash"]
    assert Path(saved[0]["path"]).read_bytes() == raw
    assert saved[0]["sha256"] == hashlib.sha256(raw).hexdigest()


def test_save_images_preserves_browser_ocr_ready_contract(tmp_path, monkeypatch):
    from routers import answer_sheet

    encoded = io.BytesIO()
    Image.new("RGB", (320, 240), "white").save(encoded, "JPEG")
    raw = encoded.getvalue()
    upload = UploadFile(
        filename="student.ocr-ready-v1.jpg",
        file=io.BytesIO(raw),
    )

    async def storage_ready(*_args, **_kwargs):
        return {"ready": True, "status": "available", "code": "ready"}

    monkeypatch.setattr(
        answer_sheet_student_upload,
        "get_upload_storage_readiness_async",
        storage_ready,
    )
    monkeypatch.setattr(
        answer_sheet_availability,
        "get_upload_storage_readiness_async",
        storage_ready,
    )
    monkeypatch.setattr(
        answer_sheet_assets,
        "get_upload_storage_readiness_async",
        storage_ready,
    )
    saved = asyncio.run(
        answer_sheet._save_images([upload], str(tmp_path), "student")
    )

    assert saved[0]["path"].endswith(".ocr-ready-v1.jpg")
    assert Path(saved[0]["path"]).read_bytes() == raw


@pytest.mark.parametrize("failure_type", [OSError, asyncio.CancelledError])
def test_save_images_rolls_back_prior_and_current_partial_files(
    tmp_path,
    monkeypatch,
    failure_type,
):
    from routers import answer_sheet

    async def storage_ready(*_args, **_kwargs):
        return {"ready": True, "status": "available", "code": "ready"}

    async def run_image_work(function, *args, **kwargs):
        if function is answer_sheet.validate_upload_path:
            return {"safe": True}
        return function(*args, **kwargs)

    async def partial_second_write(upload, path, max_bytes):
        raw = await upload.read(max_bytes)
        Path(path).write_bytes(raw[:4])
        if Path(path).name.startswith("student_2_"):
            if failure_type is asyncio.CancelledError:
                raise asyncio.CancelledError
            raise OSError("simulated disk write failure")
        Path(path).write_bytes(raw)
        return len(raw)

    monkeypatch.setattr(
        answer_sheet_student_upload,
        "get_upload_storage_readiness_async",
        storage_ready,
    )
    monkeypatch.setattr(
        answer_sheet_availability,
        "get_upload_storage_readiness_async",
        storage_ready,
    )
    monkeypatch.setattr(
        answer_sheet_assets,
        "get_upload_storage_readiness_async",
        storage_ready,
    )
    monkeypatch.setattr(answer_sheet_student_upload, "stream_to_disk_capped", partial_second_write)
    monkeypatch.setattr(answer_sheet_assets, "stream_to_disk_capped", partial_second_write)
    monkeypatch.setattr(answer_sheet_student_upload, "_run_answer_sheet_image_work", run_image_work)
    monkeypatch.setattr(answer_sheet_student_pages, "_run_answer_sheet_image_work", run_image_work)
    monkeypatch.setattr(answer_sheet_recognize_page, "_run_answer_sheet_image_work", run_image_work)
    monkeypatch.setattr(answer_sheet_layout, "_run_answer_sheet_image_work", run_image_work)
    monkeypatch.setattr(answer_sheet_assets, "_run_answer_sheet_image_work", run_image_work)
    uploads = [
        UploadFile(filename="first.png", file=io.BytesIO(b"first-image")),
        UploadFile(filename="second.png", file=io.BytesIO(b"second-image")),
    ]

    with pytest.raises(failure_type):
        asyncio.run(answer_sheet._save_images(uploads, str(tmp_path), "student"))

    assert list(tmp_path.iterdir()) == []


def test_grading_draft_rechecks_orientation_and_source_pixel_gate(client_env):
    make_client, upload_dir = client_env
    client = make_client(1)
    session = AnswerSheetSession("b" * 16, upload_dir, teacher_id=1, class_id=10)
    session.status = "review"
    session.student_pages = [{
        "paper_id": "paper-1",
        "orientation_status": "teacher_confirmed",
        "review_reasons": [],
        "registration": {
            "status": "passed",
            "diagnostics": {"answer_source_complete": False},
        },
        "identity": {"status": "teacher_confirmed", "student_id": 1},
        "answers": [],
    }]
    session.save()

    response = client.post(f"/api/answer-sheet/sessions/{session.session_id}/grading/draft")
    assert response.status_code == 409
    assert "真实源像素" in response.json()["detail"]


def test_student_page_image_is_teacher_owned_and_never_serves_outside_session(client_env, tmp_path):
    make_client, upload_dir = client_env
    session = AnswerSheetSession("c" * 16, upload_dir, teacher_id=1, class_id=10)
    Path(session.session_dir).mkdir(parents=True, exist_ok=True)
    original = Path(session.session_dir) / "student-page.png"
    original.write_bytes(_png_bytes().getvalue())
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"not-a-session-asset")
    session.student_pages = [
        {"paper_id": "student_1", "path": str(original)},
        {"paper_id": "student_2", "path": str(outside)},
    ]
    session.save()

    response = make_client(1).get(
        f"/api/answer-sheet/sessions/{session.session_id}/students/student_1/image"
    )
    assert response.status_code == 200
    assert response.content == original.read_bytes()
    assert response.headers["cache-control"] == "private, no-store"
    assert make_client(2).get(
        f"/api/answer-sheet/sessions/{session.session_id}/students/student_1/image"
    ).status_code == 404
    assert make_client(1).get(
        f"/api/answer-sheet/sessions/{session.session_id}/students/student_2/image"
    ).status_code == 404
    assert make_client(1).get(
        f"/api/answer-sheet/sessions/{session.session_id}/students/missing/image"
    ).status_code == 404


def test_orientation_confirmation_hashes_the_source_as_a_stream(
    client_env,
    monkeypatch,
):
    from routers import answer_sheet

    make_client, upload_dir = client_env
    client = make_client(1)
    session = AnswerSheetSession("e" * 16, upload_dir, teacher_id=1, class_id=10)
    Path(session.session_dir).mkdir(parents=True, exist_ok=True)
    source = Path(session.session_dir) / "student-page.png"
    payload = _png_bytes().getvalue()
    source.write_bytes(payload)
    session.student_pages = [{"paper_id": "student_1", "path": str(source)}]
    session.save()

    def forbid_read_bytes(_path):
        raise AssertionError("orientation confirmation must not buffer the whole image")

    image_functions = []
    real_image_work = answer_sheet._run_answer_sheet_image_work

    async def tracked_image_work(function, *args, **kwargs):
        image_functions.append(function)
        return await real_image_work(function, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", forbid_read_bytes)
    monkeypatch.setattr(
        answer_sheet_student_upload,
        "_run_answer_sheet_image_work",
        tracked_image_work,
    )
    monkeypatch.setattr(
        answer_sheet_student_pages,
        "_run_answer_sheet_image_work",
        tracked_image_work,
    )
    monkeypatch.setattr(
        answer_sheet_recognize_page,
        "_run_answer_sheet_image_work",
        tracked_image_work,
    )
    monkeypatch.setattr(
        answer_sheet_layout,
        "_run_answer_sheet_image_work",
        tracked_image_work,
    )
    monkeypatch.setattr(
        answer_sheet_assets,
        "_run_answer_sheet_image_work",
        tracked_image_work,
    )
    response = client.post(
        f"/api/answer-sheet/sessions/{session.session_id}/students/student_1/orientation",
        json={"quarter_turns_clockwise": 0},
    )

    assert response.status_code == 200
    page = response.json()["student_pages"][0]
    assert page["orientation_source_sha256"] == hashlib.sha256(payload).hexdigest()
    assert page["orientation_status"] == "teacher_confirmed"
    assert image_functions == [
        answer_sheet.sha256_file,
        answer_sheet.create_teacher_oriented_copy,
    ]


def test_grading_draft_persists_explicit_question_to_student_page_mapping(
    client_env,
    monkeypatch,
):
    make_client, upload_dir = client_env
    client = make_client(1)
    session = AnswerSheetSession("d" * 16, upload_dir, teacher_id=1, class_id=10)
    session.status = "review"
    session.answer_spec_snapshot = [_question("1"), _question("2")]
    session.student_pages = [
        {
            "paper_id": "student_1",
            "orientation_status": "teacher_confirmed",
            "review_reasons": [],
            "registration": {
                "status": "passed",
                "diagnostics": {"answer_source_complete": True},
            },
            "identity": {
                "status": "teacher_confirmed",
                "student_id": 101,
                "student_name": "测试学生",
            },
            "answers": [
                {"question_no": "1", "answer": "A", "status": "teacher_confirmed"},
            ],
        },
        {
            "paper_id": "student_2",
            "orientation_status": "teacher_confirmed",
            "review_reasons": [],
            "registration": {
                "status": "passed",
                "diagnostics": {"answer_source_complete": True},
            },
            "identity": {
                "status": "teacher_confirmed",
                "student_id": 101,
                "student_name": "测试学生",
            },
            "answers": [
                {"question_no": "2", "answer": "B", "status": "teacher_confirmed"},
            ],
        },
    ]
    session.save()

    async def fake_grade(paper, *_args, **_kwargs):
        assert [item["question_number"] for item in paper["questions"]] == ["1", "2"]
        return {
            "student_name": "测试学生",
            "score": None,
            "max_score": None,
            "correct_rate": 1.0,
            "correct_count": 2,
            "partial_count": 0,
            "wrong_count": 0,
            "needs_review_count": 0,
            "total": 2,
            "details": [
                {"question_no": "1", "result": "correct"},
                {"question_no": "2", "result": "correct"},
            ],
        }

    from services import grader

    monkeypatch.setattr(grader, "grade_paper_unified", fake_grade)
    response = client.post(
        f"/api/answer-sheet/sessions/{session.session_id}/grading/draft"
    )
    assert response.status_code == 200
    draft = response.json()["drafts"][0]
    assert draft["paper_ids"] == ["student_1", "student_2"]
    assert draft["question_pages"] == {
        "1": ["student_1"],
        "2": ["student_2"],
    }
    assert draft["result"]["details"][0]["paper_ids"] == ["student_1"]
    assert draft["result"]["details"][1]["paper_ids"] == ["student_2"]
    restored = AnswerSheetSession.load(session.session_id, upload_dir)
    assert restored.grouping_decisions == response.json()["drafts"]


def test_grading_draft_rejects_stale_result_when_evidence_revision_changes(
    client_env,
    monkeypatch,
):
    make_client, upload_dir = client_env
    client = make_client(1)
    session = AnswerSheetSession("f" * 16, upload_dir, teacher_id=1, class_id=10)
    session.status = "review"
    session.answer_spec_snapshot = [_question("1")]
    session.student_pages = [{
        "paper_id": "student_1",
        "orientation_status": "teacher_confirmed",
        "review_reasons": [],
        "registration": {
            "status": "passed",
            "diagnostics": {"answer_source_complete": True},
        },
        "identity": {
            "status": "teacher_confirmed",
            "student_id": 101,
            "student_name": "测试学生",
        },
        "answers": [{
            "question_no": "1",
            "answer": "A",
            "status": "teacher_confirmed",
        }],
    }]
    session.save()

    async def mutate_revision_during_grade(*_args, **_kwargs):
        latest = AnswerSheetSession.load(session.session_id, upload_dir)
        latest.invalidate_grading_drafts()
        latest.save()
        return {
            "student_name": "测试学生",
            "total": 1,
            "correct_count": 1,
            "partial_count": 0,
            "wrong_count": 0,
            "needs_review_count": 0,
            "details": [{"question_no": "1", "result": "correct"}],
        }

    from services import grader

    monkeypatch.setattr(grader, "grade_paper_unified", mutate_revision_during_grade)
    response = client.post(
        f"/api/answer-sheet/sessions/{session.session_id}/grading/draft"
    )

    assert response.status_code == 409
    assert "证据已更新" in response.json()["detail"]
    restored = AnswerSheetSession.load(session.session_id, upload_dir)
    assert restored.review_revision == 1
    assert restored.grouping_decisions == []


def test_review_evidence_mutations_share_the_session_operation_lock(client_env):
    """Evidence writers must not overwrite a newer grading decision snapshot."""
    make_client, upload_dir = client_env
    client = make_client(1)
    session = AnswerSheetSession(
        "a" * 16,
        upload_dir,
        teacher_id=1,
        class_id=10,
    )
    session.status = "review"
    session.student_pages = [{
        "paper_id": "student_1",
        "path": str(Path(session.session_dir) / "student_1.png"),
        "orientation_status": "teacher_confirmed",
        "orientation_source_sha256": "source-hash",
        "identity": {
            "status": "teacher_confirmed",
            "student_id": 101,
            "student_name": "学生甲",
        },
        "answers": [{
            "question_no": "1",
            "answer": "旧答案",
            "status": "teacher_confirmed",
        }],
        "review_reasons": [],
    }]
    Path(session.session_dir).mkdir(parents=True, exist_ok=True)
    Path(session.student_pages[0]["path"]).write_bytes(b"student-page")
    session.review_revision = 7
    session.save()

    from services.answer_sheet_session import answer_sheet_operation_lock

    with answer_sheet_operation_lock(session.session_id, upload_dir):
        response = client.post(
            f"/api/answer-sheet/sessions/{session.session_id}/students/student_1/review-confirm",
            json={"student_id": 101, "answers": {"1": "新答案"}},
        )

    assert response.status_code == 409
    assert "正在执行另一项操作" in response.json()["detail"]
    restored = AnswerSheetSession.load(session.session_id, upload_dir)
    assert restored.review_revision == 7
    assert restored.student_pages[0]["answers"][0]["answer"] == "旧答案"


def _manual_decision_session(upload_dir, details=None):
    session = AnswerSheetSession("d" * 16, upload_dir, teacher_id=1, class_id=10)
    session.status = "review"
    session.category = "daily"
    if details is None:
        details = [
            {
                "question_no": "2(1)", "question_number": "legacy-alias",
                "parent_question_no": "2", "slot_id": "2[1]",
                "result": "needs_review", "paper_ids": ["student_1"],
            },
            {
                "question_number": "2(8)", "parent_question_no": "2",
                "slot_id": "2[8]", "result": "needs_review",
                "paper_ids": ["student_2"],
            },
        ]
    session.grouping_decisions = [{
        "student_id": 101,
        "paper_ids": ["student_1", "student_2"],
        "question_pages": {"2": ["student_1", "student_2"]},
        "manual_corrections": [],
        "result": {"details": copy.deepcopy(details)},
    }]
    session.save()
    return session


def test_grading_decision_keeps_legacy_detail_ids_independent_after_reopen(client_env):
    make_client, upload_dir = client_env
    client = make_client(1)
    session = _manual_decision_session(upload_dir)
    url = f"/api/answer-sheet/sessions/{session.session_id}"
    original_details = session.grouping_decisions[0]["result"]["details"]
    expected_audit = []
    previous = ("needs_review", "needs_review")
    for revision, verdicts in enumerate((("correct", "wrong"), ("partial", "correct"))):
        # Deliberately reverse submission order: match historical IDs, not positions.
        decisions = [
            {"question_no": number, "result": verdict, "note": f"review-{revision}"}
            for number, verdict in zip(("2(1)", "2(8)"), verdicts)
        ]
        saved = client.post(f"{url}/grading/decision", json={
            "student_id": 101,
            "review_revision": revision,
            "decisions": list(reversed(decisions)),
        })
        assert saved.status_code == 200, saved.text
        assert saved.json()["changed"] == 2
        reopened = client.get(url)
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["review_revision"] == revision + 1
        draft = reopened.json()["grouping_decisions"][0]
        assert draft == saved.json()["draft"]
        assert draft == AnswerSheetSession.load(session.session_id, upload_dir).grouping_decisions[0]
        assert draft["question_pages"] == {"2": ["student_1", "student_2"]}
        details = draft["result"]["details"]
        assert [detail["result"] for detail in details] == list(verdicts)
        assert all(detail["teacher_regraded"] for detail in details)
        for original, detail in zip(original_details, details):
            for key in ("question_no", "question_number", "parent_question_no", "slot_id", "paper_ids"):
                assert detail.get(key) == original.get(key)
        assert draft["result"]["needs_review_count"] == 0
        assert draft["result"]["correct_count"] == 1
        assert draft["result"]["wrong_count"] == (1 if revision == 0 else 0)
        assert draft["result"]["partial_count"] == revision
        expected_audit.extend([
            ("2(8)", previous[1], verdicts[1], "student_2", f"review-{revision}"),
            ("2(1)", previous[0], verdicts[0], "student_1", f"review-{revision}"),
        ])
        assert [
            (item["question_no"], item["old_result"], item["new_result"], item["paper_id"], item["note"])
            for item in draft["manual_corrections"]
        ] == expected_audit
        previous = verdicts


@pytest.mark.parametrize("case,status,message", [
    ("duplicate_submission", 400, "不得重复"),
    ("missing_submission", 400, "完整覆盖"),
    ("slot_ids_submission", 400, "完整覆盖"),
    ("stale_revision", 409, "草稿已更新"),
    ("duplicate_stored_id", 409, "草稿题号无效"),
    ("missing_stored_id", 409, "草稿题号无效"),
])
def test_grading_decision_identity_guards_do_not_mutate_session(client_env, case, status, message):
    make_client, upload_dir = client_env
    client = make_client(1)
    session = _manual_decision_session(upload_dir)
    decisions = [
        {"question_no": "2(1)", "result": "correct"},
        {"question_no": "2(8)", "result": "wrong"},
    ]
    session.review_revision = 3
    revision = 3
    details = session.grouping_decisions[0]["result"]["details"]
    if case == "duplicate_submission":
        decisions[1]["question_no"] = " 2(1) "
    elif case == "missing_submission":
        decisions.pop()
    elif case == "slot_ids_submission":
        decisions[0]["question_no"], decisions[1]["question_no"] = "2[1]", "2[8]"
    elif case == "stale_revision":
        revision = 2
    elif case == "duplicate_stored_id":
        details[1]["question_number"] = "2(1)"
    elif case == "missing_stored_id":
        details[1].pop("question_number")
        details[1].pop("parent_question_no")
    session.save()
    snapshot = (Path(session.session_dir) / "meta.json").read_bytes()
    response = client.post(
        f"/api/answer-sheet/sessions/{session.session_id}/grading/decision",
        json={"student_id": 101, "review_revision": revision, "decisions": decisions},
    )
    assert response.status_code == status, response.text
    assert message in response.json()["detail"]
    assert (Path(session.session_dir) / "meta.json").read_bytes() == snapshot
    reopened = client.get(f"/api/answer-sheet/sessions/{session.session_id}")
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["review_revision"] == 3
    assert reopened.json()["grouping_decisions"] == session.grouping_decisions


def test_grading_decision_parent_id_remains_a_legacy_fallback(client_env):
    make_client, upload_dir = client_env
    client = make_client(1)
    session = _manual_decision_session(upload_dir, [{
        "parent_question_no": "2", "slot_id": "2[8]", "result": "wrong",
    }])
    url = f"/api/answer-sheet/sessions/{session.session_id}"
    saved = client.post(f"{url}/grading/decision", json={
        "student_id": 101, "review_revision": 0,
        "decisions": [{"question_no": "2", "result": "correct"}],
    })
    assert saved.status_code == 200, saved.text
    reopened = client.get(url)
    assert reopened.status_code == 200, reopened.text
    draft = reopened.json()["grouping_decisions"][0]
    assert draft["result"]["details"][0]["result"] == "correct"
    assert draft["manual_corrections"][0]["question_no"] == "2"


def test_answer_sheet_manual_decision_revision_audit_and_archived_assets(client_env):
    make_client, upload_dir = client_env
    client = make_client(1)
    session = AnswerSheetSession("e" * 16, upload_dir, teacher_id=1, class_id=10)
    Path(session.session_dir).mkdir(parents=True, exist_ok=True)
    original = Path(session.session_dir) / "student-page.png"
    original.write_bytes(_png_bytes().getvalue())
    session.status = "review"
    session.category = "daily"
    session.layout_hash = "1" * 64
    session.answer_spec_hash = "2" * 64
    session.student_pages = [{
        "paper_id": "student_1",
        "filename": "张三第1页.png",
        "path": str(original),
        "sha256": "3" * 64,
        "orientation_status": "teacher_confirmed",
        "registration": {
            "status": "passed",
            "diagnostics": {"answer_source_complete": True},
        },
        "identity": {
            "status": "teacher_confirmed",
            "student_id": 101,
            "student_name": "测试学生",
        },
    }]
    session.grouping_decisions = [{
        "student_id": 101,
        "paper_ids": ["student_1"],
        "question_pages": {"1": ["student_1"]},
        "manual_corrections": [],
        "result": {
            "student_name": "测试学生",
            "score": None,
            "max_score": None,
            "correct_rate": None,
            "total": 1,
            "correct_count": 0,
            "partial_count": 0,
            "wrong_count": 0,
            "needs_review_count": 1,
            "details": [{
                "question_no": "1",
                "question": "1+1=?",
                "student_answer": "2",
                "correct_answer": "2",
                "result": "needs_review",
                "paper_ids": ["student_1"],
            }],
        },
    }]
    session.save()

    decided = client.post(
        f"/api/answer-sheet/sessions/{session.session_id}/grading/decision",
        json={
            "student_id": 101,
            "review_revision": 0,
            "decisions": [{
                "question_no": "1",
                "result": "correct",
                "note": "教师核对原卷后确认",
            }],
        },
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["review_revision"] == 1
    assert decided.json()["draft"]["result"]["needs_review_count"] == 0
    assert decided.json()["draft"]["result"]["details"][0]["teacher_regraded"] is True

    stale = client.post(
        f"/api/answer-sheet/sessions/{session.session_id}/grading/decision",
        json={
            "student_id": 101,
            "review_revision": 0,
            "decisions": [{"question_no": "1", "result": "wrong"}],
        },
    )
    assert stale.status_code == 409

    archived = client.post(
        f"/api/answer-sheet/sessions/{session.session_id}/grading/archive"
    )
    assert archived.status_code == 200, archived.text
    result_id = archived.json()["result_ids"][0]

    db = client.session_factory()
    grading = db.query(GradingResult).filter(GradingResult.id == result_id).one()
    audit = db.query(GradingCorrection).one()
    assert grading.needs_review_count == 0
    assert audit.paper_id == "student_1"
    assert (audit.question_no, audit.old_result, audit.new_result) == (
        "1", "needs_review", "correct",
    )
    assert audit.note == "教师核对原卷后确认"
    source_assets = grading.to_dict()["source_assets"]
    assert source_assets == {
        "source": "answer_sheet",
        "session_id": session.session_id,
        "pages": [{
            "paper_id": "student_1",
            "page_index": 0,
            "filename": "张三第1页.png",
        }],
    }
    db.close()

    restored = AnswerSheetSession.load(session.session_id, upload_dir)
    assert restored.status == "archived"
    assert restored.archived_result_ids == [result_id]
    assert restored.asset_lifecycle["policy"] == "raw_images_expire_after_36h"
    assert restored.asset_lifecycle["status"] == "retained_until_expiry"
    assert restored.asset_lifecycle["retention_hours"] == 36
    assert original.is_file()
    image = client.get(
        f"/api/answer-sheet/sessions/{session.session_id}/students/student_1/image"
    )
    assert image.status_code == 200
    assert image.headers["cache-control"] == "private, no-store"
    assert make_client(2).get(
        f"/api/answer-sheet/sessions/{session.session_id}/students/student_1/image"
    ).status_code == 404

    client = make_client(1)
    replay = client.post(
        f"/api/answer-sheet/sessions/{session.session_id}/grading/archive"
    )
    assert replay.status_code == 200
    assert replay.json()["idempotent"] is True
    assert replay.json()["result_ids"] == [result_id]


def test_answer_sheet_long_operation_emits_heartbeats(monkeypatch):
    from routers import answer_sheet

    monkeypatch.setattr(answer_sheet_recognition, "_ANSWER_SHEET_HEARTBEAT_SECONDS", 0.005)
    monkeypatch.setattr(answer_sheet_grading, "_ANSWER_SHEET_HEARTBEAT_SECONDS", 0.005)
    monkeypatch.setattr(answer_sheet_common, "_ANSWER_SHEET_HEARTBEAT_SECONDS", 0.005)

    async def slow_result():
        await asyncio.sleep(0.03)
        return {"ok": True}

    async def collect():
        return [
            item
            async for item in answer_sheet._with_answer_sheet_heartbeats(
                slow_result(),
                message="working",
                current=1,
                total=2,
            )
        ]

    events = asyncio.run(collect())
    assert any(event == "progress" and payload["heartbeat"] for event, payload in events)
    assert events[-1] == ("result", {"ok": True})


def test_source_recognition_stream_resumes_only_unfinished_pages(
    client_env,
    monkeypatch,
):
    from routers import answer_sheet

    make_client, upload_dir = client_env
    session = AnswerSheetSession("1" * 16, upload_dir, teacher_id=1, class_id=10)
    session.status = "source_uploaded"
    session.answer_source = "embedded"
    session.source_exam_pages = [
        {
            "page_id": "page_1",
            "path": str(Path(session.session_dir) / "first.png"),
            "sha256": "first-hash",
            "recognition_checkpoint": {
                "mode": "embedded",
                "sha256": "first-hash",
                "questions": [_question("1")],
            },
        },
        {
            "page_id": "page_2",
            "path": str(Path(session.session_dir) / "second.png"),
            "sha256": "second-hash",
        },
    ]
    session.save()
    calls = []

    async def recognize(path):
        calls.append(Path(path).name)
        return {"questions": [_question("2")]}

    monkeypatch.setattr(answer_sheet_source, "recognize_template_with_answers", recognize)
    response = make_client(1).post(
        f"/api/answer-sheet/sessions/{session.session_id}/source/recognize/stream"
    )

    assert response.status_code == 200
    events = _sse_events(response)
    assert calls == ["second.png"]
    assert events[-1][0] == "done"
    assert events[-1][1]["session"]["status"] == "source_recognized"
    restored = AnswerSheetSession.load(session.session_id, upload_dir)
    assert [item["question_no"] for item in restored.source_question_drafts] == ["1", "2"]
    assert all("recognition_checkpoint" not in page for page in restored.source_exam_pages)
    replay = make_client(1).post(
        f"/api/answer-sheet/sessions/{session.session_id}/source/recognize/stream"
    )
    assert _sse_events(replay)[-1][1]["idempotent"] is True
    assert calls == ["second.png"]

    with answer_sheet_operation_lock(session.session_id, upload_dir):
        pass


def test_source_recognition_done_survives_immediate_stream_close(
    client_env,
    monkeypatch,
):
    from routers import answer_sheet

    _make_client, upload_dir = client_env
    session = AnswerSheetSession("3" * 16, upload_dir, teacher_id=1, class_id=10)
    session.status = "source_uploaded"
    session.answer_source = "embedded"
    session.source_exam_pages = [{
        "page_id": "page_1",
        "path": str(Path(session.session_dir) / "source.png"),
        "sha256": "source-hash",
    }]
    session.save()

    async def recognize(_path):
        return {"questions": [_question("1")]}

    monkeypatch.setattr(answer_sheet_source, "recognize_template_with_answers", recognize)

    async def scenario():
        response = await answer_sheet.recognize_source_stream(
            session.session_id,
            User(id=1, name="teacher", role="teacher"),
        )
        iterator = response.body_iterator
        async for chunk in iterator:
            text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
            if text.startswith("event: done\n"):
                await iterator.aclose()
                return
        raise AssertionError("source recognition did not emit done")

    asyncio.run(scenario())

    restored = AnswerSheetSession.load(session.session_id, upload_dir)
    assert restored.status == "source_recognized"
    assert restored.error is None


def test_source_recognition_stream_refreshes_deadline_generation_but_not_retry_scope(
    client_env,
    monkeypatch,
):
    from routers import answer_sheet
    from services.ocr_service import (
        _multi_pool_route_keys,
        _multi_pool_route_keys_for_phase,
        _vision_phase_key,
        _vision_retry_phase_key,
    )

    make_client, upload_dir = client_env
    session = AnswerSheetSession("4" * 16, upload_dir, teacher_id=1, class_id=10)
    session.status = "source_uploaded"
    session.answer_source = "embedded"
    session.source_exam_pages = [{
        "page_id": "page_1",
        "path": str(Path(session.session_dir) / "source.png"),
        "sha256": "source-hash",
    }]
    session.save()
    scopes = []
    reset_calls = []
    real_reset = answer_sheet.reset_vision_phase_key

    async def recognize(path):
        deadline_batch, _paper, _priority = _multi_pool_route_keys(path)
        retry_scope = _vision_retry_phase_key.get()
        retry_batch, _retry_paper, _retry_priority = (
            _multi_pool_route_keys_for_phase(path, retry_scope)
        )
        scopes.append({
            "phase": _vision_phase_key.get(),
            "retry_scope": retry_scope,
            "deadline_batch": deadline_batch,
            "retry_batch": retry_batch,
        })
        if len(scopes) == 1:
            return {"error": "batch_deadline_exceeded", "questions": []}
        return {"questions": [_question("1")]}

    def track_reset(token):
        reset_calls.append(token)
        real_reset(token)

    monkeypatch.setattr(answer_sheet_source, "recognize_template_with_answers", recognize)
    monkeypatch.setattr(answer_sheet_source, "reset_vision_phase_key", track_reset)
    monkeypatch.setattr(answer_sheet_recognition, "reset_vision_phase_key", track_reset)
    client = make_client(1)

    exhausted = client.post(
        f"/api/answer-sheet/sessions/{session.session_id}/source/recognize/stream"
    )
    assert _sse_events(exhausted)[-1] == (
        "error",
        {"code": "TEMPLATE_OCR_FAILED", "message": "batch_deadline_exceeded"},
    )
    interrupted = AnswerSheetSession.load(session.session_id, upload_dir)
    assert interrupted.status == "failed"
    assert interrupted.source_recognition_generation == 0

    resumed = client.post(
        f"/api/answer-sheet/sessions/{session.session_id}/source/recognize/stream"
    )
    assert _sse_events(resumed)[-1][0] == "done"
    completed = AnswerSheetSession.load(session.session_id, upload_dir)
    assert completed.source_recognition_generation == 1
    assert [item["phase"] for item in scopes] == [
        "answer-sheet-source:0",
        "answer-sheet-source:1",
    ]
    assert {item["retry_scope"] for item in scopes} == {"answer-sheet-source"}
    assert scopes[0]["deadline_batch"] != scopes[1]["deadline_batch"]
    assert scopes[0]["retry_batch"] == scopes[1]["retry_batch"]
    assert len(reset_calls) == 2


def test_legacy_source_recognition_keeps_one_phase_for_ocr_and_ai_answers(
    client_env,
    monkeypatch,
):
    from routers import answer_sheet
    from services.ocr_service import _vision_phase_key, _vision_retry_phase_key

    make_client, upload_dir = client_env
    session = AnswerSheetSession("5" * 16, upload_dir, teacher_id=1, class_id=10)
    session.status = "source_uploaded"
    session.answer_source = "ai_generated"
    session.source_exam_pages = [{
        "page_id": "page_1",
        "path": str(Path(session.session_dir) / "source.png"),
        "sha256": "source-hash",
    }]
    session.save()
    scopes = []

    def capture_scope(stage):
        scopes.append((stage, _vision_phase_key.get(), _vision_retry_phase_key.get()))

    async def recognize_blank(_path):
        capture_scope("ocr")
        return {"questions": [_question("1")]}

    async def generate_answers(_questions):
        capture_scope("answers")
        return {
            "answers": {
                "1": {
                    "standard_answer": "A",
                    "model_answer": ["1"],
                },
            },
        }

    monkeypatch.setattr(answer_sheet_source, "recognize_template_blank", recognize_blank)
    monkeypatch.setattr(answer_sheet_source, "generate_template_answers_batch", generate_answers)
    response = make_client(1).post(
        f"/api/answer-sheet/sessions/{session.session_id}/source/recognize"
    )

    assert response.status_code == 200, response.text
    assert scopes == [
        ("ocr", "answer-sheet-source:0", "answer-sheet-source"),
        ("answers", "answer-sheet-source:0", "answer-sheet-source"),
    ]
    restored = AnswerSheetSession.load(session.session_id, upload_dir)
    assert restored.source_recognition_generation == 0
    assert restored.status == "source_recognized"


def test_busy_source_recognition_stream_does_not_advance_generation(
    client_env,
):
    make_client, upload_dir = client_env
    session = AnswerSheetSession("6" * 16, upload_dir, teacher_id=1, class_id=10)
    session.status = "failed"
    session.source_recognition_generation = 3
    session.source_exam_pages = [{
        "page_id": "page_1",
        "path": str(Path(session.session_dir) / "source.png"),
        "sha256": "source-hash",
    }]
    session.save()

    with answer_sheet_operation_lock(session.session_id, upload_dir):
        response = make_client(1).post(
            f"/api/answer-sheet/sessions/{session.session_id}/source/recognize/stream"
        )

    assert _sse_events(response)[-1][1]["code"] == "ANSWER_SHEET_OPERATION_BUSY"
    restored = AnswerSheetSession.load(session.session_id, upload_dir)
    assert restored.source_recognition_generation == 3


def _student_upload_session(upload_dir: str, session_id: str) -> AnswerSheetSession:
    session = AnswerSheetSession(session_id, upload_dir, teacher_id=1, class_id=10)
    session.status = "frozen"
    session.save()
    return session


def _different_png_bytes() -> bytes:
    stream = io.BytesIO()
    image = Image.new("RGB", (1000, 1400), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((30, 30, 970, 1370), outline="black", width=5)
    draw.text((120, 120), "SECOND STUDENT PAGE", fill="black")
    image.save(stream, format="PNG")
    return stream.getvalue()


def test_student_upload_persists_valid_pages_and_deduplicates_retries(client_env):
    make_client, upload_dir = client_env
    client = make_client(1)
    session = _student_upload_session(upload_dir, "7" * 16)

    started = client.post(
        f"/api/answer-sheet/sessions/{session.session_id}/students/upload/start",
        json={"restart": False},
    )
    assert started.status_code == 200
    upload = started.json()["student_upload"]
    upload_id = upload["upload_id"]
    assert upload["uploaded_count"] == 0
    assert "pages" not in upload
    assert "directory" not in upload

    raw = _png_bytes().getvalue()
    first = client.post(
        f"/api/answer-sheet/sessions/{session.session_id}/students/upload",
        data={"upload_id": upload_id},
        files=[("files", ("student-a.png", io.BytesIO(raw), "image/png"))],
    )
    assert first.status_code == 200, first.text
    assert first.json()["student_upload"]["uploaded_count"] == 1
    assert first.json()["student_pages"] == []

    duplicate = client.post(
        f"/api/answer-sheet/sessions/{session.session_id}/students/upload",
        data={"upload_id": upload_id},
        files=[("files", ("renamed.png", io.BytesIO(raw), "image/png"))],
    )
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["student_upload"]["uploaded_count"] == 1
    staged_dir = Path(session.session_dir) / f"student_upload_{upload_id}"
    assert len(list(staged_dir.iterdir())) == 1

    rejected = client.post(
        f"/api/answer-sheet/sessions/{session.session_id}/students/upload",
        data={"upload_id": upload_id},
        files=[("files", ("broken.png", io.BytesIO(b"not-an-image"), "image/png"))],
    )
    assert rejected.status_code == 400

    restored = AnswerSheetSession.load(session.session_id, upload_dir)
    staged = restored.student_upload["pages"]
    assert len(staged) == 1
    assert staged[0]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert Path(staged[0]["path"]).read_bytes() == raw
    assert restored.student_pages == []


def test_student_upload_rejects_multi_page_requests_without_mutation(client_env):
    make_client, upload_dir = client_env
    client = make_client(1)
    session = _student_upload_session(upload_dir, "8" * 16)
    started = client.post(
        f"/api/answer-sheet/sessions/{session.session_id}/students/upload/start",
        json={"restart": False},
    ).json()
    upload_id = started["student_upload"]["upload_id"]

    response = client.post(
        f"/api/answer-sheet/sessions/{session.session_id}/students/upload",
        data={"upload_id": upload_id},
        files=[
            ("files", ("first.png", _png_bytes(), "image/png")),
            ("files", ("second.png", io.BytesIO(_different_png_bytes()), "image/png")),
        ],
    )

    assert response.status_code == 413
    restored = AnswerSheetSession.load(session.session_id, upload_dir)
    assert restored.student_upload["pages"] == []


def test_student_upload_write_failure_removes_current_partial_file(
    client_env,
    monkeypatch,
):
    from routers import answer_sheet

    make_client, upload_dir = client_env
    client = make_client(1)
    session = _student_upload_session(upload_dir, "1" * 16)
    started = client.post(
        f"/api/answer-sheet/sessions/{session.session_id}/students/upload/start",
        json={"restart": False},
    ).json()
    upload_id = started["student_upload"]["upload_id"]

    async def fail_write(upload, path, max_bytes):
        raw = await upload.read(max_bytes)
        Path(path).write_bytes(raw[:8])
        raise OSError("simulated disk write failure")

    monkeypatch.setattr(answer_sheet_student_upload, "stream_to_disk_capped", fail_write)
    monkeypatch.setattr(answer_sheet_assets, "stream_to_disk_capped", fail_write)

    with pytest.raises(OSError, match="simulated disk write failure"):
        client.post(
            f"/api/answer-sheet/sessions/{session.session_id}/students/upload",
            data={"upload_id": upload_id},
            files=[("files", ("student.png", _png_bytes(), "image/png"))],
        )

    restored = AnswerSheetSession.load(session.session_id, upload_dir)
    assert restored.student_upload["pages"] == []
    staged_dir = Path(session.session_dir) / f"student_upload_{upload_id}"
    assert list(staged_dir.iterdir()) == []


def test_student_upload_total_limit_removes_partial_file(client_env, monkeypatch):
    from routers import answer_sheet

    make_client, upload_dir = client_env
    client = make_client(1)
    session = _student_upload_session(upload_dir, "2" * 16)
    started = client.post(
        f"/api/answer-sheet/sessions/{session.session_id}/students/upload/start",
        json={"restart": False},
    ).json()
    upload_id = started["student_upload"]["upload_id"]
    restored = AnswerSheetSession.load(session.session_id, upload_dir)
    restored.student_upload["pages"] = [{
        "paper_id": "student_1",
        "index": 0,
        "path": str(Path(restored.session_dir) / "existing.png"),
        "sha256": "existing",
        "byte_size": 6,
    }]
    restored.student_upload["total_bytes"] = 6
    restored.save()
    monkeypatch.setattr(answer_sheet_student_upload, "MAX_UPLOAD_FILE_SIZE", 8)
    monkeypatch.setattr(answer_sheet_assets, "MAX_UPLOAD_FILE_SIZE", 8)
    monkeypatch.setattr(answer_sheet_student_upload, "MAX_UPLOAD_TOTAL_SIZE", 10)
    monkeypatch.setattr(answer_sheet_source, "MAX_UPLOAD_TOTAL_SIZE", 10)
    monkeypatch.setattr(answer_sheet_layout, "MAX_UPLOAD_TOTAL_SIZE", 10)
    monkeypatch.setattr(answer_sheet_availability, "MAX_UPLOAD_TOTAL_SIZE", 10)
    monkeypatch.setattr(answer_sheet_assets, "MAX_UPLOAD_TOTAL_SIZE", 10)

    response = client.post(
        f"/api/answer-sheet/sessions/{session.session_id}/students/upload",
        data={"upload_id": upload_id},
        files=[("files", ("student.png", io.BytesIO(b"12345"), "image/png"))],
    )

    assert response.status_code == 413
    after = AnswerSheetSession.load(session.session_id, upload_dir)
    assert len(after.student_upload["pages"]) == 1
    staged_dir = Path(session.session_dir) / f"student_upload_{upload_id}"
    assert list(staged_dir.iterdir()) == []


def test_student_upload_processing_resumes_page_checkpoints_and_swaps_atomically(
    client_env,
    monkeypatch,
):
    from routers import answer_sheet

    make_client, upload_dir = client_env
    client = make_client(1)
    session = _student_upload_session(upload_dir, "9" * 16)
    old_page = Path(session.session_dir) / "old-student.png"
    old_page.write_bytes(_png_bytes().getvalue())
    session.student_pages = [{
        "paper_id": "old_student",
        "path": str(old_page),
        "sha256": "old-hash",
    }]
    session.grouping_decisions = [{"student_id": 101}]
    session.review_revision = 4
    session.save()

    started = client.post(
        f"/api/answer-sheet/sessions/{session.session_id}/students/upload/start",
        json={"restart": False},
    ).json()
    upload_id = started["student_upload"]["upload_id"]
    for filename, raw in (
        ("first.png", _png_bytes().getvalue()),
        ("second.png", _different_png_bytes()),
    ):
        uploaded = client.post(
            f"/api/answer-sheet/sessions/{session.session_id}/students/upload",
            data={"upload_id": upload_id},
            files=[("files", (filename, io.BytesIO(raw), "image/png"))],
        )
        assert uploaded.status_code == 200, uploaded.text

    calls = []
    fail_second_once = True

    def inspect(path):
        nonlocal fail_second_once
        calls.append(Path(path).name)
        if Path(path).name.startswith("student_2_") and fail_second_once:
            fail_second_once = False
            raise RuntimeError("simulated native image failure")
        return {
            "orientation_status": "auto_passed",
            "orientation_reason": "test",
            "orientation_confidence": 1.0,
        }

    monkeypatch.setattr(answer_sheet_student_upload, "_accelerator_managed_orientation", inspect)
    failed = client.post(
        f"/api/answer-sheet/sessions/{session.session_id}/students/upload/{upload_id}/process/stream"
    )
    failed_events = _sse_events(failed)
    assert failed_events[-1][0] == "error"
    interrupted = AnswerSheetSession.load(session.session_id, upload_dir)
    assert interrupted.student_upload["status"] == "processing"
    assert sum(
        answer_sheet._student_upload_checkpoint_complete(page)
        for page in interrupted.student_upload["pages"]
    ) == 1
    assert [page["paper_id"] for page in interrupted.student_pages] == ["old_student"]
    assert old_page.is_file()

    resumed = client.post(
        f"/api/answer-sheet/sessions/{session.session_id}/students/upload/{upload_id}/process/stream"
    )
    resumed_events = _sse_events(resumed)
    assert resumed_events[-1][0] == "done"
    completed = AnswerSheetSession.load(session.session_id, upload_dir)
    assert completed.status == "recognizing"
    assert completed.student_upload["status"] == "completed"
    assert completed.student_upload["uploaded_count"] == 2
    assert [page["paper_id"] for page in completed.student_pages] == [
        "student_1",
        "student_2",
    ]
    assert all("upload_checkpoint" not in page for page in completed.student_pages)
    assert completed.grouping_decisions == []
    assert completed.review_revision == 5
    assert not old_page.exists()
    assert sum(name.startswith("student_1_") for name in calls) == 1
    assert sum(name.startswith("student_2_") for name in calls) == 2

    replay = client.post(
        f"/api/answer-sheet/sessions/{session.session_id}/students/upload/{upload_id}/process/stream"
    )
    assert _sse_events(replay)[-1][1]["idempotent"] is True


def test_student_recognition_stream_skips_durable_page_checkpoint(
    client_env,
    monkeypatch,
):
    from routers import answer_sheet

    make_client, upload_dir = client_env
    session = AnswerSheetSession("2" * 16, upload_dir, teacher_id=1, class_id=10)
    session.status = "recognizing"
    session.layout_hash = "layout"
    session.student_pages = [
        {
            "paper_id": "student_1",
            "path": str(Path(session.session_dir) / "first.png"),
            "sha256": "first-hash",
            "review_reasons": [],
            "recognition_checkpoint": {
                "signature": "layout:first-hash",
                "state": "completed",
            },
        },
        {
            "paper_id": "student_2",
            "path": str(Path(session.session_dir) / "second.png"),
            "sha256": "second-hash",
            "review_reasons": [],
        },
    ]
    session.save()
    calls = []

    monkeypatch.setattr(
        answer_sheet_student_upload,
        "TemplateEvidenceMatcher",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        answer_sheet_recognize_page,
        "TemplateEvidenceMatcher",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        answer_sheet_recognition,
        "TemplateEvidenceMatcher",
        lambda *_args: object(),
    )

    async def recognize_page(locked, page, *_args):
        calls.append(page["paper_id"])
        page["recognition_checkpoint"] = {
            "signature": answer_sheet._student_recognition_signature(locked, page),
            "state": "completed",
        }

    monkeypatch.setattr(answer_sheet_recognition, "_recognize_one_student_page", recognize_page)
    response = make_client(1).post(
        f"/api/answer-sheet/sessions/{session.session_id}/students/recognize/stream"
    )

    assert response.status_code == 200
    assert calls == ["student_2"]
    assert _sse_events(response)[-1][0] == "done"
    restored = AnswerSheetSession.load(session.session_id, upload_dir)
    assert restored.status == "review"
    assert restored.error is None
    replay = make_client(1).post(
        f"/api/answer-sheet/sessions/{session.session_id}/students/recognize/stream"
    )
    assert _sse_events(replay)[-1][1]["idempotent"] is True
    assert calls == ["student_2"]


def test_student_recognition_stream_uses_bounded_isolated_workers(
    client_env,
    monkeypatch,
):
    from routers import answer_sheet
    from services import ocr_service
    from services.bounded_dispatch import bounded_completion_map

    make_client, upload_dir = client_env
    session = AnswerSheetSession("3" * 16, upload_dir, teacher_id=1, class_id=10)
    session.status = "recognizing"
    session.layout_hash = "layout"
    session.student_pages = [
        {
            "paper_id": f"student_{index}",
            "path": str(Path(session.session_dir) / f"student_{index}.png"),
            "sha256": f"hash-{index}",
            "review_reasons": [],
        }
        for index in range(4)
    ]
    session.save()

    active = 0
    max_active = 0
    contexts = []
    dispatch = {}

    async def recognize_page(locked, page, *_args):
        nonlocal active, max_active
        assert all(page is not stored for stored in locked.student_pages)
        active += 1
        max_active = max(max_active, active)
        contexts.append({
            "paper_id": page["paper_id"],
            "phase": ocr_service._vision_phase_key.get(),
            "retry_scope": ocr_service._vision_retry_phase_key.get(),
            "batch_count": ocr_service._vision_batch_paper_count.get(),
            "batch_deadline_s": ocr_service._vision_batch_deadline_s.get(),
            "route_ordinal": ocr_service._vision_route_ordinal.get(),
        })
        try:
            await asyncio.sleep(0.02)
            page["recognition_checkpoint"] = {
                "signature": answer_sheet._student_recognition_signature(locked, page),
                "state": "completed",
            }
        finally:
            active -= 1

    def completion_map(items, worker, **kwargs):
        dispatch.update(kwargs)
        return bounded_completion_map(
            items,
            worker,
            max_concurrent=kwargs["max_concurrent"],
        )

    monkeypatch.setattr(answer_sheet_recognition, "_recognize_one_student_page", recognize_page)
    monkeypatch.setattr(answer_sheet_recognition, "teacher_waved_completion_map", completion_map)
    monkeypatch.setattr(answer_sheet_recognition, "OCR_BATCH_DISPATCH_CONCURRENT", 2)
    monkeypatch.setattr(answer_sheet_recognition, "OCR_TEACHER_WAVE_SIZE", 8)

    response = make_client(1).post(
        f"/api/answer-sheet/sessions/{session.session_id}/students/recognize/stream"
    )

    assert response.status_code == 200
    assert _sse_events(response)[-1][0] == "done"
    assert max_active == 2
    assert dispatch["max_concurrent"] == 2
    assert dispatch["wave_size"] == 8
    assert dispatch["teacher_id"] == "1"
    assert len({item["phase"] for item in contexts}) == 1
    assert contexts[0]["phase"].startswith("answer-sheet-students:")
    assert {item["retry_scope"] for item in contexts} == {
        "answer-sheet-students"
    }
    assert {item["batch_count"] for item in contexts} == {4}
    assert {item["batch_deadline_s"] for item in contexts} == {600.0}
    assert {item["route_ordinal"] for item in contexts} == {0, 1, 2, 3}
    restored = AnswerSheetSession.load(session.session_id, upload_dir)
    assert restored.status == "review"
    assert all(
        page["recognition_checkpoint"]["state"] == "completed"
        for page in restored.student_pages
    )


def test_student_recognition_worker_failure_keeps_completed_page_checkpoint(
    client_env,
    monkeypatch,
):
    from routers import answer_sheet
    from services.bounded_dispatch import bounded_completion_map

    make_client, upload_dir = client_env
    session = AnswerSheetSession("5" * 16, upload_dir, teacher_id=1, class_id=10)
    session.status = "recognizing"
    session.layout_hash = "layout"
    session.student_pages = [
        {
            "paper_id": f"student_{index}",
            "path": str(Path(session.session_dir) / f"student_{index}.png"),
            "sha256": f"hash-{index}",
            "review_reasons": [],
        }
        for index in range(3)
    ]
    session.save()
    third_started = asyncio.Event()
    third_cancelled = asyncio.Event()

    async def recognize_page(locked, page, *_args):
        paper_id = page["paper_id"]
        if paper_id == "student_0":
            await asyncio.sleep(0.01)
            page["recognition_checkpoint"] = {
                "signature": answer_sheet._student_recognition_signature(locked, page),
                "state": "completed",
            }
            return None
        if paper_id == "student_1":
            await asyncio.sleep(0.08)
            page["review_reasons"] = ["fault"]
            page["recognition_checkpoint"] = {
                "signature": answer_sheet._student_recognition_signature(locked, page),
                "state": "fault",
                "reason_code": "test_fault",
            }
            raise answer_sheet.AnswerSheetAcceleratorState("fault", "test_fault")
        third_started.set()
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            third_cancelled.set()
            raise

    def completion_map(items, worker, **kwargs):
        return bounded_completion_map(
            items,
            worker,
            max_concurrent=kwargs["max_concurrent"],
        )

    monkeypatch.setattr(answer_sheet_recognition, "_recognize_one_student_page", recognize_page)
    monkeypatch.setattr(answer_sheet_recognition, "teacher_waved_completion_map", completion_map)
    monkeypatch.setattr(answer_sheet_recognition, "OCR_BATCH_DISPATCH_CONCURRENT", 2)

    response = make_client(1).post(
        f"/api/answer-sheet/sessions/{session.session_id}/students/recognize/stream"
    )

    events = _sse_events(response)
    assert events[-1][0] == "error"
    assert events[-1][1]["code"] == "ACCELERATOR_FAULT"
    assert third_started.is_set()
    assert third_cancelled.is_set()
    restored = AnswerSheetSession.load(session.session_id, upload_dir)
    assert restored.status == "recognizing"
    assert restored.student_pages[0]["recognition_checkpoint"]["state"] == "completed"
    assert restored.student_pages[1]["recognition_checkpoint"] == {
        "signature": "layout:hash-1",
        "state": "fault",
        "reason_code": "test_fault",
    }
    assert "recognition_checkpoint" not in restored.student_pages[2]


def test_student_recognition_done_survives_immediate_stream_close(
    client_env,
    monkeypatch,
):
    from routers import answer_sheet

    make_client, upload_dir = client_env
    client = make_client(1)
    session = AnswerSheetSession("4" * 16, upload_dir, teacher_id=1, class_id=10)
    session.status = "recognizing"
    session.layout_hash = "layout"
    session.student_pages = [{
        "paper_id": "student_1",
        "path": str(Path(session.session_dir) / "student.png"),
        "sha256": "student-hash",
        "review_reasons": [],
    }]
    session.save()

    monkeypatch.setattr(
        answer_sheet_student_upload,
        "TemplateEvidenceMatcher",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        answer_sheet_recognize_page,
        "TemplateEvidenceMatcher",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        answer_sheet_recognition,
        "TemplateEvidenceMatcher",
        lambda *_args: object(),
    )

    async def recognize_page(locked, page, *_args):
        page["recognition_checkpoint"] = {
            "signature": answer_sheet._student_recognition_signature(locked, page),
            "state": "completed",
        }

    monkeypatch.setattr(answer_sheet_recognition, "_recognize_one_student_page", recognize_page)

    async def scenario():
        db = client.session_factory()
        try:
            response = await answer_sheet.recognize_students_stream(
                session.session_id,
                db,
                User(id=1, name="teacher", role="teacher"),
            )
            iterator = response.body_iterator
            async for chunk in iterator:
                text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
                if text.startswith("event: done\n"):
                    await iterator.aclose()
                    return
            raise AssertionError("student recognition did not emit done")
        finally:
            db.close()

    asyncio.run(scenario())

    restored = AnswerSheetSession.load(session.session_id, upload_dir)
    assert restored.status == "review"
    assert restored.error is None


def test_grading_draft_done_survives_immediate_stream_close(client_env):
    from routers import answer_sheet

    _make_client, upload_dir = client_env
    session = AnswerSheetSession("5" * 16, upload_dir, teacher_id=1, class_id=10)
    session.status = "review"
    session.student_pages = [{"paper_id": "student_1", "review_reasons": []}]
    session.grouping_decisions = [{"student_id": 101, "result": {"score": 1}}]
    session.save()

    async def scenario():
        response = await answer_sheet.build_grading_draft_stream(
            session.session_id,
            User(id=1, name="teacher", role="teacher"),
        )
        iterator = response.body_iterator
        async for chunk in iterator:
            text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
            if text.startswith("event: done\n"):
                await iterator.aclose()
                return
        raise AssertionError("grading draft did not emit done")

    asyncio.run(scenario())

    restored = AnswerSheetSession.load(session.session_id, upload_dir)
    assert restored.error is None
    assert restored.grouping_decisions == session.grouping_decisions


def _runtime_gate_grading_page() -> dict:
    return {
        "paper_id": "student_1",
        "path": "student.png",
        "sha256": "student-hash",
        "orientation_status": "teacher_confirmed",
        "review_reasons": [],
        "registration": {
            "status": "passed",
            "diagnostics": {"answer_source_complete": True},
        },
        "identity": {
            "status": "teacher_confirmed",
            "student_id": 101,
            "student_name": "Test Student",
        },
        "answers": [{
            "question_no": "1",
            "answer": "A",
            "status": "teacher_confirmed",
        }],
    }


def _runtime_gate_case(upload_dir: str, operation: str):
    operation_index = {
        "source_sync": "1",
        "source_stream": "2",
        "upload_process_stream": "3",
        "students_sync": "4",
        "students_stream": "5",
        "grading_sync": "6",
        "grading_stream": "7",
    }[operation]
    session = AnswerSheetSession(
        operation_index * 16,
        upload_dir,
        teacher_id=1,
        class_id=10,
    )

    if operation.startswith("source_"):
        session.status = "failed"
        session.answer_source = "embedded"
        session.source_recognition_generation = 4
        session.error = "previous source failure"
        session.source_exam_pages = [{
            "page_id": "page_1",
            "path": str(Path(session.session_dir) / "source.png"),
            "sha256": "source-hash",
            "recognition_checkpoint": {
                "mode": "embedded",
                "sha256": "source-hash",
                "questions": [_question("1")],
            },
        }]
        endpoint = f"/api/answer-sheet/sessions/{session.session_id}/source/recognize"
        if operation.endswith("stream"):
            endpoint += "/stream"
        work_kind = "source"
    elif operation == "upload_process_stream":
        upload_id = "a" * 32
        session.status = "frozen"
        session.error = "previous upload failure"
        session.student_pages = [{"paper_id": "old_student", "sha256": "old-hash"}]
        session.grouping_decisions = [{"student_id": 101}]
        session.review_revision = 5
        session.student_upload = {
            "upload_id": upload_id,
            "status": "uploading",
            "error": "previous upload failure",
            "pages": [{
                "paper_id": "student_1",
                "path": str(Path(session.session_dir) / "student.png"),
                "sha256": "student-hash",
                "filename": "student.png",
            }],
        }
        endpoint = (
            f"/api/answer-sheet/sessions/{session.session_id}/students/upload/"
            f"{upload_id}/process/stream"
        )
        work_kind = "upload"
    elif operation.startswith("students_"):
        session.status = "recognizing"
        session.layout_hash = "layout-hash"
        session.error = "previous recognition failure"
        session.grouping_decisions = [{"student_id": 101}]
        session.review_revision = 5
        session.asset_lifecycle = {"sentinel": "keep"}
        session.student_pages = [{
            "paper_id": "student_1",
            "path": str(Path(session.session_dir) / "student.png"),
            "sha256": "student-hash",
            "review_reasons": ["previous_review_reason"],
        }]
        endpoint = f"/api/answer-sheet/sessions/{session.session_id}/students/recognize"
        if operation.endswith("stream"):
            endpoint += "/stream"
        work_kind = "students"
    else:
        session.status = "review"
        session.review_revision = 5
        session.layout_hash = "layout-hash"
        session.answer_spec_hash = "answer-hash"
        session.answer_spec_snapshot = [_question("1")]
        session.student_pages = [_runtime_gate_grading_page()]
        session.asset_lifecycle = {
            "grading_draft_checkpoint": {
                "review_revision": 4,
                "layout_hash": "stale-layout",
                "answer_spec_hash": "stale-answer",
                "student_ids": [101],
                "drafts": [],
            },
        }
        endpoint = f"/api/answer-sheet/sessions/{session.session_id}/grading/draft"
        if operation.endswith("stream"):
            endpoint += "/stream"
        work_kind = "grading"

    session.save()
    return session, endpoint, work_kind


def _forbid_runtime_work(monkeypatch, answer_sheet, work_kind: str, calls: list[str]):
    def forbidden(*_args, **_kwargs):
        calls.append(work_kind)
        raise AssertionError(f"{work_kind} work must not start while the chain is down")

    async def forbidden_async(*_args, **_kwargs):
        forbidden()

    if work_kind == "source":
        monkeypatch.setattr(
            answer_sheet_source,
            "recognize_template_with_answers",
            forbidden_async,
        )
    elif work_kind == "upload":
        monkeypatch.setattr(
            answer_sheet_student_upload,
            "_accelerator_managed_orientation",
            forbidden,
        )
    elif work_kind == "students":
        monkeypatch.setattr(answer_sheet_student_upload, "TemplateEvidenceMatcher", forbidden)
        monkeypatch.setattr(answer_sheet_recognize_page, "TemplateEvidenceMatcher", forbidden)
        monkeypatch.setattr(answer_sheet_recognition, "TemplateEvidenceMatcher", forbidden)
    else:
        from services import grader

        monkeypatch.setattr(grader, "grade_paper_unified", forbidden_async)


@pytest.mark.parametrize("runtime_status", ["unavailable", "fault"])
@pytest.mark.parametrize(
    ("operation", "streaming"),
    [
        ("source_sync", False),
        ("source_stream", True),
        ("upload_process_stream", True),
        ("students_sync", False),
        ("students_stream", True),
        ("grading_sync", False),
        ("grading_stream", True),
    ],
)
def test_existing_answer_sheet_runtime_gate_has_no_side_effects(
    client_env,
    monkeypatch,
    runtime_status,
    operation,
    streaming,
):
    from routers import answer_sheet

    make_client, upload_dir = client_env
    session, endpoint, work_kind = _runtime_gate_case(upload_dir, operation)
    before_meta = Path(session.meta_path).read_bytes()
    work_calls = []

    async def runtime_down(_user):
        return {
            "status": runtime_status,
            "message": f"runtime {runtime_status}",
        }

    monkeypatch.setattr(answer_sheet_availability, "answer_sheet_availability", runtime_down)
    _forbid_runtime_work(monkeypatch, answer_sheet, work_kind, work_calls)

    response = make_client(1).post(endpoint)

    if streaming:
        assert response.status_code == 200
        event, payload = _sse_events(response)[-1]
        assert event == "error"
        assert payload["code"] == "GRADING_CHAIN_FAILED"
    else:
        assert response.status_code == 503
    assert work_calls == []
    assert Path(session.meta_path).read_bytes() == before_meta


def _completed_runtime_gate_case(upload_dir: str, operation: str):
    operation_index = {
        "source": "8",
        "upload": "9",
        "students": "a",
        "grading": "b",
    }[operation]
    session = AnswerSheetSession(
        operation_index * 16,
        upload_dir,
        teacher_id=1,
        class_id=10,
    )

    if operation == "source":
        session.status = "source_recognized"
        session.source_question_drafts = [_question("1")]
        session.source_exam_pages = [{
            "page_id": "page_1",
            "path": str(Path(session.session_dir) / "source.png"),
            "sha256": "source-hash",
        }]
        endpoint = f"/api/answer-sheet/sessions/{session.session_id}/source/recognize/stream"
    elif operation == "upload":
        upload_id = "b" * 32
        session.status = "recognizing"
        session.student_upload = {
            "upload_id": upload_id,
            "status": "completed",
            "uploaded_count": 1,
        }
        session.student_pages = [{
            "paper_id": "student_1",
            "path": str(Path(session.session_dir) / "student.png"),
            "sha256": "student-hash",
        }]
        endpoint = (
            f"/api/answer-sheet/sessions/{session.session_id}/students/upload/"
            f"{upload_id}/process/stream"
        )
    elif operation == "students":
        session.status = "review"
        session.layout_hash = "layout-hash"
        session.student_pages = [{
            "paper_id": "student_1",
            "path": str(Path(session.session_dir) / "student.png"),
            "sha256": "student-hash",
            "recognition_checkpoint": {
                "signature": "layout-hash:student-hash",
                "state": "completed",
            },
        }]
        endpoint = f"/api/answer-sheet/sessions/{session.session_id}/students/recognize/stream"
    else:
        session.status = "review"
        session.student_pages = [_runtime_gate_grading_page()]
        session.grouping_decisions = [{
            "student_id": 101,
            "paper_ids": ["student_1"],
            "result": {"total": 1},
        }]
        session.review_revision = 6
        endpoint = f"/api/answer-sheet/sessions/{session.session_id}/grading/draft/stream"

    session.save()
    return session, endpoint


@pytest.mark.parametrize("operation", ["source", "upload", "students", "grading"])
def test_completed_answer_sheet_sse_replay_bypasses_runtime_gate(
    client_env,
    monkeypatch,
    operation,
):
    from routers import answer_sheet

    make_client, upload_dir = client_env
    session, endpoint = _completed_runtime_gate_case(upload_dir, operation)
    before_meta = Path(session.meta_path).read_bytes()

    async def readiness_must_not_run(_user):
        raise AssertionError("completed SSE replay must not depend on current readiness")

    monkeypatch.setattr(
        answer_sheet_availability,
        "answer_sheet_availability",
        readiness_must_not_run,
    )
    response = make_client(1).post(endpoint)

    assert response.status_code == 200
    event, payload = _sse_events(response)[-1]
    assert event == "done"
    assert payload["idempotent"] is True
    assert Path(session.meta_path).read_bytes() == before_meta


def test_grading_draft_stream_recovers_only_unfinished_students(
    client_env,
    monkeypatch,
):
    from services import grader

    make_client, upload_dir = client_env
    client = make_client(1)
    session = AnswerSheetSession("3" * 16, upload_dir, teacher_id=1, class_id=10)
    session.status = "review"
    session.review_revision = 7
    session.layout_hash = "layout-hash"
    session.answer_spec_hash = "answer-hash"
    session.answer_spec_snapshot = [_question("1"), _question("2")]
    session.student_pages = []
    for index, student_id in enumerate((101, 102), 1):
        session.student_pages.append({
            "paper_id": f"student_{index}",
            "orientation_status": "teacher_confirmed",
            "review_reasons": [],
            "registration": {
                "status": "passed",
                "diagnostics": {"answer_source_complete": True},
            },
            "identity": {
                "status": "teacher_confirmed",
                "student_id": student_id,
                "student_name": f"学生{index}",
            },
            "answers": [{
                "question_no": str(index),
                "answer": "A",
                "status": "teacher_confirmed",
            }],
        })
    first_draft = {
        "student_id": 101,
        "paper_ids": ["student_1"],
        "question_pages": {"1": ["student_1"]},
        "manual_corrections": [],
        "result": {
            "total": 1,
            "needs_review_count": 0,
            "details": [{"question_no": "1", "result": "correct"}],
        },
    }
    session.asset_lifecycle["grading_draft_checkpoint"] = {
        "review_revision": 7,
        "layout_hash": "layout-hash",
        "answer_spec_hash": "answer-hash",
        "student_ids": [101, 102],
        "drafts": [first_draft],
    }
    session.save()
    calls = []

    async def fail_second(_paper, _answers, student_name, *_args, **_kwargs):
        calls.append(student_name)
        raise TimeoutError("provider stalled")

    monkeypatch.setattr(grader, "grade_paper_unified", fail_second)
    failed = client.post(
        f"/api/answer-sheet/sessions/{session.session_id}/grading/draft/stream"
    )
    failed_events = _sse_events(failed)
    assert failed_events[-1][0] == "error"
    assert failed_events[-1][1]["code"] == "GRADING_CHAIN_FAILED"
    interrupted = AnswerSheetSession.load(session.session_id, upload_dir)
    checkpoint = interrupted.asset_lifecycle["grading_draft_checkpoint"]
    assert [item["student_id"] for item in checkpoint["drafts"]] == [101]
    assert "已保存完成学生" in interrupted.error

    async def finish_second(_paper, _answers, student_name, *_args, **_kwargs):
        calls.append(student_name)
        return {
            "total": 1,
            "needs_review_count": 0,
            "details": [{"question_no": "2", "result": "correct"}],
        }

    monkeypatch.setattr(grader, "grade_paper_unified", finish_second)
    resumed = client.post(
        f"/api/answer-sheet/sessions/{session.session_id}/grading/draft/stream"
    )
    assert _sse_events(resumed)[-1][0] == "done"
    assert calls == ["学生2", "学生2"]
    restored = AnswerSheetSession.load(session.session_id, upload_dir)
    assert [item["student_id"] for item in restored.grouping_decisions] == [101, 102]
    assert restored.review_revision == 8
    assert "grading_draft_checkpoint" not in restored.asset_lifecycle
    assert restored.error is None
    replay = client.post(
        f"/api/answer-sheet/sessions/{session.session_id}/grading/draft/stream"
    )
    assert _sse_events(replay)[-1][1]["idempotent"] is True
    assert calls == ["学生2", "学生2"]


def test_grading_draft_stream_uses_bounded_student_concurrency(
    client_env,
    monkeypatch,
):
    from routers import answer_sheet
    from services import grader

    make_client, upload_dir = client_env
    session = AnswerSheetSession("6" * 16, upload_dir, teacher_id=1, class_id=10)
    session.status = "review"
    session.layout_hash = "layout-hash"
    session.answer_spec_hash = "answer-hash"
    session.answer_spec_snapshot = [_question("1")]
    session.student_pages = [
        {
            "paper_id": f"student_{index}",
            "orientation_status": "teacher_confirmed",
            "review_reasons": [],
            "registration": {
                "status": "passed",
                "diagnostics": {"answer_source_complete": True},
            },
            "identity": {
                "status": "teacher_confirmed",
                "student_id": 200 + index,
                "student_name": f"学生{index}",
            },
            "answers": [{
                "question_no": "1",
                "answer": "A",
                "status": "teacher_confirmed",
            }],
        }
        for index in range(4)
    ]
    session.save()
    active = 0
    max_active = 0

    async def grade(_paper, _answers, student_name, *_args, **_kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.02)
            return {
                "student_name": student_name,
                "total": 1,
                "needs_review_count": 0,
                "details": [{"question_no": "1", "result": "correct"}],
            }
        finally:
            active -= 1

    monkeypatch.setattr(grader, "grade_paper_unified", grade)
    monkeypatch.setattr(answer_sheet_grading, "FLASH_LOCAL_MAX_CONCURRENT", 2)

    response = make_client(1).post(
        f"/api/answer-sheet/sessions/{session.session_id}/grading/draft/stream"
    )

    events = _sse_events(response)
    assert events[-1][0] == "done"
    assert max_active == 2
    assert sum(
        event == "progress" and payload.get("checkpoint") is True
        for event, payload in events
    ) == 4
    assert [draft["student_id"] for draft in events[-1][1]["drafts"]] == [
        200,
        201,
        202,
        203,
    ]
