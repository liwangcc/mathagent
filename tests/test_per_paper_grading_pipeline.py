"""逐卷流水线批阅：识别定稿一卷立即批一卷，不等全量识别。

契约（2026-09-27 用户裁定）：
- 某卷识别+配准定稿后立刻批该卷，其余卷继续识别；识别与批阅重叠。
- 单卷内「识别定稿→再批该卷」有序；身份未确认的卷不得静默归属（fail-closed），
  留给教师确认后由既有 confirm 流程补批。
- 结果带版本戳（rubric/template 哈希）；同一输入只一份有效结果，
  旧戳任务不能覆盖新结果（识别重试/模板返工后按新戳重批）。
"""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models  # noqa: F401
import models_user  # noqa: F401
import models_rate_limit  # noqa: F401
from database import Base, get_db

from models_user import User
from models_rate_limit import TeacherClass
from routers.auth import _require_user
from services.quick_grader import QuickBatch
from services.template_session import TemplateSession
from tests.grade_family import set_grade_global
from routers import grade_availability
from routers import grade_report
from routers import grade_papers


@pytest.fixture(autouse=True)
def _isolated_grading_dependencies(monkeypatch):
    import routers.grade as grade_router
    from routers import grade_pipeline
    from services import task_queue

    async def available(_batch):
        return {"status": "available", "message": "synthetic grading readiness"}

    async def no_report(_batch, _teacher_id):
        return None

    # Keep the real local queue/idempotency behavior, but isolate each test's
    # cache and avoid live Redis/model readiness or narrative-report requests.
    monkeypatch.setattr(task_queue, "_grading_queue", None)
    monkeypatch.setattr(grade_availability, "_batch_grade_scoring_availability", available)
    monkeypatch.setattr(grade_pipeline, "_start_batch_report_generation", no_report)
    monkeypatch.setattr(grade_report, "_start_batch_report_generation", no_report)


def _events(response):
    events = []
    current = None
    for line in response.text.splitlines():
        if line.startswith("event: "):
            current = line[7:]
        elif line.startswith("data: ") and current:
            events.append((current, json.loads(line[6:])))
            current = None
    return events


def _setup(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(User(id=1, name="老师", role="teacher", education_stage="senior"))
    db.add(TeacherClass(id=1, teacher_id=1, class_name="一班"))
    db.commit()

    def override_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    import routers.grade as grade_router
    from routers import grade_pipeline
    from routers import grade_review
    from routers import grade_template
    from services import grader as gr_svc

    test_app = FastAPI()
    test_app.include_router(grade_router.router, prefix="/api")
    test_app.include_router(grade_template.router, prefix="/api")
    test_app.include_router(grade_review.router, prefix="/api")
    auth_user = User(id=1, name="老师", role="teacher", current_class_id=1, education_stage="senior")
    test_app.dependency_overrides[get_db] = override_db
    test_app.dependency_overrides[_require_user] = lambda: auth_user
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_pipeline, "SessionLocal", Session)
    monkeypatch.setattr(grade_router, "SessionLocal", Session)
    set_grade_global(monkeypatch, "_validate_client_ocr_ready_upload", lambda filename, path: {"safe": True, "reason": ""})
    set_grade_global(monkeypatch, "validate_upload_path", lambda filename, content: {"safe": True, "reason": ""})
    set_grade_global(
        monkeypatch,
        "_client_ocr_ready_orientation",
        lambda _filename: {
            "orientation_status": "auto_passed",
            "orientation_reason": "client_ocr_ready_v1",
            "orientation_confidence": 1.0,
            "detected_tilt_degrees": 0.0,
            "exif_orientation": 1,
            "orientation_coordinate_space": "client_normalized_pixels",
        },
    )
    monkeypatch.setattr(grade_router, "_check_limits_or_raise", lambda db, user_id: None)

    async def available(_user):
        return {"status": "available", "message": "批改服务可用"}

    async def hardware_ready(_teacher_id, **_contract):
        return {"status": "ready", "message": "加速节点已就绪"}

    monkeypatch.setattr(grade_availability, "batch_grade_availability", available)
    monkeypatch.setattr(grade_availability, "get_public_hardware_readiness", hardware_ready)
    monkeypatch.setattr(grade_template, "get_public_hardware_readiness", hardware_ready)

    # 模型边界 mock：判分整卷调用不打真实模型。
    judge_calls = []

    async def fake_judge_paper(questions, detail_level="detailed", *, teacher_material=None):
        judge_calls.append([str(q.get("question_no") or "") for q in questions])
        return {str(q["question_no"]): {
            "result": "correct", "explanation": "", "hint": "",
            "model_answer": "", "standard_answer": q.get("standard_answer", ""),
            "error_type": "",
        } for q in questions}

    monkeypatch.setattr(gr_svc, "judge_paper", fake_judge_paper)
    return TestClient(test_app, raise_server_exceptions=False), Session, judge_calls


def _confirmed_template(tmp_path, template_id: str) -> TemplateSession:
    session = TemplateSession(template_id, str(tmp_path), teacher_id=1)
    session.status = "confirmed"
    session.category = "daily"
    session.add_page(0, [{
        "question_number": "1",
        "question_text": "1+1=?",
        "question_type": "fill",
        "standard_answer": "2",
    }])
    session.confirm_current_contract(1)
    session.save_meta()
    return session


def _recognize(client, template_id, files, source_paths, mode="auto"):
    return client.post("/api/batch-grade/recognize", files=[
        ("files", (name, f"synthetic-{index}".encode(), "image/jpeg"))
        for index, name in enumerate(files)
    ], data={
        "template_id": template_id,
        "mode": mode,
        "category": "daily",
        "source_paths": json.dumps(source_paths),
    })


@pytest.mark.parametrize("pending", ["correction_only", "answer_changed", "geometry_changed"])
def test_prepared_student_pages_wait_for_final_template_confirmation(monkeypatch, tmp_path, pending):
    client, _Session, judge_calls = _setup(monkeypatch, tmp_path)
    session = _confirmed_template(tmp_path, "pending-template")
    if pending == "correction_only":
        session.status = "ocr_done"
    elif pending == "answer_changed":
        session.pages[0]["questions"][0]["standard_answer"] = "3"
    else:
        session.page_artifacts["page_1"] = {
            "version": 1, "sha256": "1" * 64, "width": 100, "height": 100,
            "question_schema": [{"question_number": "1", "question_type": "fill"}],
            "question_numbers": ["1"],
        }
        session.confirm_current_contract(1)
        session.page_artifacts["page_1"]["question_anchors"] = []
    session.save_meta()

    import routers.grade as grade_router

    def no_comparison_before_confirmation(*_args, **_kwargs):
        pytest.fail("Student comparison/OCR started before the answer and geometry contracts were confirmed")

    set_grade_global(monkeypatch, "_build_template_paper_cache", no_comparison_before_confirmation)
    monkeypatch.setattr(grade_papers, "recognize_paper_with_cache", no_comparison_before_confirmation)
    response = _recognize(
        client, "pending-template", ["prepared.ocr-ready-v1.jpg"], ["prepared.jpg"],
    )
    assert response.status_code == (400 if pending == "correction_only" else 409), response.text
    assert judge_calls == []


def test_recognition_stream_pipelines_grading_per_paper(monkeypatch, tmp_path):
    """一张卷识别定稿立刻批这一张：快卷的结果先于慢卷识别完成出现。"""
    client, Session, judge_calls = _setup(monkeypatch, tmp_path)
    _confirmed_template(tmp_path, "pipeline-template")
    from models import Student
    import routers.grade as grade_router
    from routers import grade_recognition

    with Session() as db:
        db.add_all([
            Student(id=1, teacher_id=1, class_id=1, name="张三", student_no="01"),
            Student(id=2, teacher_id=1, class_id=1, name="李四", student_no="02"),
            Student(id=3, teacher_id=1, class_id=1, name="王五", student_no="03"),
        ])
        db.commit()

    async def blank_identity(*_args, **_kwargs):
        return {"name": "", "student_no": "", "confidence": 0.0, "candidate_names": [], "match_status": "no_match"}

    first_result = asyncio.Event()
    slow_wait_timed_out = []
    original_result_sse = grade_router._pipeline_result_sse

    def observe_result_sse(pipeline, graded):
        event = original_result_sse(pipeline, graded)
        if event.startswith("event: result\n"):
            first_result.set()
        return event

    async def recognized_answers(*args, **_kwargs):
        save_path = str(args[0]) if args else ""
        if "王五" in os.path.basename(save_path):
            # Hold this OCR until a fast paper's actual SSE result is emitted.
            # This also catches dispatchers that wait for the heartbeat timeout
            # before handling an already-completed OCR task.
            try:
                await asyncio.wait_for(first_result.wait(), timeout=5)
            except asyncio.TimeoutError:
                slow_wait_timed_out.append(True)
        return {"student_name": "", "student_no": "", "questions": [{
            "question_number": "1", "question_text": "1+1=?", "question_type": "fill", "student_answer": "2",
        }]}

    monkeypatch.setattr(grade_papers, "recognize_student_info_with_roster", blank_identity)
    monkeypatch.setattr(grade_papers, "recognize_paper_with_cache", recognized_answers)
    # The held OCR waits for a result streamed during the OCR phase itself.
    monkeypatch.setattr(grade_recognition, "_pipeline_result_sse", observe_result_sse)

    source_paths = ["收集.zip/张三/a.jpg", "收集.zip/李四/b.jpg", "收集.zip/王五/c.jpg"]
    response = _recognize(
        client, "pipeline-template",
        ["张三.ocr-ready-v1.jpg", "李四.ocr-ready-v1.jpg", "王五.ocr-ready-v1.jpg"],
        source_paths,
    )
    assert response.status_code == 200, response.text
    events = _events(response)

    assert not slow_wait_timed_out, "快卷批阅仍在等待慢卷 OCR 或心跳超时"
    result_indexes = [i for i, (event, _p) in enumerate(events) if event == "result"]
    assert result_indexes, f"识别流中没有逐卷批阅结果事件: {events}"
    slow_done_indexes = [
        i for i, (event, payload) in enumerate(events)
        if event == "progress" and "已识别 王五" in str(payload.get("message") or "")
    ]
    assert slow_done_indexes, f"缺少慢卷识别完成事件: {events}"
    # 逐卷流水的关键证据：快卷的批阅结果在慢卷识别完成前就已产出。
    assert min(result_indexes) < min(slow_done_indexes), (
        "批阅仍在等全量识别完成（结果事件排在慢卷识别完成之后）"
    )

    done = [payload for event, payload in events if event == "done"]
    assert len(done) == 1, response.text
    batch = QuickBatch.load_meta(done[0]["batch_id"], str(tmp_path))
    assert batch.status == "ocr_done"
    assert [q["question_no"] for q in batch.questions] == ["1"]

    # 同一输入只一份有效结果：逐卷结果按 paper_id 唯一。
    row_ids = [row.get("paper_id") for row in batch.results]
    assert len(row_ids) == len(set(row_ids)) == 3, row_ids
    review_ids = [row.get("paper_id") for row in batch.review_results]
    assert len(review_ids) == len(set(review_ids)) == 3, review_ids
    # 结果带版本戳（rubric/template 哈希）。
    assert batch.grading_rubric_hash
    for row in batch.results:
        assert row.get("grading_rubric_hash") == batch.grading_rubric_hash
        assert row.get("template_contract_hash") == batch.template_contract_hash
    # 判分被真正调用过（每卷一次整卷调用）。
    assert len(judge_calls) == 3, judge_calls


def test_grading_stopwatch_spans_pipeline_and_confirm(monkeypatch, tmp_path):
    """批阅计时从第一张卷开始判分起算（含识别中的逐卷批阅），到 review_ready 停表。"""
    client, _Session, judge_calls = _setup(monkeypatch, tmp_path)
    _confirmed_template(tmp_path, "stopwatch-template")
    import routers.grade as grade_router

    async def recognized_answers(*_args, **_kwargs):
        return {"student_name": "", "student_no": "", "questions": [{
            "question_number": "1", "question_text": "1+1=?", "question_type": "fill", "student_answer": "2",
        }]}

    monkeypatch.setattr(grade_papers, "recognize_paper_with_cache", recognized_answers)
    response = client.post("/api/batch-grade/recognize", files=[
        ("files", (f"page-{index}.ocr-ready-v1.jpg", f"synthetic-{index}".encode(), "image/jpeg"))
        for index in range(2)
    ], data={"template_id": "stopwatch-template", "mode": "auto", "category": "daily", "anonymous": "true"})
    assert response.status_code == 200, response.text
    done = [payload for event, payload in _events(response) if event == "done"]
    assert len(done) == 1, response.text
    batch = QuickBatch.load_meta(done[0]["batch_id"], str(tmp_path))
    assert len(judge_calls) == 2 and len(batch.results) == 2, "papers should be graded during recognition"
    assert batch.grading_started_at, "the first pipeline grade starts the stopwatch"
    assert batch.grading_elapsed_seconds is None, "the stopwatch runs until review_ready"

    # Backdate the recorded start: confirm must keep it, not restart the clock.
    started = datetime.now(timezone.utc) - timedelta(seconds=90)
    batch.grading_started_at = started.isoformat()
    batch.save_meta()

    confirm = client.post("/api/batch-grade/confirm", json={
        "batch_id": batch.batch_id,
        "mode": "auto",
        "category": "daily",
        "exam_name": "",
        "confirmed_answers": [],
        "identity_resolutions": [],
    })
    assert confirm.status_code == 200, confirm.text
    confirm_events = _events(confirm)
    progress = [payload for event, payload in confirm_events if event == "progress"]
    confirm_done = [payload for event, payload in confirm_events if event == "done"]
    assert len(confirm_done) == 1, confirm_events
    assert len(judge_calls) == 2, "confirm must replay pipeline results, not re-grade them"
    assert progress and 90 <= progress[0]["grading_elapsed_seconds"] < 150
    elapsed = confirm_done[0]["grading_elapsed_seconds"]
    assert 90 <= elapsed < 150

    finished = QuickBatch.load_meta(batch.batch_id, str(tmp_path))
    assert finished.status == "review_ready"
    assert finished.grading_started_at == started.isoformat()
    assert finished.grading_elapsed_seconds == elapsed
    review = client.get(f"/api/batch-grade/review/{batch.batch_id}").json()
    assert review["summary"]["grading_elapsed_seconds"] == elapsed

    replay = client.post("/api/batch-grade/confirm", json={
        "batch_id": batch.batch_id,
        "mode": "auto",
        "category": "daily",
        "exam_name": "",
        "confirmed_answers": [],
        "identity_resolutions": [],
    })
    replay_done = [payload for event, payload in _events(replay) if event == "done"]
    assert replay_done and replay_done[0]["idempotent"] is True
    assert replay_done[0]["grading_elapsed_seconds"] == elapsed


def test_pipeline_never_grades_papers_with_unresolved_identity(monkeypatch, tmp_path):
    """身份未确认的卷不批、不静默归属；教师确认身份后由 confirm 补批。"""
    client, Session, judge_calls = _setup(monkeypatch, tmp_path)
    _confirmed_template(tmp_path, "identity-template")
    import routers.grade as grade_router

    async def blank_identity(*_args, **_kwargs):
        return {"name": "", "student_no": "", "confidence": 0.0, "candidate_names": [], "match_status": "no_match"}

    async def recognized_answers(*_args, **_kwargs):
        return {"student_name": "", "student_no": "", "questions": [{
            "question_number": "1", "question_text": "1+1=?", "question_type": "fill", "student_answer": "2",
        }]}

    monkeypatch.setattr(grade_papers, "recognize_student_info_with_roster", blank_identity)
    monkeypatch.setattr(grade_papers, "recognize_paper_with_cache", recognized_answers)

    response = _recognize(
        client, "identity-template",
        ["陌生文件甲.ocr-ready-v1.jpg"],
        ["收集.zip/随机目录/陌生文件甲.jpg"],
    )
    assert response.status_code == 200, response.text
    events = _events(response)
    assert not [p for e, p in events if e == "result"], (
        "身份未确认的卷被静默批阅/归属了"
    )
    assert judge_calls == [], "身份未确认的卷触发了判分模型调用"

    done = [payload for event, payload in events if event == "done"]
    assert len(done) == 1, response.text
    batch = QuickBatch.load_meta(done[0]["batch_id"], str(tmp_path))
    assert batch.status == "ocr_done"
    assert batch.results == [] and batch.review_results == []
    paper_id = batch.papers[0]["paper_id"]

    confirm = client.post("/api/batch-grade/confirm", json={
        "batch_id": batch.batch_id,
        "mode": "auto",
        "category": "daily",
        "exam_name": "",
        "confirmed_answers": [],
        "identity_resolutions": [{
            "paper_id": paper_id,
            "action": "create",
            "confirmed_name": "新同学",
            "confirmed_student_no": "",
        }],
    })
    assert confirm.status_code == 200, confirm.text
    confirm_events = _events(confirm)
    rows = [p for e, p in confirm_events if e == "result"]
    assert rows and rows[0]["student_name"] == "新同学", confirm_events
    assert len(judge_calls) == 1, judge_calls


def test_stale_stamped_rows_are_not_replayed_over_new_results(monkeypatch, tmp_path):
    """旧版本戳的结果不回放、不覆盖：按当前戳重批；同戳结果保持幂等回放。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    db = Session()
    db.add(User(id=1, name="老师", role="teacher"))
    db.add(TeacherClass(id=1, teacher_id=1, class_name="一班"))
    db.add(models.Student(id=10, teacher_id=1, class_id=1, name="张三", student_no="001"))
    db.commit()
    db.close()

    import routers.grade as gr_router
    from routers import grade_pipeline
    from routers import grade_review
    from routers import grade_template
    from services import grader as gr_svc

    upload_dir = tmp_path / "uploads"
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(upload_dir))
    monkeypatch.setattr(grade_pipeline, "SessionLocal", Session)
    monkeypatch.setattr(gr_router, "SessionLocal", Session)
    paper_path = upload_dir / "quick_batch_stamped" / "p1.png"
    paper_path.parent.mkdir(parents=True)
    paper_path.write_bytes(b"paper-bytes")
    paper2_path = upload_dir / "quick_batch_stamped" / "p2.png"
    paper2_path.write_bytes(b"paper-bytes-2")

    def _paper(paper_id, filename, path):
        return {
            "paper_id": paper_id,
            "filename": filename,
            "student_name": "张三",
            "student_no": "001",
            "identity": {
                "status": "matched", "student_id": 10, "reason": "unique_student_no",
                "ocr_name": "张三", "ocr_student_no": "001", "candidates": [],
            },
            "image_path": str(path),
            "orientation_status": "auto_passed",
            "orientation_reason": "normal_angle",
            "questions": [{"question_no": "1", "student_answer": "2", "question_type": "fill"}],
        }

    def _row(paper_id, stamp):
        return {
            "review_id": paper_id,
            "paper_id": paper_id,
            "student_name": "张三",
            "grading_result_id": None,
            "profile_id": None,
            "score": None,
            "max_score": None,
            "correct_rate": 1.0,
            "category": "daily",
            "exam_name": "",
            "grading_mode": "auto",
            "correct": 1,
            "partial": 0,
            "wrong": 0,
            "needs_review": 0,
            "total": 1,
            "details": [],
            "idempotent": False,
            "needs_name_confirm": False,
            "disposition": "active",
            "grading_rubric_hash": stamp,
            "template_contract_hash": "current-template",
        }

    batch = QuickBatch("stamped", str(upload_dir))
    batch.teacher_id = 1
    batch.class_id = 1
    batch.source_mode = "template"
    batch.status = "ocr_done"
    batch.papers = [_paper("paper-1", "p1.png", paper_path), _paper("paper-2", "p2.png", paper2_path)]
    batch.questions = [{"question_no": "1", "question_text": "1+1=?", "question_type": "fill"}]
    batch.answer_map = {
        "1": {
            "matched": True,
            "standard_answer": "2",
            "answer_variants": [],
            "solution_steps": [],
            "grading_method": "equivalent",
            "question_type": "fill",
            "question_text": "1+1=?",
            "question_bank_id": None,
            "chapter": "",
            "topic": "",
            "difficulty": 3,
            "source": "template_confirmed",
        }
    }
    batch.grading_rubric_hash = "current-rubric"
    batch.template_contract_hash = "current-template"
    batch.results = [_row("paper-1", "stale-rubric"), _row("paper-2", "current-rubric")]
    batch.review_results = [
        {**_row("paper-1", "stale-rubric"), "result": {"details": []}},
        {**_row("paper-2", "current-rubric"), "result": {"details": []}},
    ]
    batch.save_meta()

    app = FastAPI()
    app.include_router(gr_router.router, prefix="/api")
    app.include_router(grade_template.router, prefix="/api")
    app.include_router(grade_review.router, prefix="/api")

    def _override_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[_require_user] = lambda: User(id=1, name="老师", role="teacher", current_class_id=1)
    client = TestClient(app, raise_server_exceptions=False)

    judge_calls = []

    async def _mock_judge_paper(questions, detail_level="detailed", *, teacher_material=None):
        judge_calls.append([str(q.get("question_no") or "") for q in questions])
        return {str(q["question_no"]): {
            "result": "correct", "explanation": "", "hint": "",
            "model_answer": "", "standard_answer": q.get("standard_answer", ""),
            "error_type": "",
        } for q in questions}

    monkeypatch.setattr(gr_svc, "judge_paper", _mock_judge_paper)

    async def confirm_and_close_after_done():
        request_db = Session()
        try:
            response = await gr_router.batch_grade_confirm(
                gr_router.ConfirmReq.model_validate({
                    "batch_id": "stamped",
                    "mode": "auto",
                    "category": "daily",
                    "exam_name": "",
                    "confirmed_answers": [{
                        "question_no": "1", "content": "1+1=?", "answer": "2",
                        "question_type": "fill", "grading_method": "equivalent",
                    }],
                }),
                User(id=1, name="老师", role="teacher", current_class_id=1),
                request_db,
            )
            iterator = response.body_iterator
            chunks = []
            async for chunk in iterator:
                text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
                chunks.append(text)
                if text.startswith("event: done\n"):
                    await iterator.aclose()
                    return _events_from_text("".join(chunks))
            raise AssertionError("batch grading did not emit done")
        finally:
            request_db.close()

    def _events_from_text(text):
        events = []
        current = None
        for line in text.splitlines():
            if line.startswith("event: "):
                current = line[7:]
            elif line.startswith("data: ") and current:
                events.append((current, json.loads(line[6:])))
                current = None
        return events

    events = asyncio.run(confirm_and_close_after_done())
    rows = {p.get("paper_id"): p for e, p in events if e == "result"}
    # 同戳结果幂等回放；旧戳结果按当前戳重批，不得以旧盖新。
    assert rows["paper-2"]["idempotent"] is True
    assert rows["paper-1"]["idempotent"] is False
    assert rows["paper-1"]["grading_rubric_hash"] == "current-rubric"
    assert rows["paper-1"]["template_contract_hash"] == "current-template"
    # 只有旧戳那一张真正重批（防重复批）。
    assert len(judge_calls) == 1, judge_calls

    staged = QuickBatch.load_meta("stamped", str(upload_dir))
    by_id = {row["paper_id"]: row for row in staged.results}
    assert by_id["paper-1"]["grading_rubric_hash"] == "current-rubric"
    assert by_id["paper-2"]["grading_rubric_hash"] == "current-rubric"


def _two_page_template(tmp_path, template_id: str) -> TemplateSession:
    session = TemplateSession(template_id, str(tmp_path), teacher_id=1)
    session.status = "confirmed"
    session.category = "daily"
    for index, (number, text, answer) in enumerate([("1", "1+1=?", "2"), ("2", "2+2=?", "4")]):
        session.add_page(index, [{
            "question_number": number,
            "question_text": text,
            "question_type": "fill",
            "standard_answer": answer,
        }])
    session.confirm_current_contract(1)
    session.save_meta()
    return session


@pytest.mark.parametrize("inherit", [True, False])
def test_blank_pages_inherit_the_previous_page_identity_only_when_enabled(monkeypatch, tmp_path, inherit):
    """Opt-in: pages with no name take the previous page's student, in file-name order."""
    client, Session, judge_calls = _setup(monkeypatch, tmp_path)
    _two_page_template(tmp_path, "inherit-template")
    from models import Student
    import routers.grade as grade_router
    from routers import grade_recognition
    from routers import grade_review

    with Session() as db:
        db.add_all([
            Student(id=1, teacher_id=1, class_id=1, name="张三", student_no="01"),
            Student(id=2, teacher_id=1, class_id=1, name="李四", student_no="02"),
        ])
        db.commit()

    written = {"scan_1": "张三", "scan_3": "李四"}

    def page_name(image_path):
        with open(image_path, "rb") as handle:
            return handle.read().decode()

    async def identity(image_path, _roster, **_kwargs):
        name = written.get(page_name(image_path), "")
        return {
            "name": name, "student_no": "", "confidence": 0.99 if name else 0.0,
            "candidate_names": [name] if name else [],
            "match_status": "unique_high_confidence" if name else "no_match",
            "name_written": bool(name),
        }

    follower_ingested = asyncio.Event()
    anchor_wait_timed_out = []
    original_ingest = grade_router._ingest_recognized_paper

    def observe_ingest(batch, *args, **kwargs):
        result = original_ingest(batch, *args, **kwargs)
        if any(page_name(paper["image_path"]) == "scan_2" for paper in batch.papers):
            follower_ingested.set()
        return result

    async def answers(image_path, *_args, **_kwargs):
        if inherit and page_name(image_path) == "scan_1":
            # The follower is recognized first; it must still be graded once
            # its anchor arrives (the dispatch_inherited path).
            try:
                await asyncio.wait_for(follower_ingested.wait(), timeout=5)
            except asyncio.TimeoutError:
                anchor_wait_timed_out.append(True)
        page = 1 if page_name(image_path) in written else 2
        return {"student_name": "", "student_no": "", "_cache_type_label": f"page_{page}", "questions": [{
            "question_number": str(page), "question_text": "", "question_type": "fill",
            "student_answer": "2" if page == 1 else "4",
        }]}

    monkeypatch.setattr(grade_papers, "recognize_student_info_with_roster", identity)
    monkeypatch.setattr(grade_papers, "recognize_paper_with_cache", answers)
    set_grade_global(monkeypatch, "_ingest_recognized_paper", observe_ingest)
    monkeypatch.setattr(grade_recognition, "OCR_BATCH_DISPATCH_CONCURRENT", 5)

    # The browser order is shuffled; inheritance must follow the file names.
    names = ["scan_4", "scan_2", "scan_10", "scan_3", "scan_1"]
    written["scan_10"] = ""  # a later blank page_1: someone else forgot a name
    response = client.post("/api/batch-grade/recognize", files=[
        ("files", (f"{name}.ocr-ready-v1.jpg", name.encode(), "image/jpeg")) for name in names
    ], data={
        "template_id": "inherit-template", "mode": "auto", "category": "daily",
        "source_paths": json.dumps([f"{name}.jpg" for name in names]),
        **({"inherit_identity": "true"} if inherit else {}),
    })
    assert response.status_code == 200, response.text
    assert not anchor_wait_timed_out, "the follower page was not recognized before its anchor"
    done = [payload for event, payload in _events(response) if event == "done"]
    assert len(done) == 1, response.text
    batch = QuickBatch.load_meta(done[0]["batch_id"], str(tmp_path))
    assert batch.identity_inheritance is inherit
    by_name = {
        upload["source_relative_path"]: next(p for p in batch.papers if p["paper_id"] == upload["paper_id"])
        for upload in batch.uploads
    }
    if not inherit:
        assert [upload["source_relative_path"] for upload in batch.uploads] == [f"{name}.jpg" for name in names]
        assert by_name["scan_2.jpg"]["identity"]["status"] == "needs_confirmation"
        assert {row["paper_id"] for row in batch.results} == {
            by_name["scan_1.jpg"]["paper_id"], by_name["scan_3.jpg"]["paper_id"],
        }
        return

    assert [upload["source_relative_path"] for upload in batch.uploads] == [
        "scan_1.jpg", "scan_2.jpg", "scan_3.jpg", "scan_4.jpg", "scan_10.jpg",
    ]
    assert by_name["scan_2.jpg"]["identity"]["student_id"] == 1
    assert by_name["scan_2.jpg"]["identity"]["inherited_from"]["paper_id"] == by_name["scan_1.jpg"]["paper_id"]
    assert by_name["scan_2.jpg"]["student_name"] == "张三"
    assert by_name["scan_4.jpg"]["identity"]["student_id"] == 2
    # Li Si already has a page_1: a second blank page_1 is never folded in.
    blocked = by_name["scan_10.jpg"]["identity"]
    assert blocked["status"] == "needs_confirmation" and blocked["inherit_blocked"] == "same_page_type"

    # Inherited pages are graded during recognition like auto-matched ones.
    graded = {row["paper_id"]: row["student_name"] for row in batch.results}
    assert graded == {
        by_name["scan_1.jpg"]["paper_id"]: "张三", by_name["scan_2.jpg"]["paper_id"]: "张三",
        by_name["scan_3.jpg"]["paper_id"]: "李四", by_name["scan_4.jpg"]["paper_id"]: "李四",
    }
    assert len(judge_calls) == 4, judge_calls

    blocked_id = by_name["scan_10.jpg"]["paper_id"]
    confirm = client.post("/api/batch-grade/confirm", json={
        "batch_id": batch.batch_id, "mode": "auto", "category": "daily", "exam_name": "",
        "confirmed_answers": [],
        "identity_resolutions": [{"paper_id": blocked_id, "action": "create", "confirmed_name": "王五"}],
    })
    assert confirm.status_code == 200, confirm.text
    assert [e for e, _p in _events(confirm)][-1] == "done", confirm.text
    batch = QuickBatch.load_meta(batch.batch_id, str(tmp_path))
    contracts = grade_review._review_page_contracts(batch)
    assert contracts[by_name["scan_2.jpg"]["paper_id"]]["page_count"] == 2
    assert [page["paper_id"] for page in contracts[by_name["scan_4.jpg"]["paper_id"]]["pages"]] == [
        by_name["scan_3.jpg"]["paper_id"], by_name["scan_4.jpg"]["paper_id"],
    ]
    assert contracts[blocked_id]["page_count"] == 1
