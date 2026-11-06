import asyncio
import json

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
from models import GradingResult, KnowledgeEvent, QuestionBank, Student, StudentProfile, Weakness
from models_user import User
from routers.auth import _require_user
from services.quick_grader import QuickBatch, batch_operation_lock
from tests.grade_family import set_grade_global


@pytest.fixture(autouse=True)
def _ready_batch_grade_chain(monkeypatch):
    async def available(_user):
        return {"status": "available", "message": "批改服务可用"}

    monkeypatch.setattr(
        "routers.grade.batch_grade_availability",
        available,
    )
    monkeypatch.setattr(
        "routers.grade._batch_grade_scoring_availability",
        available,
    )


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """Parse `event: X\\ndata: {...}` SSE blocks into [(event, payload)]."""
    events = []
    cur_event = None
    for line in text.splitlines():
        if line.startswith("event:"):
            cur_event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            payload = json.loads(line[len("data:"):].strip())
            events.append((cur_event, payload))
    return events


def test_grading_contract_hash_is_semantic_and_order_independent():
    from routers.grade import _build_grading_contract, _grading_contract_hash
    from services.grading_contract import AnswerSpec

    first = AnswerSpec(question_no="1", question_text="1+1=?", standard_answer="2")
    second = AnswerSpec(question_no="10", question_text="2+2=?", standard_answer="4")
    original = _build_grading_contract(
        mode="precise",
        category="daily",
        exam_name="  ",
        confirmed=[second, first],
        identity_plan={
            "paper-1": {
                "action": "existing", "student_id": 10,
                "name": "old-name", "student_no": "001",
            },
        },
    )
    reordered_after_roster_edit = _build_grading_contract(
        mode="precise",
        category="daily",
        exam_name="",
        confirmed=[first, second],
        identity_plan={
            "paper-1": {
                "action": "existing", "student_id": 10,
                "name": "new-name", "student_no": "009",
            },
        },
    )

    assert [item["question_no"] for item in original["confirmed"]] == ["1", "10"]
    assert _grading_contract_hash(original) == _grading_contract_hash(
        reordered_after_roster_edit
    )


@pytest.mark.parametrize("kind", ["drawing", "mixed"])
def test_frozen_batch_contract_preserves_graphic_routing_and_reference(kind):
    from copy import deepcopy
    from routers.grade import (_build_grading_contract, _grading_contract_hash,
                               _contract_answer_specs, _confirmation_questions_from_contract)
    from services.grading_contract import AnswerSpec
    from services.drawing_reference import normalize_reference
    reference = normalize_reference({
        "version": "drawing-reference-v1", "coordinate_space": "normalized_1000_v1",
        "comparison_mode": "fixed", "variants": [{"strokes": [{"kind": "point", "point": [800, 400]}]}],
        "context_regions": [],
    }, trusted_confirmed=True)
    answer = AnswerSpec(question_no="2.3", question_text="Mark the fourth vertex", standard_answer="(6,5)",
                        directory_node_id="n008", directory_parent_id="n003", printed_path=["2", "3"],
                        answer_kind=kind, drawing_regions=[[700, 300, 900, 500]], label_anchor=[690, 400],
                        drawing_reference=reference)
    contract = _build_grading_contract(mode="precise", category="daily", exam_name="", confirmed=[answer], identity_plan={})
    # This is the exact freeze/reload/map transition in batch_grade_confirm.
    restored = _contract_answer_specs(json.loads(json.dumps(contract)))[0]
    entry = restored.to_answer_map_entry()
    assert entry["answer_kind"] == kind
    assert entry["directory_node_id"] == "n008"
    assert entry["drawing_regions"] == answer.drawing_regions
    assert entry["drawing_reference"] == reference
    assert _confirmation_questions_from_contract(contract)[0]["drawing_reference"] == reference
    altered = deepcopy(contract)
    altered["confirmed"][0]["drawing_regions"][0][0] = 650
    assert _grading_contract_hash(altered) != _grading_contract_hash(contract)
    altered = deepcopy(contract)
    altered["confirmed"][0]["drawing_reference"]["variants"][0]["strokes"][0]["point"][0] = 810
    assert _grading_contract_hash(altered) != _grading_contract_hash(contract)


def test_load_meta_does_not_requeue_interrupted_second_ocr_attempt(tmp_path):
    batch = QuickBatch("ocr-attempts-exhausted", str(tmp_path))
    image_path = tmp_path / "paper.png"
    batch.ensure_ocr_jobs([{
        "paper_id": "paper-1",
        "filename": "paper.png",
        "image_path": str(image_path),
    }])
    batch.ocr_jobs[0].update({
        "status": "running",
        "attempts": 2,
        "retryable": True,
        "error_code": "provider_timeout",
    })
    batch.save_meta()

    restored = QuickBatch.load_meta(batch.batch_id, str(tmp_path))

    assert restored.ocr_jobs[0]["status"] == "running"
    with batch_operation_lock(batch.batch_id, str(tmp_path)):
        restored.recover_interrupted_ocr_jobs()
    assert restored.ocr_jobs[0]["status"] == "failed"
    assert restored.ocr_jobs[0]["attempts"] == 2
    assert restored.ocr_jobs[0]["retryable"] is False
    assert restored.ocr_jobs[0]["last_error"] == "服务中断且自动识别次数已用尽"
    assert (
        restored.ocr_jobs[0]["error_code"]
        == "worker_interrupted_attempts_exhausted"
    )


def test_batch_grade_confirm_stages_results_before_archive(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    db = Session()
    db.add(User(id=1, name="老师", role="teacher"))
    from models_rate_limit import TeacherClass
    db.add(TeacherClass(id=1, teacher_id=1, class_name="一班"))
    db.add(Student(id=10, teacher_id=1, class_id=1, name="张三", student_no="001"))
    db.commit()
    db.close()

    import routers.grade as gr_router
    from routers import grade_pipeline
    from routers import grade_review
    from routers import grade_template
    import services.grading_persistence as gp
    from services import grader as gr_svc

    upload_dir = tmp_path / "uploads"
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(upload_dir))
    # SSE 生成器自建会话（脱离请求作用域），需指向测试内存库
    monkeypatch.setattr(grade_pipeline, "SessionLocal", Session)
    monkeypatch.setattr(gr_router, "SessionLocal", Session)
    monkeypatch.setattr(gp, "ARCHIVE_DIR", str(tmp_path / "archive"))
    paper_path = upload_dir / "quick_batch_batch1" / "p1.png"
    paper_path.parent.mkdir(parents=True)
    paper_path.write_bytes(b"paper-bytes")

    batch = QuickBatch("batch1", str(upload_dir))
    batch.teacher_id = 1
    batch.class_id = 1
    batch.source_mode = "template"
    batch.template_id = "staging-template"
    batch.status = "ocr_done"
    batch.papers = [{
        "paper_id": "paper-1",
        "filename": "p1.png",
        "student_name": "张三",
        "student_no": "001",
        "identity": {
            "status": "matched", "student_id": 10, "reason": "unique_student_no",
            "ocr_name": "张三", "ocr_student_no": "001", "candidates": [],
        },
        "image_path": str(paper_path),
        "orientation_status": "auto_passed",
        "orientation_reason": "normal_angle",
        "questions": [{"question_no": "1", "student_answer": "2", "question_type": "fill"}],
    }]
    batch.questions = [{"question_no": "1", "question_text": "1+1=?", "question_type": "fill"}]
    batch.answer_map = {
        "1": {
            "matched": True,
            "standard_answer": "2",
            "answer_variants": ["2.0"],
            "solution_steps": ["先计算左边"],
            "grading_method": "equivalent",
            "question_type": "fill",
            "question_text": "1+1=?",
            "chapter": "代数",
            "topic": "加法",
            "difficulty": 2,
            "source": "template_confirmed",
        }
    }
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

    # mock judge_paper：非选择题（此处 solve）走 LLM，simple 模式只是输出简版，仍调 judge_paper
    async def _mock_judge_paper(questions, detail_level="detailed", *, teacher_material=None):
        return {str(q["question_no"]): {
            "result": "correct", "explanation": "", "hint": "",
            "model_answer": "", "standard_answer": q.get("standard_answer", ""),
            "error_type": "",
        } for q in questions}
    monkeypatch.setattr(gr_svc, "judge_paper", _mock_judge_paper)

    # A duplicated question number in the frozen snapshot is rejected before
    # anything is written.
    duplicated = QuickBatch.load_meta("batch1", str(upload_dir))
    duplicated.questions = batch.questions + [dict(batch.questions[0])]
    duplicated.save_meta()
    duplicate = client.post("/api/batch-grade/confirm", json={
        "batch_id": "batch1", "mode": "auto", "category": "daily",
    })
    assert duplicate.status_code == 409
    assert "重复题号" in duplicate.json()["detail"]
    batch.save_meta()

    # auto 模式：选择题机械比对、非选择题走 LLM（此处 judge_paper 已 mock）
    confirm_payload = {
        "batch_id": "batch1",
        "mode": "auto",
        "category": "daily",
        "exam_name": "",
    }

    async def confirm_and_close_after_done():
        request_db = Session()
        try:
            response = await gr_router.batch_grade_confirm(
                gr_router.ConfirmReq.model_validate(confirm_payload),
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
                    return _parse_sse("".join(chunks))
            raise AssertionError("batch grading did not emit done")
        finally:
            request_db.close()

    events = asyncio.run(confirm_and_close_after_done())
    result_events = [p for e, p in events if e == "result"]
    assert result_events, f"no result event: {events}"
    assert result_events[0]["review_id"] == "paper-1"
    assert result_events[0]["grading_result_id"] is None
    assert result_events[0]["profile_id"] is None
    assert result_events[0]["grading_mode"] == "auto"
    assert result_events[0]["details"][0]["question_no"] == "1"
    done_events = [p for e, p in events if e == "done"]
    assert done_events and done_events[0]["review_revision"] == 0

    db = Session()
    # Formal student records, archive profiles and learning analytics must
    # wait for archive-all; grading writes nothing to the private bank.
    assert db.query(GradingResult).count() == 0
    assert db.query(StudentProfile).count() == 0
    assert db.query(KnowledgeEvent).count() == 0
    assert db.query(Weakness).count() == 0
    assert db.query(QuestionBank).count() == 0
    db.close()

    staged = QuickBatch.load_meta("batch1", str(upload_dir))
    assert staged.status == "review_ready"
    assert len(staged.review_results) == 1
    assert staged.review_results[0]["disposition"] == "active"
    assert staged.review_results[0]["result"]["score"] is None
    assert staged.review_results[0]["result"]["correct_rate"] == 1.0
    assert staged.grading_contract["version"] == 1
    assert staged.grading_contract["confirmed"][0]["question_text"] == "1+1=?"
    assert staged.grading_contract["identity_plan"]["paper-1"]["student_id"] == 10
    assert staged.confirmation_questions[0]["content"] == "1+1=?"
    assert staged.confirmation_questions[0]["answer_variants"] == ["2.0"]

    # 作废后即使前端因 SSE/网络重试再次提交 confirm，也必须回放原暂存结果，
    # 绝不能重新判分并把 voided 变回 active。
    voided = client.post("/api/batch-grade/review/void-all", json={"batch_id": "batch1"})
    assert voided.status_code == 200
    changed_replay = client.post("/api/batch-grade/confirm", json={
        "batch_id": "batch1", "mode": "precise", "category": "daily", "exam_name": "",
    })
    assert changed_replay.status_code == 200
    changed_errors = [
        payload for event, payload in _parse_sse(changed_replay.text)
        if event == "error"
    ]
    assert changed_errors[0]["code"] == "GRADING_CONTRACT_CHANGED"

    replay = client.post("/api/batch-grade/confirm", json={
        "batch_id": "batch1", "mode": "auto", "category": "daily", "exam_name": "",
    })
    assert replay.status_code == 200
    replay_rows = [payload for event, payload in _parse_sse(replay.text) if event == "result"]
    assert len(replay_rows) == 1
    assert replay_rows[0]["disposition"] == "voided"
    replayed = QuickBatch.load_meta("batch1", str(upload_dir))
    assert replayed.status == "voided"
    assert replayed.review_results[0]["disposition"] == "voided"
    db = Session()
    assert db.query(GradingResult).count() == 0
    db.close()


def test_confirm_checkpoints_each_paper_and_resumes_only_unfinished(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    from models_rate_limit import TeacherClass

    db = Session()
    db.add(User(id=1, name="teacher", role="teacher"))
    db.add(TeacherClass(id=1, teacher_id=1, class_name="class"))
    db.add_all([
        Student(id=10, teacher_id=1, class_id=1, name="student-a", student_no="001"),
        Student(id=11, teacher_id=1, class_id=1, name="student-b", student_no="002"),
        Student(id=12, teacher_id=1, class_id=1, name="student-c", student_no="003"),
    ])
    db.commit()
    db.close()

    import routers.grade as gr_router
    from routers import grade_pipeline
    from routers import grade_review
    from routers import grade_template
    from services.grader import GradingModelUnavailable

    upload_dir = tmp_path / "uploads"
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(upload_dir))
    monkeypatch.setattr(grade_pipeline, "SessionLocal", Session)
    monkeypatch.setattr(gr_router, "SessionLocal", Session)
    monkeypatch.setattr(grade_pipeline, "FLASH_LOCAL_MAX_CONCURRENT", 2)

    batch = QuickBatch("resume-batch", str(upload_dir))
    batch.teacher_id = 1
    batch.class_id = 1
    batch.source_mode = "template"
    batch.template_id = "resume-template"
    batch.status = "ocr_done"
    batch.questions = [{
        "question_no": "1", "question_text": "1+1=?", "question_type": "fill",
    }]
    batch.answer_map = {
        "1": {
            "matched": True,
            "standard_answer": "2",
            "question_text": "1+1=?",
            "question_type": "fill",
            "source": "template_confirmed",
        },
    }
    for index, (student_id, name, student_no) in enumerate((
        (10, "student-a", "001"),
        (11, "student-b", "002"),
        (12, "student-c", "003"),
    ), start=1):
        image_path = upload_dir / "quick_batch_resume-batch" / f"p{index}.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(f"paper-{index}".encode())
        batch.papers.append({
            "paper_id": f"paper-{index}",
            "filename": image_path.name,
            "student_name": name,
            "student_no": student_no,
            "identity": {
                "status": "matched",
                "student_id": student_id,
                "reason": "unique_student_no",
                "ocr_name": name,
                "ocr_student_no": student_no,
                "candidates": [],
            },
            "image_path": str(image_path),
            "orientation_status": "auto_passed",
            "orientation_reason": "normal_angle",
            "questions": [{
                "question_no": "1", "student_answer": "2", "question_type": "fill",
            }],
        })
    batch.save_meta()

    app = FastAPI()
    app.include_router(gr_router.router, prefix="/api")
    app.include_router(grade_template.router, prefix="/api")
    app.include_router(grade_review.router, prefix="/api")

    def _override_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[_require_user] = lambda: User(
        id=1, name="teacher", role="teacher", current_class_id=1,
    )
    client = TestClient(app, raise_server_exceptions=False)

    calls = []

    async def fail_first_after_second_finishes(*, student_name, **_kwargs):
        calls.append(student_name)
        if student_name == "student-a":
            await asyncio.sleep(0.04)
            raise GradingModelUnavailable("provider unavailable")
        await asyncio.sleep(0.005)
        return {
            "score": None,
            "max_score": None,
            "correct_rate": 1.0,
            "correct_count": 1,
            "partial_count": 0,
            "wrong_count": 0,
            "needs_review_count": 0,
            "total": 1,
            "details": [{"question_no": "1", "result": "correct"}],
        }

    monkeypatch.setattr(
        grade_pipeline,
        "grade_paper_unified",
        fail_first_after_second_finishes,
    )
    payload = {
        "batch_id": "resume-batch",
        "mode": "auto",
        "category": "daily",
        "exam_name": "",
    }

    first = client.post("/api/batch-grade/confirm", json=payload)
    first_events = _parse_sse(first.text)
    errors = [data for event, data in first_events if event == "error"]
    # The provider failure stays the one paper's: the run reports it as pending,
    # not as a broken grading chain.
    assert [error["code"] for error in errors] == ["GRADING_PAPERS_PENDING"]
    assert [failure["paper_id"] for failure in errors[0]["failures"]] == ["paper-1"]
    interrupted = QuickBatch.load_meta("resume-batch", str(upload_dir))
    assert interrupted.status == "grading_interrupted"
    # Other papers continue after one paper exhausts its own retry path.
    assert [row["paper_id"] for row in interrupted.results] == ["paper-2", "paper-3"]
    assert calls == ["student-a", "student-b", "student-c"]

    # Roster labels are mutable. Resume must compare the stable student_id and
    # keep using the full identity assignment frozen with the partial results.
    db = Session()
    renamed = db.query(Student).filter(Student.id == 10).one()
    renamed.name = "student-a-renamed"
    renamed.student_no = "009"
    db.commit()
    db.close()

    calls.clear()

    async def finish_remaining(*, student_name, **_kwargs):
        calls.append(student_name)
        return {
            "score": None,
            "max_score": None,
            "correct_rate": 1.0,
            "correct_count": 1,
            "partial_count": 0,
            "wrong_count": 0,
            "needs_review_count": 0,
            "total": 1,
            "details": [{"question_no": "1", "result": "correct"}],
        }

    monkeypatch.setattr(grade_pipeline, "grade_paper_unified", finish_remaining)
    resumed = client.post("/api/batch-grade/confirm", json=payload)
    resumed_events = _parse_sse(resumed.text)
    result_rows = [data for event, data in resumed_events if event == "result"]
    assert [row["paper_id"] for row in result_rows] == ["paper-2", "paper-3", "paper-1"]
    assert result_rows[0]["idempotent"] is True
    assert calls == ["student-a"]
    completed = QuickBatch.load_meta("resume-batch", str(upload_dir))
    assert completed.status == "review_ready"
    assert [row["paper_id"] for row in completed.results] == ["paper-1", "paper-2", "paper-3"]


def _one_paper_grading_client(monkeypatch, tmp_path, batch_id: str):
    """A template batch with one OCR-complete paper and a client for /confirm."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    from models_rate_limit import TeacherClass
    import routers.grade as gr_router
    from routers import grade_pipeline
    from routers import grade_review
    from routers import grade_template

    db = Session()
    db.add(User(id=1, name="teacher", role="teacher"))
    db.add(TeacherClass(id=1, teacher_id=1, class_name="class"))
    db.add(Student(id=10, teacher_id=1, class_id=1, name="student", student_no="001"))
    db.commit()
    db.close()

    upload_dir = tmp_path / "uploads"
    paper_path = upload_dir / f"quick_batch_{batch_id}" / "paper.png"
    paper_path.parent.mkdir(parents=True)
    paper_path.write_bytes(b"paper")
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(upload_dir))
    monkeypatch.setattr(grade_pipeline, "SessionLocal", Session)
    monkeypatch.setattr(gr_router, "SessionLocal", Session)

    batch = QuickBatch(batch_id, str(upload_dir))
    batch.teacher_id = 1
    batch.class_id = 1
    batch.source_mode = "template"
    batch.template_id = f"{batch_id}-template"
    batch.status = "ocr_done"
    batch.questions = [{
        "question_no": "1",
        "question_text": "1+1=?",
        "question_type": "fill",
    }]
    batch.answer_map = {"1": {
        "matched": True,
        "standard_answer": "2",
        "question_text": "1+1=?",
        "question_type": "fill",
    }}
    batch.papers = [{
        "paper_id": "paper-1",
        "filename": "paper.png",
        "student_name": "student",
        "student_no": "001",
        "identity": {
            "status": "matched",
            "student_id": 10,
            "reason": "unique_student_no",
            "ocr_name": "student",
            "ocr_student_no": "001",
            "candidates": [],
        },
        "image_path": str(paper_path),
        "orientation_status": "auto_passed",
        "orientation_reason": "normal_angle",
        "questions": [{
            "question_no": "1",
            "student_answer": "2",
            "question_type": "fill",
        }],
    }]
    batch.save_meta()

    app = FastAPI()
    app.include_router(gr_router.router, prefix="/api")
    app.include_router(grade_template.router, prefix="/api")
    app.include_router(grade_review.router, prefix="/api")

    def _override_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[_require_user] = lambda: User(
        id=1, name="teacher", role="teacher", current_class_id=1,
    )
    return TestClient(app, raise_server_exceptions=False), upload_dir


def _confirm_errors(client, batch_id: str) -> list[dict]:
    response = client.post("/api/batch-grade/confirm", json={
        "batch_id": batch_id,
        "mode": "auto",
        "category": "daily",
        "exam_name": "",
    })
    assert response.status_code == 200
    return [
        payload for event, payload in _parse_sse(response.text) if event == "error"
    ]


def test_confirm_stops_at_batch_grading_deadline_and_preserves_pending(
    monkeypatch, tmp_path,
):
    from routers import grade_pipeline

    client, upload_dir = _one_paper_grading_client(monkeypatch, tmp_path, "deadline-batch")
    monkeypatch.setattr(grade_pipeline, "FLASH_LOCAL_MAX_CONCURRENT", 1)
    monkeypatch.setattr(grade_pipeline, "_GRADE_BATCH_DEADLINE_S", 0.01)
    cancelled = {"value": False}

    async def slow_grade(**_kwargs):
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled["value"] = True
            raise
        raise AssertionError("the batch deadline did not cancel the active grade")

    monkeypatch.setattr(grade_pipeline, "grade_paper_unified", slow_grade)
    errors = _confirm_errors(client, "deadline-batch")

    assert [payload["code"] for payload in errors] == ["GRADING_BATCH_DEADLINE"]
    assert cancelled["value"] is True
    saved = QuickBatch.load_meta("deadline-batch", str(upload_dir))
    assert saved.status == "grading_interrupted"
    assert saved.results == []
    assert "剩余 1 张" in saved.error


def test_confirm_defers_a_paper_whose_grading_model_stays_unavailable(monkeypatch, tmp_path):
    from routers import grade_pipeline
    from services.grader import GradingModelUnavailable

    client, upload_dir = _one_paper_grading_client(monkeypatch, tmp_path, "pending-batch")

    async def unavailable(**_kwargs):
        raise GradingModelUnavailable("数学批改模型暂不可用")

    monkeypatch.setattr(grade_pipeline, "grade_paper_unified", unavailable)
    errors = _confirm_errors(client, "pending-batch")

    assert [payload["code"] for payload in errors] == ["GRADING_PAPERS_PENDING"]
    assert [failure["paper_id"] for failure in errors[0]["failures"]] == ["paper-1"]
    saved = QuickBatch.load_meta("pending-batch", str(upload_dir))
    assert saved.status == "grading_interrupted"
    assert saved.results == []
    assert "1 张试卷的数学判分" in saved.error


def test_confirm_refuses_a_batch_that_is_already_being_graded(monkeypatch, tmp_path):
    from routers import grade_pipeline

    client, upload_dir = _one_paper_grading_client(monkeypatch, tmp_path, "busy-batch")

    async def must_not_grade(**_kwargs):
        raise AssertionError("a locked batch must not be graded")

    monkeypatch.setattr(grade_pipeline, "grade_paper_unified", must_not_grade)
    with batch_operation_lock("busy-batch", str(upload_dir)):
        errors = _confirm_errors(client, "busy-batch")

    assert [payload["code"] for payload in errors] == ["BATCH_GRADING_BUSY"]
    assert QuickBatch.load_meta("busy-batch", str(upload_dir)).status == "ocr_done"


def test_same_name_conflict_requires_explicit_existing_resolution(monkeypatch, tmp_path):
    """同名冲突先阻断；老师显式选择已有学生后才批改，绝不创建占位生。"""
    from models_rate_limit import TeacherClass

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
    db.add(Student(id=100, teacher_id=1, class_id=1, name="王伟", student_no="20230011"))
    db.add(Student(id=101, teacher_id=1, class_id=1, name="王伟", student_no="20230012"))
    db.commit()
    db.close()

    import routers.grade as gr_router
    from routers import grade_pipeline
    from routers import grade_review
    from routers import grade_template
    import services.grading_persistence as gp
    from services import grader as gr_svc

    upload_dir = tmp_path / "uploads"
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(upload_dir))
    monkeypatch.setattr(grade_pipeline, "SessionLocal", Session)
    monkeypatch.setattr(gr_router, "SessionLocal", Session)
    monkeypatch.setattr(gp, "ARCHIVE_DIR", str(tmp_path / "archive"))
    paper_path = upload_dir / "quick_batch_batch3" / "p1.png"
    paper_path.parent.mkdir(parents=True)
    paper_path.write_bytes(b"paper-bytes")

    batch = QuickBatch("batch3", str(upload_dir))
    batch.teacher_id = 1
    batch.class_id = 1
    batch.source_mode = "template"
    batch.template_id = "same-name-template"
    batch.status = "awaiting_confirmation"
    batch.papers = [{
        "paper_id": "paper-1",
        "filename": "p1.png",
        "student_name": "王伟",
        "student_no": "",
        "identity": {
            "status": "needs_confirmation", "student_id": None,
            "reason": "name_ambiguous", "ocr_name": "王伟", "ocr_student_no": "",
            "candidates": [
                {"student_id": 100, "name": "王伟", "student_no": "20230011", "class_id": 1},
                {"student_id": 101, "name": "王伟", "student_no": "20230012", "class_id": 1},
            ],
        },
        "image_path": str(paper_path),
        "orientation_status": "auto_passed",
        "orientation_reason": "normal_angle",
        "questions": [{"question_no": "1", "student_answer": "2", "question_type": "solve"}],
    }]
    batch.questions = [{"question_no": "1", "question_text": "1+1=?"}]
    batch.answer_map = {
        "1": {
            "matched": True, "standard_answer": "2", "question_text": "1+1=?",
            "question_bank_id": None, "chapter": "代数", "topic": "加法",
            "difficulty": 2, "source": "manual",
        }
    }
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
    app.dependency_overrides[_require_user] = lambda: User(
        id=1, name="老师", role="teacher", current_class_id=1,
    )
    client = TestClient(app, raise_server_exceptions=False)

    async def _mock_judge_paper(questions, detail_level="detailed", *, teacher_material=None):
        return {str(q["question_no"]): {
            "result": "correct", "explanation": "", "hint": "",
            "model_answer": "", "standard_answer": q.get("standard_answer", ""),
            "error_type": "",
        } for q in questions}
    monkeypatch.setattr(gr_svc, "judge_paper", _mock_judge_paper)

    payload = {
        "batch_id": "batch3",
        "mode": "auto",
        "category": "daily",
        "exam_name": "",
    }

    blocked = client.post("/api/batch-grade/confirm", json=payload)
    assert blocked.status_code == 400
    db = Session()
    assert db.query(Student).count() == 2
    assert db.query(GradingResult).count() == 0
    db.close()

    payload["identity_resolutions"] = [{
        "paper_id": "paper-1", "action": "existing", "student_id": 101,
    }]
    response = client.post("/api/batch-grade/confirm", json=payload)
    assert response.status_code == 200, response.text
    events = _parse_sse(response.text)
    row = [item for event, item in events if event == "result"][0]
    assert row["needs_name_confirm"] is False

    db = Session()
    assert db.query(Student).count() == 2
    assert db.query(GradingResult).count() == 0
    db.close()
    staged = QuickBatch.load_meta("batch3", str(upload_dir))
    assignment = staged.review_results[0]["identity_assignment"]
    assert assignment["action"] == "existing"
    assert assignment["student_id"] == 101


def test_confirm_routes_to_existing_student_by_number(monkeypatch, tmp_path):
    """确认时老师填的学号命中某个已存在同名学生 → 结果改挂到该生（按学号路由），
    绝不因同名 .first() 归并到任意一个同名学生。这是同名消歧的核心正确性。
    """
    from models import Student
    from models_rate_limit import TeacherClass

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
    # 两个"王伟"：学号 001 / 002。批改建的占位生（第三个王伟）要按学号归并到 002。
    db.add(Student(id=200, teacher_id=1, class_id=1, name="王伟", student_no="001"))
    db.add(Student(id=201, teacher_id=1, class_id=1, name="王伟", student_no="002"))
    # 占位生：批改时同名冲突建出来的，姓名占位、无学号
    db.add(Student(id=202, teacher_id=1, class_id=1, name="学生_abc123", student_no=None))
    gr = GradingResult(
        id=500, student_id=202, category="daily", exam_name="",
        image_content_hash="hash-conf-2", total_questions=1, correct_count=1,
        score=1.0, grading_mode="auto",
    )
    gr.set_details([{"question_no": "1", "result": "correct",
                     "needs_name_confirm": True, "ocr_name": "王伟", "ocr_student_no": ""}])
    db.add(gr)
    db.commit()
    db.close()

    import routers.student_confirm as sc_router

    app = FastAPI()
    app.include_router(sc_router.router, prefix="/api")

    def _override_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[_require_user] = lambda: User(id=1, name="老师", role="teacher", current_class_id=1)
    client = TestClient(app, raise_server_exceptions=False)

    # 老师确认：姓名王伟 + 学号 002 → 应改挂到 id=201（而非 .first() 命中的 200）
    r = client.post("/api/student/confirm", json={
        "upload_id": 500,
        "confirmed_name": "王伟",
        "confirmed_student_no": "002",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["merged"] is True
    assert body["student_id"] == 201  # 按学号路由，不是同名首个 200

    db = Session()
    assert db.query(GradingResult).filter(GradingResult.id == 500).one().student_id == 201
    db.close()


def test_confirm_rejected_when_no_class_selected(monkeypatch, tmp_path):
    """未选班级时批改 confirm 直接 400——守住"必须在已选班级下批改"的产品约束，
    杜绝跨班误配 / 无班级归属孤儿生。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    db.add(User(id=1, name="老师", role="teacher"))
    db.commit()
    db.close()

    import routers.grade as gr_router
    from routers import grade_pipeline
    from routers import grade_review
    from routers import grade_template
    upload_dir = tmp_path / "uploads"
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(upload_dir))
    monkeypatch.setattr(grade_pipeline, "SessionLocal", Session)
    monkeypatch.setattr(gr_router, "SessionLocal", Session)

    batch = QuickBatch("batch_noclass", str(upload_dir))
    batch.teacher_id = 1
    batch.class_id = None
    batch.status = "ocr_done"
    batch.papers = [{"filename": "p.png", "student_name": "张三", "student_no": "",
                     "image_path": "x", "questions": [{"question_no": "1", "student_answer": "2"}]}]
    batch.questions = [{"question_no": "1", "question_text": "1+1=?"}]
    batch.answer_map = {}
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
    # current_class_id 缺省为 None → 未选班级
    app.dependency_overrides[_require_user] = lambda: User(id=1, name="老师", role="teacher")
    client = TestClient(app, raise_server_exceptions=False)

    r = client.post("/api/batch-grade/confirm", json={
        "batch_id": "batch_noclass", "mode": "auto",
        "confirmed_answers": [{"question_no": "1", "content": "1+1=?", "answer": "2", "matched": True}],
        "category": "daily", "exam_name": "",
    })
    assert r.status_code == 400
    assert "班级" in r.json()["detail"]
