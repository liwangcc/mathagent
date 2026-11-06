import json
import multiprocessing
import multiprocessing.connection
import os
from threading import Event, Thread

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models  # noqa: F401
import models_rate_limit  # noqa: F401
import models_user  # noqa: F401
from database import Base, get_db
from models import GradingCorrection, GradingResult, Student, StudentProfile
from models_correction import (
    CorrectionEvidence,
    QuestionAttemptEvent,
    StudentObservationPeriod,
    WrongQuestionCycle,
)
from models_rate_limit import TeacherClass
from models_user import User
from routers.auth import _require_user
from services.quick_grader import BatchOperationBusy, QuickBatch, batch_operation_lock


def _hold_batch_lock_in_child(upload_dir: str, batch_id: str, ready, release) -> None:
    """Top-level so Windows multiprocessing spawn can import it."""
    with batch_operation_lock(batch_id, upload_dir):
        ready.send("locked")
        release.wait(timeout=10)


def _setup_review_batch(monkeypatch, tmp_path):
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
    db.add(Student(id=10, teacher_id=1, class_id=1, name="已存在", student_no="001"))
    db.commit()
    db.close()

    import routers.grade as grade_router
    from routers import grade_pipeline
    from routers import grade_review
    from routers import grade_template
    upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(grade_pipeline, "UPLOAD_DIR", str(upload_dir))
    monkeypatch.setattr(grade_router, "UPLOAD_DIR", str(upload_dir))
    monkeypatch.setattr(grade_pipeline, "SessionLocal", Session)
    monkeypatch.setattr(grade_router, "SessionLocal", Session)

    batch = QuickBatch("review-batch", str(upload_dir))
    batch.teacher_id = 1
    batch.class_id = 1
    batch.status = "review_ready"
    batch.results = [
        {"review_id": "p1", "paper_id": "p1", "student_name": "已存在", "score": 1.0, "disposition": "active"},
        {"review_id": "p2", "paper_id": "p2", "student_name": "待建", "score": 0.0, "disposition": "active"},
    ]
    batch.review_results = [
        {
            "review_id": "p1", "paper_id": "p1", "student_name": "已存在", "student_no": "001",
            "identity_assignment": {"action": "existing", "student_id": 10, "name": "已存在", "student_no": "001"},
            "result": {"score": 0.0, "total": 1, "correct_count": 0, "partial_count": 0, "wrong_count": 1, "needs_review_count": 0, "details": [{"question_no": "1", "question": "题目一", "student_answer": "A", "correct_answer": "B", "solution_steps": ["解析"], "result": "wrong"}]},
            "knowledge_items": [], "disposition": "active", "voided_at": "", "void_reason": "",
        },
        {
            "review_id": "p2", "paper_id": "p2", "student_name": "待建", "student_no": "002",
            "identity_assignment": {"action": "create", "student_id": None, "name": "待建", "student_no": "002"},
            "result": {"score": 0.0, "total": 1, "correct_count": 0, "partial_count": 0, "wrong_count": 1, "needs_review_count": 0, "details": []},
            "knowledge_items": [], "disposition": "active", "voided_at": "", "void_reason": "",
        },
    ]
    batch.save_meta()

    app = FastAPI()
    app.include_router(grade_router.router, prefix="/api")
    app.include_router(grade_template.router, prefix="/api")
    app.include_router(grade_review.router, prefix="/api")

    def override_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[_require_user] = lambda: User(id=1, name="老师", role="teacher", current_class_id=1)
    return TestClient(app, raise_server_exceptions=False), Session, upload_dir


def test_void_one_review_result_excludes_it_without_formal_write(monkeypatch, tmp_path):
    client, Session, upload_dir = _setup_review_batch(monkeypatch, tmp_path)

    response = client.post("/api/batch-grade/review/void", json={
        "batch_id": "review-batch", "paper_id": "p2", "reason": "非本次试卷",
    })

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["voided"] == 1
    batch = QuickBatch.load_meta("review-batch", str(upload_dir))
    assert batch.review_results[0]["disposition"] == "active"
    assert batch.review_results[1]["disposition"] == "voided"
    assert batch.review_results[1]["void_reason"] == "非本次试卷"
    assert batch.results[1]["disposition"] == "voided"
    db = Session()
    assert db.query(Student).count() == 1
    assert db.query(GradingResult).count() == 0
    assert db.query(StudentProfile).count() == 0
    db.close()


def test_unknown_review_batch_does_not_create_a_metadata_directory(monkeypatch, tmp_path):
    client, _Session, upload_dir = _setup_review_batch(monkeypatch, tmp_path)
    unknown_id = "unknown-review-batch"
    unknown_dir = upload_dir / f"quick_batch_{unknown_id}"

    response = client.post(
        "/api/batch-grade/review/void", json={"batch_id": unknown_id, "paper_id": "p1"}
    )

    assert response.status_code == 404
    assert not unknown_dir.exists()


def test_review_state_groups_pages_only_by_frozen_identity_and_preserves_upload_order(monkeypatch, tmp_path):
    client, _Session, upload_dir = _setup_review_batch(monkeypatch, tmp_path)
    batch = QuickBatch.load_meta("review-batch", str(upload_dir))
    batch.uploads = [
        {"paper_id": "p3", "filename": "第2页.png"},
        {"paper_id": "p1", "filename": "第1页.png"},
        {"paper_id": "p2", "filename": "同名但不同学生.png"},
    ]
    batch.review_results[0]["filename"] = "第1页.png"
    batch.review_results[0]["result"]["details"][0]["question_no"] = "1"
    batch.questions = [{"question_no": "1", "question_type": "choice"}]
    batch.review_results.append({
        "review_id": "p3", "paper_id": "p3", "filename": "第2页.png",
        "student_name": "已存在", "student_no": "001",
        "identity_assignment": {
            "action": "existing", "student_id": 10,
            "name": "已存在", "student_no": "001",
        },
        "result": {
            "score": 1.0, "total": 1, "correct_count": 1,
            "partial_count": 0, "wrong_count": 0, "needs_review_count": 0,
            "details": [{"question_no": "12", "result": "correct"}],
        },
        "knowledge_items": [], "disposition": "active",
    })
    batch.results.append({
        "review_id": "p3", "paper_id": "p3", "student_name": "已存在",
        "score": 1.0, "disposition": "active",
    })
    # Same display name must not merge when the frozen identity differs.
    batch.review_results[1]["student_name"] = "已存在"
    batch.save_meta()

    response = client.get("/api/batch-grade/review/review-batch")

    assert response.status_code == 200, response.text
    rows = {row["paper_id"]: row for row in response.json()["results"]}
    assert rows["p3"]["pages"] == [
        {"paper_id": "p3", "page_index": 0, "filename": "第2页.png"},
        {"paper_id": "p1", "page_index": 1, "filename": "第1页.png"},
    ]
    assert rows["p1"]["review_group_id"] == rows["p3"]["review_group_id"]
    assert rows["p1"]["page_index"] == 1
    assert rows["p1"]["details"][0]["paper_id"] == "p1"
    assert rows["p1"]["details"][0]["page_index"] == 1
    assert rows["p1"]["details"][0]["question_type"] == "choice"
    assert rows["p2"]["page_count"] == 1
    assert rows["p2"]["review_group_id"] != rows["p1"]["review_group_id"]


@pytest.mark.skipif(os.name != "nt", reason="Windows msvcrt lock regression")
def test_windows_cross_process_lock_reports_busy_not_permission_error(tmp_path):
    context = multiprocessing.get_context("spawn")
    upload_dir = str(tmp_path / "uploads")
    batch_id = "windows-lock"
    ready, locked = context.Pipe(duplex=False)
    release = context.Event()
    holder = context.Process(
        target=_hold_batch_lock_in_child,
        args=(upload_dir, batch_id, locked, release),
    )
    holder.start()
    try:
        # Spawning a Python child takes as long as the machine needs: wait until
        # it holds the lock, or until it has exited without taking it.
        multiprocessing.connection.wait([ready, holder.sentinel])
        assert ready.poll(), "child did not acquire the batch lock"
        with pytest.raises(BatchOperationBusy):
            with batch_operation_lock(batch_id, upload_dir):
                pass
    finally:
        release.set()
        holder.join(timeout=10)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5)
    assert holder.exitcode == 0


def test_manual_decision_resolves_pending_review_and_rejects_stale_revision(monkeypatch, tmp_path):
    client, Session, upload_dir = _setup_review_batch(monkeypatch, tmp_path)
    batch = QuickBatch.load_meta("review-batch", str(upload_dir))
    pending = batch.review_results[0]
    pending["result"] = {
        "score": None,
        "total": 1,
        "correct_count": 0,
        "partial_count": 0,
        "wrong_count": 0,
        "needs_review_count": 1,
        "details": [{
            "question_no": "1", "question": "题目一", "student_answer": "B",
            "correct_answer": "B", "result": "needs_review", "explanation": "模型失败",
        }],
    }
    batch.results[0].update({"score": None, "correct": 0, "partial": 0, "wrong": 0, "needs_review": 1})
    batch.save_meta()

    payload = {
        "batch_id": "review-batch",
        "paper_id": "p1",
        "review_revision": 0,
        "decisions": [{"question_no": "1", "result": "correct", "note": "教师复核"}],
    }
    saved = client.post("/api/batch-grade/review/decision", json=payload)

    assert saved.status_code == 200, saved.text
    response = saved.json()
    assert response["review_revision"] == 1
    saved_batch = QuickBatch.load_meta("review-batch", str(upload_dir))
    saved_review = saved_batch.review_results[0]
    assert saved_review["result"]["score"] is None
    assert saved_review["result"]["correct_rate"] == 1.0
    assert saved_review["result"]["needs_review_count"] == 0
    assert saved_review["result"]["details"][0]["result"] == "correct"
    correction = saved_review["manual_corrections"][0]
    assert {key: correction[key] for key in ("question_no", "old_result", "new_result", "note", "teacher_id")} == {
        "question_no": "1", "old_result": "needs_review", "new_result": "correct",
        "note": "教师复核", "teacher_id": 1,
    }
    assert correction["created_at"]
    assert saved_batch.results[0]["score"] is None
    assert saved_batch.results[0]["correct_rate"] == 1.0
    assert saved_batch.results[0]["needs_review"] == 0
    db = Session()
    assert db.query(Student).count() == 1
    assert db.query(GradingResult).count() == 0
    db.close()

    stale = client.post("/api/batch-grade/review/decision", json=payload)
    assert stale.status_code == 409
    assert QuickBatch.load_meta("review-batch", str(upload_dir)).review_revision == 1


def test_page_type_pending_review_is_skipped_until_teacher_resolves_it(monkeypatch, tmp_path):
    client, Session, upload_dir = _setup_review_batch(monkeypatch, tmp_path)
    batch = QuickBatch.load_meta("review-batch", str(upload_dir))
    review = batch.review_results[0]
    review["category"] = "exam"
    review["exam_name"] = "月考"
    review["result"] = {
        "score": None,
        "max_score": 5.0,
        "correct_rate": None,
        "total": 1,
        "correct_count": 0,
        "partial_count": 0,
        "wrong_count": 0,
        "needs_review_count": 1,
        "details": [{
            "question_no": "1",
            "question": "题目一",
            "student_answer": "A",
            "correct_answer": "B",
            "result": "needs_review",
            "error_type": "unexpected_page_type",
            "match_method": "page_type_gate",
            "max_score": 5.0,
            "awarded_score": None,
        }],
    }
    batch.results[0].update({
        "category": "exam", "exam_name": "月考",
        "score": None, "max_score": 5.0, "correct_rate": None,
        "wrong": 0, "needs_review": 1,
    })
    batch.review_results[1]["disposition"] = "voided"
    batch.results[1]["disposition"] = "voided"
    batch.save_meta()

    skipped = client.post(
        "/api/batch-grade/review/archive-all",
        json={"batch_id": "review-batch"},
    )

    assert skipped.status_code == 200, skipped.text
    assert skipped.json()["archived"] == 0
    assert skipped.json()["skipped_pending"] == 1
    db = Session()
    assert db.query(GradingResult).count() == 0
    db.close()

    resolved = client.post("/api/batch-grade/review/decision", json={
        "batch_id": "review-batch",
        "paper_id": "p1",
        "review_revision": 0,
        "decisions": [{"question_no": "1", "result": "wrong", "note": "教师核对页型和原卷"}],
    })
    assert resolved.status_code == 200, resolved.text
    saved = QuickBatch.load_meta("review-batch", str(upload_dir))
    assert saved.review_results[0]["result"]["needs_review_count"] == 0
    assert saved.review_results[0]["result"]["score"] == 0.0


def test_teacher_changed_review_is_archived_as_audit_not_student_correction(monkeypatch, tmp_path):
    client, Session, upload_dir = _setup_review_batch(monkeypatch, tmp_path)
    batch = QuickBatch.load_meta("review-batch", str(upload_dir))
    batch.review_results[0]["result"] = {
        "score": None,
        "total": 1,
        "correct_count": 0,
        "partial_count": 0,
        "wrong_count": 0,
        "needs_review_count": 1,
        "details": [{
            "question_no": "1", "question": "题目一", "student_answer": "B",
            "correct_answer": "B", "standard_answer": "B", "result": "needs_review",
        }],
    }
    batch.results[0].update({"score": None, "correct": 0, "partial": 0, "wrong": 0, "needs_review": 1})
    batch.review_results[1]["disposition"] = "voided"
    batch.results[1]["disposition"] = "voided"
    batch.save_meta()

    decided = client.post("/api/batch-grade/review/decision", json={
        "batch_id": "review-batch", "paper_id": "p1", "review_revision": 0,
        "decisions": [{"question_no": "1", "result": "correct", "note": "教师改判"}],
    })
    assert decided.status_code == 200, decided.text
    archived = client.post("/api/batch-grade/review/archive-all", json={"batch_id": "review-batch"})
    assert archived.status_code == 200, archived.text

    db = Session()
    assert db.query(GradingCorrection).count() == 1
    attempt = db.query(QuestionAttemptEvent).one()
    assert attempt.question_snapshot()["teacher_regraded"] is True
    grading = db.query(GradingResult).one()
    assert grading.get_details()[0]["teacher_regraded"] is True
    assert db.query(WrongQuestionCycle).count() == 0
    assert db.query(CorrectionEvidence).count() == 0
    db.close()


def test_manual_exam_decision_recalculates_actual_points(monkeypatch, tmp_path):
    client, _Session, upload_dir = _setup_review_batch(monkeypatch, tmp_path)
    batch = QuickBatch.load_meta("review-batch", str(upload_dir))
    review = batch.review_results[0]
    review["category"] = "exam"
    review["exam_name"] = "期中考试"
    review["result"] = {
        "score": None,
        "max_score": 5.0,
        "correct_rate": None,
        "total": 1,
        "correct_count": 0,
        "partial_count": 0,
        "wrong_count": 0,
        "needs_review_count": 1,
        "details": [{
            "question_no": "1",
            "question": "填空题",
            "student_answer": "2",
            "correct_answer": "2",
            "result": "needs_review",
            "max_score": 5.0,
            "awarded_score": None,
        }],
    }
    batch.results[0].update({
        "category": "exam", "exam_name": "期中考试", "score": None,
        "max_score": 5.0, "correct_rate": None, "needs_review": 1,
    })
    batch.save_meta()

    response = client.post("/api/batch-grade/review/decision", json={
        "batch_id": "review-batch",
        "paper_id": "p1",
        "review_revision": 0,
        "decisions": [{"question_no": "1", "result": "correct", "note": "教师确认"}],
    })

    assert response.status_code == 200, response.text
    saved = QuickBatch.load_meta("review-batch", str(upload_dir)).review_results[0]["result"]
    assert saved["score"] == 5.0
    assert saved["max_score"] == 5.0
    assert saved["correct_rate"] == 1.0
    assert saved["details"][0]["awarded_score"] == 5.0


def test_archive_all_archives_resolved_and_leaves_pending_until_manual_decision(monkeypatch, tmp_path):
    client, Session, upload_dir = _setup_review_batch(monkeypatch, tmp_path)
    batch = QuickBatch.load_meta("review-batch", str(upload_dir))
    batch.review_results[1]["result"] = {
        "score": None,
        "total": 1,
        "correct_count": 0,
        "partial_count": 0,
        "wrong_count": 0,
        "needs_review_count": 1,
        "details": [{
            "question_no": "1", "question": "题目二", "student_answer": "3",
            "correct_answer": "3", "result": "needs_review", "explanation": "模型失败",
        }],
    }
    batch.results[1].update({"score": None, "correct": 0, "partial": 0, "wrong": 0, "needs_review": 1})
    batch.save_meta()

    first = client.post("/api/batch-grade/review/archive-all", json={"batch_id": "review-batch"})

    assert first.status_code == 200, first.text
    assert first.json()["archived"] == 1
    assert first.json()["skipped_pending"] == 1
    assert first.json()["archived_paper_ids"] == ["p1"]
    db = Session()
    assert db.query(Student).count() == 1
    assert db.query(GradingResult).count() == 1
    assert db.query(GradingCorrection).count() == 0
    db.close()
    staged = QuickBatch.load_meta("review-batch", str(upload_dir))
    assert staged.status == "review_ready"
    assert staged.review_results[0]["disposition"] == "archived"
    assert staged.review_results[1]["disposition"] == "active"
    assert staged.review_revision == 1

    retry = client.post("/api/batch-grade/review/archive-all", json={"batch_id": "review-batch"})
    assert retry.status_code == 200, retry.text
    assert retry.json()["archived"] == 0
    assert retry.json()["skipped_pending"] == 1
    assert retry.json()["archived_paper_ids"] == []
    cannot_void_archived = client.post(
        "/api/batch-grade/review/void", json={"batch_id": "review-batch", "paper_id": "p1"}
    )
    assert cannot_void_archived.status_code == 409
    db = Session()
    assert db.query(GradingResult).count() == 1
    db.close()

    decided = client.post("/api/batch-grade/review/decision", json={
        "batch_id": "review-batch", "paper_id": "p2", "review_revision": 1,
        "decisions": [{"question_no": "1", "result": "correct", "note": "教师复核"}],
    })
    assert decided.status_code == 200, decided.text
    second = client.post("/api/batch-grade/review/archive-all", json={"batch_id": "review-batch"})

    assert second.status_code == 200, second.text
    assert second.json()["archived"] == 1
    assert second.json()["skipped_pending"] == 0
    assert second.json()["archived_paper_ids"] == ["p2"]
    db = Session()
    assert db.query(Student).count() == 2
    assert db.query(GradingResult).count() == 2
    audit = db.query(GradingCorrection).one()
    assert (audit.paper_id, audit.question_no, audit.old_result, audit.new_result, audit.note) == (
        "p2", "1", "needs_review", "correct", "教师复核",
    )
    db.close()
    final = QuickBatch.load_meta("review-batch", str(upload_dir))
    assert final.status == "archived"
    assert {review["disposition"] for review in final.review_results} == {"archived"}


def test_observation_mode_never_bypasses_pending_review_gate(monkeypatch, tmp_path):
    client, Session, upload_dir = _setup_review_batch(monkeypatch, tmp_path)
    db = Session()
    db.add(StudentObservationPeriod(
        student_id=10,
        class_id=1,
        teacher_id=1,
        reason="只排除统计，不降低教师确认门槛",
        created_by=1,
    ))
    db.commit()
    db.close()

    batch = QuickBatch.load_meta("review-batch", str(upload_dir))
    batch.review_results[0]["result"] = {
        "score": None,
        "total": 1,
        "correct_count": 0,
        "partial_count": 0,
        "wrong_count": 0,
        "needs_review_count": 1,
        "details": [{
            "question_no": "1",
            "question": "待教师确认题",
            "student_answer": "",
            "correct_answer": "",
            "result": "needs_review",
        }],
    }
    batch.results[0].update({
        "score": None,
        "correct": 0,
        "partial": 0,
        "wrong": 0,
        "needs_review": 1,
    })
    batch.review_results[1]["disposition"] = "voided"
    batch.results[1]["disposition"] = "voided"
    batch.save_meta()

    response = client.post("/api/batch-grade/review/archive-all", json={
        "batch_id": "review-batch",
    })

    assert response.status_code == 200, response.text
    assert response.json()["archived"] == 0
    assert response.json()["skipped_pending"] == 1
    db = Session()
    assert db.query(GradingResult).count() == 0
    db.close()


def test_void_all_is_idempotent_and_never_archives(monkeypatch, tmp_path):
    client, Session, upload_dir = _setup_review_batch(monkeypatch, tmp_path)

    first = client.post("/api/batch-grade/review/void-all", json={"batch_id": "review-batch"})
    second = client.post("/api/batch-grade/review/void-all", json={"batch_id": "review-batch"})

    assert first.status_code == 200, first.text
    assert first.json()["voided"] == 2
    assert second.status_code == 200, second.text
    assert second.json()["voided"] == 0
    batch = QuickBatch.load_meta("review-batch", str(upload_dir))
    assert batch.status == "voided"
    assert {item["disposition"] for item in batch.review_results} == {"voided"}
    db = Session()
    assert db.query(Student).count() == 1
    assert db.query(GradingResult).count() == 0
    db.close()


def test_archive_all_writes_only_active_results_and_is_idempotent(monkeypatch, tmp_path):
    client, Session, upload_dir = _setup_review_batch(monkeypatch, tmp_path)

    voided = client.post("/api/batch-grade/review/void", json={
        "batch_id": "review-batch", "paper_id": "p2", "reason": "异常卷",
    })
    assert voided.status_code == 200, voided.text

    archived = client.post("/api/batch-grade/review/archive-all", json={
        "batch_id": "review-batch",
    })
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived"] == 1
    assert archived.json()["skipped_voided"] == 1

    db = Session()
    assert db.query(Student).count() == 1  # p2's create assignment remains voided
    grading = db.query(GradingResult).one()
    assert grading.student_id == 10
    assert db.query(StudentProfile).count() == 1
    db.close()

    batch = QuickBatch.load_meta("review-batch", str(upload_dir))
    assert batch.status == "archived"
    assert batch.review_results[0]["archived_grading_result_id"] == grading.id
    assert batch.review_results[1]["archived_grading_result_id"] is None

    again = client.post("/api/batch-grade/review/archive-all", json={
        "batch_id": "review-batch",
    })
    assert again.status_code == 200, again.text
    assert again.json()["archived"] == 0
    assert again.json()["idempotent"] is True
    db = Session()
    assert db.query(GradingResult).count() == 1
    assert db.query(StudentProfile).count() == 1
    db.close()


def test_multi_page_student_archives_atomically_as_one_formal_result(monkeypatch, tmp_path):
    client, Session, upload_dir = _setup_review_batch(monkeypatch, tmp_path)
    batch = QuickBatch.load_meta("review-batch", str(upload_dir))
    batch.uploads = [
        {"paper_id": "p1", "filename": "第1页.png"},
        {"paper_id": "p3", "filename": "第2页.png"},
        {"paper_id": "p2", "filename": "其他学生.png"},
    ]
    batch.review_results[0].update({
        "filename": "第1页.png",
        "image_content_hash": "1" * 64,
    })
    batch.review_results[0]["result"]["details"][0].update({
        "paper_id": "p1", "page_index": 0,
    })
    batch.review_results.append({
        "review_id": "p3", "paper_id": "p3", "filename": "第2页.png",
        "student_name": "已存在", "student_no": "001",
        "identity_assignment": {
            "action": "existing", "student_id": 10,
            "name": "已存在", "student_no": "001",
        },
        "image_content_hash": "3" * 64,
        "result": {
            "score": None, "total": 1, "correct_count": 0,
            "partial_count": 0, "wrong_count": 0, "needs_review_count": 1,
            "details": [{
                "question_no": "2", "question": "题目二",
                "student_answer": "B", "correct_answer": "B",
                "result": "needs_review", "paper_id": "p3", "page_index": 1,
            }],
        },
        "knowledge_items": [], "disposition": "active",
        "manual_corrections": [],
    })
    batch.results.append({
        "review_id": "p3", "paper_id": "p3", "student_name": "已存在",
        "score": None, "needs_review": 1, "disposition": "active",
    })
    batch.review_results[1]["disposition"] = "voided"
    batch.results[1]["disposition"] = "voided"
    batch.save_meta()

    blocked = client.post(
        "/api/batch-grade/review/archive-all",
        json={"batch_id": "review-batch"},
    )
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["archived"] == 0
    assert blocked.json()["skipped_pending"] == 1
    db = Session()
    assert db.query(GradingResult).count() == 0
    assert db.query(StudentProfile).count() == 0
    db.close()

    decided = client.post("/api/batch-grade/review/decision", json={
        "batch_id": "review-batch", "paper_id": "p3", "review_revision": 0,
        "decisions": [{"question_no": "2", "result": "correct", "note": "教师确认"}],
    })
    assert decided.status_code == 200, decided.text
    archived = client.post(
        "/api/batch-grade/review/archive-all",
        json={"batch_id": "review-batch"},
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived"] == 1
    assert set(archived.json()["archived_paper_ids"]) == {"p1", "p3"}

    db = Session()
    grading = db.query(GradingResult).one()
    profile = db.query(StudentProfile).one()
    grading_id = grading.id
    assert grading.student_id == 10
    assert grading.total_questions == 2
    assert profile.grading_result_id == grading.id
    assert {detail["paper_id"] for detail in grading.get_details()} == {"p1", "p3"}
    assert db.query(GradingCorrection).count() == 1
    db.close()
    restored = QuickBatch.load_meta("review-batch", str(upload_dir))
    pages = [
        item for item in restored.review_results
        if item["paper_id"] in {"p1", "p3"}
    ]
    assert {item["disposition"] for item in pages} == {"archived"}
    assert {item["archived_grading_result_id"] for item in pages} == {grading_id}


def test_voiding_one_page_voids_the_whole_confirmed_student_group(monkeypatch, tmp_path):
    client, _Session, upload_dir = _setup_review_batch(monkeypatch, tmp_path)
    batch = QuickBatch.load_meta("review-batch", str(upload_dir))
    batch.review_results.append({
        **batch.review_results[0],
        "review_id": "p3", "paper_id": "p3", "filename": "第2页.png",
        "result": {**batch.review_results[0]["result"]},
    })
    batch.results.append({
        "review_id": "p3", "paper_id": "p3", "student_name": "已存在",
        "disposition": "active",
    })
    batch.save_meta()

    response = client.post("/api/batch-grade/review/void", json={
        "batch_id": "review-batch", "paper_id": "p3", "reason": "整份卷作废",
    })

    assert response.status_code == 200, response.text
    assert response.json()["voided"] == 2
    assert set(response.json()["paper_ids"]) == {"p1", "p3"}
    restored = QuickBatch.load_meta("review-batch", str(upload_dir))
    assert {
        item["disposition"] for item in restored.review_results
        if item["paper_id"] in {"p1", "p3"}
    } == {"voided"}


def test_void_is_rejected_while_archive_transaction_is_in_progress(monkeypatch, tmp_path):
    """作废不能与已开始的 archive 交错，否则旧 meta 会覆盖新的 voided。"""
    client, _Session, upload_dir = _setup_review_batch(monkeypatch, tmp_path)
    import routers.grade as grade_router
    from routers import grade_review

    entered_persist = Event()
    allow_archive = Event()
    response_holder = {}
    original_persist = grade_review.persist_grading_result

    def delayed_persist(*args, **kwargs):
        entered_persist.set()
        assert allow_archive.wait(timeout=5), "archive test did not release the persistence barrier"
        return original_persist(*args, **kwargs)

    monkeypatch.setattr(grade_review, "persist_grading_result", delayed_persist)

    def archive_in_background():
        response_holder["response"] = client.post(
            "/api/batch-grade/review/archive-all", json={"batch_id": "review-batch"}
        )

    worker = Thread(target=archive_in_background)
    worker.start()
    assert entered_persist.wait(timeout=5), "archive did not enter its transaction"

    voided = client.post(
        "/api/batch-grade/review/void", json={"batch_id": "review-batch", "paper_id": "p1"}
    )
    assert voided.status_code == 409, voided.text

    allow_archive.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert response_holder["response"].status_code == 200
    batch = QuickBatch.load_meta("review-batch", str(upload_dir))
    assert batch.status == "archived"


def test_archive_retry_with_missing_image_keeps_one_formal_result(monkeypatch, tmp_path):
    """文件丢失时也必须有稳定幂等键，不能因 archive 后 meta 保存失败而重复入档。"""
    client, Session, upload_dir = _setup_review_batch(monkeypatch, tmp_path)
    assert client.post(
        "/api/batch-grade/review/void", json={"batch_id": "review-batch", "paper_id": "p2"}
    ).status_code == 200

    initial = QuickBatch.load_meta("review-batch", str(upload_dir))
    initial.review_results[0]["image_path"] = str(tmp_path / "removed-before-archive.png")
    initial.review_results[0]["image_content_hash"] = ""
    initial.save_meta()

    first = client.post("/api/batch-grade/review/archive-all", json={"batch_id": "review-batch"})
    assert first.status_code == 200, first.text
    db = Session()
    assert db.query(GradingResult).count() == 1
    assert db.query(StudentProfile).count() == 1
    db.close()

    # 模拟 DB 提交成功但进程在 meta.json 更新前崩溃：旧批次仍显示 review_ready/active。
    replay = QuickBatch.load_meta("review-batch", str(upload_dir))
    replay.status = "review_ready"
    replay.review_archived_at = ""
    replay.review_results[0]["disposition"] = "active"
    replay.review_results[0]["archived_grading_result_id"] = None
    replay.review_results[0]["archived_profile_id"] = None
    replay.results[0]["disposition"] = "active"
    replay.results[0]["grading_result_id"] = None
    replay.results[0]["profile_id"] = None
    replay.save_meta()

    second = client.post("/api/batch-grade/review/archive-all", json={"batch_id": "review-batch"})
    assert second.status_code == 200, second.text
    db = Session()
    assert db.query(GradingResult).count() == 1
    assert db.query(StudentProfile).count() == 1
    db.close()

    client, Session, _upload_dir = _setup_review_batch(monkeypatch, tmp_path)

    voided = client.post("/api/batch-grade/review/void", json={
        "batch_id": "review-batch", "paper_id": "p1", "reason": "本次不计入",
    })
    assert voided.status_code == 200, voided.text
    archived = client.post("/api/batch-grade/review/archive-all", json={"batch_id": "review-batch"})
    assert archived.status_code == 200, archived.text

    db = Session()
    assert db.query(Student).count() == 2
    created = db.query(Student).filter(Student.student_no == "002").one()
    assert created.name == "待建"
    assert created.class_id == 1
    grading = db.query(GradingResult).one()
    assert grading.student_id == created.id
    db.close()


def test_archive_all_creates_confirmed_new_student_only_at_archive_time(monkeypatch, tmp_path):
    client, Session, _upload_dir = _setup_review_batch(monkeypatch, tmp_path)

    voided = client.post("/api/batch-grade/review/void", json={
        "batch_id": "review-batch", "paper_id": "p1", "reason": "本次不计入",
    })
    assert voided.status_code == 200, voided.text
    archived = client.post("/api/batch-grade/review/archive-all", json={"batch_id": "review-batch"})
    assert archived.status_code == 200, archived.text

    db = Session()
    assert db.query(Student).count() == 2
    created = db.query(Student).filter(Student.student_no == "002").one()
    assert created.name == "待建"
    assert created.class_id == 1
    grading = db.query(GradingResult).one()
    assert grading.student_id == created.id
    db.close()


def test_review_export_routes_use_active_provisional_results(monkeypatch, tmp_path):
    import io
    import zipfile
    import services.generated_files as generated_files

    client, _Session, _upload_dir = _setup_review_batch(monkeypatch, tmp_path)
    monkeypatch.setattr(generated_files, "OUTPUT_DIR", str(tmp_path / "output"))

    single = client.post("/api/batch-grade/review/export/student", json={
        "batch_id": "review-batch", "paper_id": "p1",
    })
    assert single.status_code == 200, single.text
    assert single.content[:4] == b"%PDF"

    voided = client.post("/api/batch-grade/review/void", json={
        "batch_id": "review-batch", "paper_id": "p2", "reason": "异常卷",
    })
    assert voided.status_code == 200, voided.text
    all_sheets = client.post("/api/batch-grade/review/export/all-errors", json={"batch_id": "review-batch"})
    assert all_sheets.status_code == 200, all_sheets.text
    with zipfile.ZipFile(io.BytesIO(all_sheets.content)) as archive:
        names = archive.namelist()
        assert len(names) == 1
        assert "已存在" in names[0]

    summary = client.post("/api/batch-grade/review/export/summary", json={"batch_id": "review-batch"})
    assert summary.status_code == 200, summary.text
    assert summary.content[:4] == b"%PDF"


def _setup_review_image_batch(monkeypatch, tmp_path):
    from pathlib import Path
    from PIL import Image

    client, _session, upload_dir = _setup_review_batch(monkeypatch, tmp_path)
    batch = QuickBatch.load_meta("review-batch", str(upload_dir))
    for index, review in enumerate(batch.review_results):
        paper_id = review["paper_id"]
        image_path = Path(batch.batch_dir) / f"{paper_id}.png"
        Image.new("RGB", (800, 1000), "white").save(image_path)
        batch.papers.append({"paper_id": paper_id, "image_path": str(image_path)})
        batch.uploads.append({"paper_id": paper_id, "image_path": str(image_path)})
        review["student_name"] = "../同名学生"
        review["result"]["details"] = [{
            "question_no": str(index + 1), "result": "wrong",
            "answer_bboxes": [[200, 200, 300, 240]],
        }]
    batch.save_meta()
    return client, upload_dir, batch


def test_review_image_zip_matches_single_images_including_archived_pages(monkeypatch, tmp_path):
    import io
    import zipfile
    import copy

    client, upload_dir, batch = _setup_review_image_batch(monkeypatch, tmp_path)
    batch.review_results[1]["identity_assignment"] = copy.deepcopy(batch.review_results[0]["identity_assignment"])
    batch.review_results[1]["disposition"] = "archived"
    voided = copy.deepcopy(batch.review_results[0])
    voided.update(paper_id="voided", review_id="voided", disposition="voided")
    batch.review_results.append(voided)  # No source file: must be skipped, not opened.
    batch.save_meta()
    images = [client.get(f"/api/batch-grade/review-batch/paper/{pid}/image?variant=review") for pid in ("p1", "p2")]
    response = client.post("/api/batch-grade/review/export/images", json={"batch_id": "review-batch"})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names)) == 2
        assert "第1页" in names[0] and "第2页" in names[1]
        assert all("/" not in name and "\\" not in name for name in names)
        for name, image in zip(names, images):
            assert image.status_code == 200
            assert archive.read(name) == image.content
            assert image.content.startswith(b"\x89PNG")
    assert QuickBatch.load_meta("review-batch", str(upload_dir)).review_revision == 0


def test_review_image_preview_does_not_save_until_teacher_decision(monkeypatch, tmp_path):
    client, upload_dir, _batch = _setup_review_image_batch(monkeypatch, tmp_path)
    url = "/api/batch-grade/review-batch/paper/p1/image?variant=review"
    before = client.get(url)
    decisions = [{"question_no": "1", "result": "correct"}]
    preview = client.post("/api/batch-grade/review/image-preview", json={
        "batch_id": "review-batch", "paper_id": "p1", "decisions": decisions,
    })
    assert preview.status_code == 200, preview.text
    assert preview.content != before.content
    assert client.get(url).content == before.content
    assert QuickBatch.load_meta("review-batch", str(upload_dir)).review_revision == 0
    saved = client.post("/api/batch-grade/review/decision", json={
        "batch_id": "review-batch", "paper_id": "p1", "review_revision": 0,
        "decisions": decisions,
    })
    assert saved.status_code == 200, saved.text
    assert client.get(url).content == preview.content
    invalid = client.post("/api/batch-grade/review/image-preview", json={
        "batch_id": "review-batch", "paper_id": "p1", "decisions": [{"question_no": "2", "result": "correct"}],
    })
    assert invalid.status_code == 400


@pytest.mark.parametrize("action", ["owner", "expired", "outside", "voided"])
def test_review_images_preserve_ownership_retention_and_path_checks(monkeypatch, tmp_path, action):
    from pathlib import Path
    from PIL import Image

    client, _upload_dir, batch = _setup_review_image_batch(monkeypatch, tmp_path)
    expected = {"owner": 404, "expired": 410, "outside": 404, "voided": 409}[action]
    if action == "owner":
        client.app.dependency_overrides[_require_user] = lambda: User(id=2, name="另一位老师", role="teacher")
    elif action == "expired":
        batch.asset_lifecycle = {"status": "expired"}
    elif action == "outside":
        outside = Path(tmp_path) / "outside.png"
        Image.new("RGB", (80, 100), "white").save(outside)
        batch.papers[0]["image_path"] = str(outside)
    else:
        for review in batch.review_results:
            review["disposition"] = "voided"
    batch.save_meta()
    assert client.get("/api/batch-grade/review-batch/paper/p1/image?variant=review").status_code == expected
    assert client.post("/api/batch-grade/review/export/images", json={"batch_id": "review-batch"}).status_code == expected
    assert client.post("/api/batch-grade/review/image-preview", json={
        "batch_id": "review-batch", "paper_id": "p1",
    }).status_code == expected


def test_review_image_recovers_old_boxes_from_the_matched_template_page(monkeypatch, tmp_path):
    from pathlib import Path
    from types import SimpleNamespace
    from PIL import Image
    import routers.grade as grade_router
    from routers import grade_template

    client, _upload_dir, batch = _setup_review_image_batch(monkeypatch, tmp_path)
    batch.template_id = "two-page-template"
    batch.papers[0]["ocr_page_type"] = "page_2"
    del batch.review_results[0]["result"]["details"][0]["answer_bboxes"]
    batch.save_meta()
    template_dir = tmp_path / "template"
    template_dir.mkdir()
    Image.new("RGB", (800, 1000), "white").save(template_dir / "page2.png")
    template = SimpleNamespace(teacher_id=1, template_dir=str(template_dir), page_artifacts={
        "page_1": {"template_image": "missing-page1.png", "question_schema": [{"question_number": "2"}]},
        "page_2": {"template_image": "page2.png", "question_schema": [{"question_number": "1"}],
                   "template_answer_regions": [{"question_number": "1", "regions": [[100, 100, 150, 120]]}]},
    })
    monkeypatch.setattr(grade_router, "_load_owned_template_session", lambda *args: template)
    monkeypatch.setattr(grade_template, "_load_owned_template_session", lambda *args: template)
    mapped = []
    def align(details, source, target):
        assert Path(source).name == "page2.png"
        assert details[0]["answer_box_source"] == "template"
        assert details[0]["answer_bboxes"] == [[100, 100, 150, 120]]
        details[0]["answer_bboxes"] = [[200, 200, 300, 240]]
        mapped.append(True)
        return details
    monkeypatch.setattr(grade_router, "align_template_review_details", align)
    response = client.get("/api/batch-grade/review-batch/paper/p1/image?variant=review")
    assert response.status_code == 200, response.text
    assert mapped
    assert response.content != (Path(batch.batch_dir) / "p1.png").read_bytes()


def test_review_template_boxes_are_transformed_before_rendering(monkeypatch, tmp_path):
    import numpy as np
    from PIL import Image
    from services import review_marks

    template = tmp_path / "template.png"
    student = tmp_path / "student.png"
    Image.new("RGB", (1000, 1000), "white").save(template)
    Image.new("RGB", (2000, 2000), "white").save(student)
    monkeypatch.setattr(review_marks, "_template_to_target_homography", lambda *_: np.array([
        [2., 0., 200.], [0., 2., 100.], [0., 0., 1.],
    ]))
    details = [{
        "answer_box_source": "template",
        "answer_bboxes": [[100, 100, 300, 200]],
        "grading_units": [{
            "result": "wrong",
            "answer_bboxes": [[100, 100, 300, 200]],
        }],
    }]
    mapped = review_marks.align_template_review_details(details, template, student)
    assert mapped[0]["answer_bboxes"] == [[200., 150., 400., 250.]]
    assert mapped[0]["grading_units"][0]["answer_bboxes"] == [[200., 150., 400., 250.]]
    assert details[0]["answer_bboxes"] == [[100, 100, 300, 200]]


def test_review_template_registration_handles_repeated_answer_rows(tmp_path):
    from PIL import Image, ImageDraw
    from services import review_marks

    template = tmp_path / "repeated-template.png"
    student = tmp_path / "repeated-student.png"
    source = Image.new("L", (1000, 1400), 255)
    draw = ImageDraw.Draw(source)
    for y in range(50, 1350, 60):
        for x in range(40, 950, 70):
            draw.rectangle((x, y, x + 30 + (y % 13), y + 20), fill=0)
    source.convert("RGB").save(template)
    target = Image.new("L", (1100, 1500), 255)
    target.paste(source, (30, 40))
    target.convert("RGB").save(student)

    details = [{
        "answer_box_source": "template",
        "answer_bboxes": [[100, 100, 300, 200]],
    }]
    mapped = review_marks.align_template_review_details(details, template, student)
    left, top, right, bottom = mapped[0]["answer_bboxes"][0]
    assert 115 < left < 122
    assert 117 < top < 123
    assert 297 < right < 304
    assert 210 < bottom < 216


def test_archive_retry_keeps_actual_hash_after_post_commit_meta_failure(monkeypatch, tmp_path):
    """真实图片哈希必须在首次 DB 写入前落盘，防止提交后崩溃再重试重复入档。"""
    client, Session, upload_dir = _setup_review_batch(monkeypatch, tmp_path)
    image_path = tmp_path / "paper-with-real-hash.png"
    image_path.write_bytes(b"stable-image-content")
    batch = QuickBatch.load_meta("review-batch", str(upload_dir))
    batch.review_results[0]["image_path"] = str(image_path)
    batch.review_results[0]["image_content_hash"] = ""
    batch.review_results[1]["disposition"] = "voided"
    batch.results[1]["disposition"] = "voided"
    batch.save_meta()

    original_save_meta = QuickBatch.save_meta

    def fail_only_final_archive_meta_save(self):
        if self.batch_id == "review-batch" and self.review_results[0].get("disposition") == "archived":
            raise OSError("simulate crash after DB commit")
        return original_save_meta(self)

    monkeypatch.setattr(QuickBatch, "save_meta", fail_only_final_archive_meta_save)
    crashed = client.post("/api/batch-grade/review/archive-all", json={"batch_id": "review-batch"})
    assert crashed.status_code == 500
    db = Session()
    assert db.query(GradingResult).count() == 1
    assert db.query(StudentProfile).count() == 1
    db.close()

    persisted_before_retry = QuickBatch.load_meta("review-batch", str(upload_dir))
    stable_hash = persisted_before_retry.review_results[0].get("image_content_hash")
    assert stable_hash, "archive idempotency hash must be persisted before DB commit"
    image_path.unlink()
    monkeypatch.setattr(QuickBatch, "save_meta", original_save_meta)

    replay = client.post("/api/batch-grade/review/archive-all", json={"batch_id": "review-batch"})
    assert replay.status_code == 200, replay.text
    db = Session()
    assert db.query(GradingResult).count() == 1
    assert db.query(StudentProfile).count() == 1
    db.close()


def test_review_get_hydrates_latest_manual_decision(monkeypatch, tmp_path):
    client, _Session, upload_dir = _setup_review_batch(monkeypatch, tmp_path)
    batch = QuickBatch.load_meta("review-batch", str(upload_dir))
    batch.review_results[0]["result"] = {
        "score": None, "total": 1, "correct_count": 0, "partial_count": 0,
        "wrong_count": 0, "needs_review_count": 1,
        "details": [{
            "question_no": "1", "question": "题目一", "student_answer": "B",
            "correct_answer": "B", "result": "needs_review", "explanation": "模型失败",
        }],
    }
    batch.results[0].update({"score": None, "correct": 0, "partial": 0, "wrong": 0, "needs_review": 1})
    batch.save_meta()

    saved = client.post("/api/batch-grade/review/decision", json={
        "batch_id": "review-batch", "paper_id": "p1", "review_revision": 0,
        "decisions": [{"question_no": "1", "result": "correct", "note": "刷新前保存"}],
    })
    assert saved.status_code == 200, saved.text

    resumed = client.get("/api/batch-grade/review/review-batch")
    assert resumed.status_code == 200, resumed.text
    payload = resumed.json()
    assert payload["status"] == "review_ready"
    assert payload["review_revision"] == 1
    row = next(item for item in payload["results"] if item["paper_id"] == "p1")
    assert row["score"] is None
    assert row["correct_rate"] == 1.0
    assert row["needs_review"] == 0
    assert row["details"][0]["result"] == "correct"
