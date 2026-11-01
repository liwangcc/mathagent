from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models  # noqa: F401
import models_rate_limit  # noqa: F401
import models_user  # noqa: F401
from database import Base
from models import (
    GradingCorrection,
    GradingResult,
    KnowledgeEvent,
    QuestionBank,
    Student,
    StudentProfile,
)
from models_correction import QuestionAttemptEvent, WrongQuestionCycle
from models_user import User
from routers.teacher import ConfirmJudgmentRequest, confirm_judgment, delete_from_bank
from services.grader import GraderService
from services.grading_persistence import persist_grading_result
from services.correction_tracking import correction_rate, wrong_question_summary


def _session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_conn, _connection_record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def test_teacher_can_delete_private_bank_question_without_erasing_history():
    """Deleting a private source question preserves the student's historical event."""
    Session = _session_factory()
    db = Session()
    try:
        teacher = User(id=1, name="教师", role="teacher")
        student = Student(id=1, teacher_id=teacher.id, name="学生", student_no="001")
        question = QuestionBank(
            id=1,
            teacher_id=teacher.id,
            is_public=False,
            question="1 + 1 = ?",
            answer="2",
            source="manual",
        )
        db.add(teacher)
        db.commit()
        db.add_all([student, question])
        db.commit()
        event = KnowledgeEvent(
            student_id=student.id,
            teacher_id=teacher.id,
            question_bank_id=question.id,
            chapter="代数",
            topic="加法",
            result="wrong",
        )
        db.add(event)
        db.commit()

        GraderService._index_dirty = False
        response = delete_from_bank(question.id, db=db, user=teacher)

        assert response["ok"] is True
        assert db.get(QuestionBank, question.id) is None
        saved_event = db.get(KnowledgeEvent, event.id)
        assert saved_event is not None
        assert saved_event.question_bank_id is None
        assert GraderService._index_dirty is True
    finally:
        db.close()


def test_voiding_auto_question_detaches_historical_events_before_delete():
    """The pending-judgment void path follows the same history-preserving deletion rule."""
    Session = _session_factory()
    db = Session()
    try:
        teacher = User(id=1, name="教师", role="teacher")
        db.add(teacher)
        db.commit()
        student = Student(id=1, teacher_id=teacher.id, name="学生", student_no="001")
        db.add(student)
        db.commit()

        result = GradingResult(student_id=student.id, total_questions=1, wrong_count=1, score=0.0)
        result.set_details([{"question": "自动题", "result": "wrong"}])
        db.add(result)
        db.commit()
        auto_question = QuestionBank(
            teacher_id=teacher.id,
            is_public=False,
            question="自动题",
            answer="答案",
            source="auto",
        )
        db.add(auto_question)
        db.commit()
        knowledge_event = KnowledgeEvent(
            student_id=student.id,
            teacher_id=teacher.id,
            grading_result_id=result.id,
            question_bank_id=auto_question.id,
            chapter="代数",
            topic="加法",
            result="wrong",
        )
        db.add(knowledge_event)
        db.commit()

        GraderService._index_dirty = False
        response = confirm_judgment(
            result.id,
            ConfirmJudgmentRequest(action="void", detail_index=0),
            db=db,
            user=teacher,
        )

        assert response == {"ok": True}
        assert db.get(QuestionBank, auto_question.id) is None
        saved_event = db.get(KnowledgeEvent, knowledge_event.id)
        assert saved_event is not None
        assert saved_event.question_bank_id is None
        assert GraderService._index_dirty is True
    finally:
        db.close()


def test_hidden_result_cannot_be_mutated_through_pending_judgment_endpoint():
    """Soft-hidden records cannot be edited through a stale pending-item URL."""
    Session = _session_factory()
    db = Session()
    try:
        teacher = User(id=1, name="教师", role="teacher")
        db.add(teacher)
        db.commit()
        student = Student(id=1, teacher_id=teacher.id, name="学生", student_no="001")
        db.add(student)
        db.commit()
        hidden_result = GradingResult(
            student_id=student.id,
            deleted_at=datetime.now(),
            total_questions=1,
            wrong_count=1,
            score=0.0,
        )
        hidden_result.set_details([{"question": "隐藏题", "result": "wrong"}])
        db.add(hidden_result)
        db.commit()

        with pytest.raises(HTTPException) as exc_info:
            confirm_judgment(
                hidden_result.id,
                ConfirmJudgmentRequest(action="correct", detail_index=0),
                db=db,
                user=teacher,
            )

        assert exc_info.value.status_code == 404
    finally:
        db.close()


def test_pending_teacher_judgment_updates_archive_analytics_and_correction_cycle():
    Session = _session_factory()
    db = Session()
    try:
        teacher = User(id=1, name="教师", role="teacher")
        student = Student(id=1, teacher_id=teacher.id, name="学生", student_no="001")
        db.add(teacher)
        db.commit()
        db.add(student)
        db.commit()
        detail = {
            "question_no": "1",
            "question": "判断函数单调性",
            "question_type": "solve",
            "standard_answer": "递增",
            "student_answer": "待确认作答",
            "result": "needs_review",
        }

        first, first_profile, _created = persist_grading_result(
            db,
            student=student,
            teacher_id=teacher.id,
            category="daily",
            exam_name="",
            image_path="",
            image_hash="f1" * 32,
            total_questions=1,
            correct_count=0,
            partial_count=0,
            wrong_count=0,
            needs_review_count=1,
            score=None,
            details=[detail],
            knowledge_items=[{"chapter": "函数", "topic": "单调性"}],
        )
        confirm_judgment(
            first.id,
            ConfirmJudgmentRequest(action="wrong", detail_index=0),
            db=db,
            user=teacher,
        )

        db.refresh(first)
        db.refresh(first_profile)
        assert first.wrong_count == first_profile.wrong_count == 1
        assert first.needs_review_count == first_profile.needs_review_count == 0
        assert first_profile.get_details()[0]["result"] == "wrong"
        assert db.query(KnowledgeEvent).filter_by(grading_result_id=first.id).one().result == "wrong"
        assert db.query(QuestionAttemptEvent).filter_by(grading_result_id=first.id).one().final_result == "wrong"
        cycle = db.query(WrongQuestionCycle).one()
        assert cycle.status == "pending"

        second, _profile, _created = persist_grading_result(
            db,
            student=student,
            teacher_id=teacher.id,
            category="daily",
            exam_name="",
            image_path="",
            image_hash="f2" * 32,
            total_questions=1,
            correct_count=0,
            partial_count=0,
            wrong_count=0,
            needs_review_count=1,
            score=None,
            details=[detail],
            knowledge_items=[{"chapter": "函数", "topic": "单调性"}],
        )
        confirm_judgment(
            second.id,
            ConfirmJudgmentRequest(action="correct", detail_index=0),
            db=db,
            user=teacher,
        )

        db.refresh(cycle)
        assert cycle.status == "corrected"
        assert cycle.closed_attempt_id == db.query(QuestionAttemptEvent).filter_by(
            grading_result_id=second.id
        ).one().id
        assert db.query(GradingCorrection).count() == 2
    finally:
        db.close()


def test_voiding_projected_wrong_attempt_removes_it_from_current_wrong_archive():
    Session = _session_factory()
    db = Session()
    try:
        teacher = User(id=1, name="教师", role="teacher")
        student = Student(id=1, teacher_id=teacher.id, name="学生", student_no="001")
        db.add(teacher)
        db.commit()
        db.add(student)
        db.commit()
        detail = {
            "question_no": "1",
            "question": "误判后应作废的题",
            "question_type": "solve",
            "standard_answer": "2",
            "student_answer": "1",
            "result": "wrong",
        }
        result, _profile, _created = persist_grading_result(
            db,
            student=student,
            teacher_id=teacher.id,
            category="daily",
            exam_name="",
            image_path="",
            image_hash="fa" * 32,
            total_questions=1,
            correct_count=0,
            partial_count=0,
            wrong_count=1,
            needs_review_count=0,
            score=None,
            details=[detail],
            knowledge_items=[{"chapter": "代数", "topic": "方程"}],
        )
        cycle = db.query(WrongQuestionCycle).one()

        confirm_judgment(
            result.id,
            ConfirmJudgmentRequest(action="void", detail_index=0),
            db=db,
            user=teacher,
        )

        db.refresh(cycle)
        attempt = db.query(QuestionAttemptEvent).one()
        event = db.query(KnowledgeEvent).one()
        assert cycle.status == "voided"
        assert attempt.analytics_included is False
        assert event.analytics_included is False
        assert wrong_question_summary(db, student.id) == []
        assert correction_rate(db, student.id) == 0.0
    finally:
        db.close()
