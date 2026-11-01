from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models  # noqa: F401
import models_user  # noqa: F401
import models_rate_limit  # noqa: F401
from database import Base
from models import GradingResult, KnowledgeEvent, Student, StudentProfile, Weakness
from models_user import User


def _session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def test_persist_grading_result_is_idempotent(monkeypatch, tmp_path):
    import services.grading_persistence as gp

    monkeypatch.setattr(gp, "ARCHIVE_DIR", str(tmp_path / "archive"))
    Session = _session()
    db = Session()
    db.add(User(id=1, name="teacher", role="teacher"))
    student = Student(id=1, teacher_id=1, name="张三")
    db.add(student)
    db.commit()

    details = [
        {
            "index": 0,
            "question_number": "1",
            "question": "1+1=?",
            "student_answer": "3",
            "result": "wrong",
        }
    ]
    knowledge = [{
        "question_bank_id": 7,
        "chapter": "代数",
        "topic": "加法",
        "difficulty": 2,
        "error_type": "计算",
    }]

    first, profile, created = gp.persist_grading_result(
        db,
        student=student,
        teacher_id=1,
        category="daily",
        exam_name="",
        image_path="/tmp/paper.png",
        image_hash="abc123",
        total_questions=1,
        correct_count=0,
        partial_count=0,
        wrong_count=1,
        needs_review_count=0,
        score=0,
        details=details,
        knowledge_items=knowledge,
    )
    assert created is True
    assert profile.grading_result_id == first.id
    assert first.score is None
    assert first.max_score is None
    assert first.correct_rate == 0.0
    assert first.scoring_version == 2
    assert profile.score is None
    assert profile.correct_rate == 0.0

    second, profile2, created2 = gp.persist_grading_result(
        db,
        student=student,
        teacher_id=1,
        category="daily",
        exam_name="",
        image_path="/tmp/paper.png",
        image_hash="abc123",
        total_questions=1,
        correct_count=0,
        partial_count=0,
        wrong_count=1,
        needs_review_count=0,
        score=0,
        details=details,
        knowledge_items=knowledge,
    )

    assert created2 is False
    assert second.id == first.id
    assert profile2.id == profile.id
    assert db.query(GradingResult).count() == 1
    assert db.query(StudentProfile).count() == 1
    assert db.query(KnowledgeEvent).count() == 1
    weakness = db.query(Weakness).one()
    assert weakness.chapter == "代数"
    assert weakness.topic == "加法"
    assert weakness.occurrences == 1


def test_persist_exam_keeps_actual_points_and_snapshot_metrics(monkeypatch, tmp_path):
    import services.grading_persistence as gp

    monkeypatch.setattr(gp, "ARCHIVE_DIR", str(tmp_path / "archive"))
    Session = _session()
    db = Session()
    db.add(User(id=1, name="teacher", role="teacher"))
    student = Student(id=2, teacher_id=1, name="李四")
    db.add(student)
    db.commit()

    result, profile, created = gp.persist_grading_result(
        db,
        student=student,
        teacher_id=1,
        category="exam",
        exam_name="期中考试",
        image_path="/tmp/exam.png",
        image_hash="exam-hash",
        total_questions=2,
        correct_count=1,
        partial_count=1,
        wrong_count=0,
        needs_review_count=0,
        score=7.5,
        max_score=12.0,
        correct_rate=0.5,
        details=[
            {"question_no": "1", "result": "correct", "max_score": 5.0, "awarded_score": 5.0},
            {"question_no": "2", "result": "partial", "max_score": 7.0, "awarded_score": 2.5},
        ],
    )

    assert created is True
    assert (result.score, result.max_score, result.correct_rate, result.scoring_version) == (7.5, 12.0, 0.5, 2)
    assert (profile.score, profile.max_score, profile.correct_rate, profile.scoring_version) == (7.5, 12.0, 0.5, 2)


def test_same_image_with_revised_scoring_contract_is_not_reused(monkeypatch, tmp_path):
    """A corrected mark scheme must create a new snapshot, not replay old points."""
    import services.grading_persistence as gp

    monkeypatch.setattr(gp, "ARCHIVE_DIR", str(tmp_path / "archive"))
    Session = _session()
    db = Session()
    db.add(User(id=1, name="teacher", role="teacher"))
    student = Student(id=20, teacher_id=1, name="改分学生")
    db.add(student)
    db.commit()

    base = {
        "db": db,
        "student": student,
        "teacher_id": 1,
        "category": "exam",
        "exam_name": "期末考试",
        "image_path": "/tmp/same-paper.png",
        "image_hash": "same-paper-hash",
        "total_questions": 1,
        "correct_count": 1,
        "partial_count": 0,
        "wrong_count": 0,
        "needs_review_count": 0,
    }
    first, _, first_created = gp.persist_grading_result(
        **base,
        score=5.0,
        max_score=5.0,
        correct_rate=1.0,
        details=[{
            "question_no": "1", "question": "1+1=?", "question_type": "fill",
            "standard_answer": "2", "grading_method": "exact",
            "result": "correct", "max_score": 5.0, "awarded_score": 5.0,
        }],
    )
    revised, _, revised_created = gp.persist_grading_result(
        **base,
        score=10.0,
        max_score=10.0,
        correct_rate=1.0,
        details=[{
            "question_no": "1", "question": "1+1=?", "question_type": "fill",
            "standard_answer": "2", "grading_method": "exact",
            "result": "correct", "max_score": 10.0, "awarded_score": 10.0,
        }],
    )
    replay, _, replay_created = gp.persist_grading_result(
        **base,
        score=10.0,
        max_score=10.0,
        correct_rate=1.0,
        details=[{
            "question_no": "1", "question": "1+1=?", "question_type": "fill",
            "standard_answer": "2", "grading_method": "exact",
            "result": "correct", "max_score": 10.0, "awarded_score": 10.0,
        }],
    )

    assert first_created is True
    assert revised_created is True
    assert replay_created is False
    assert (first.id, first.score, first.max_score) != (revised.id, revised.score, revised.max_score)
    assert replay.id == revised.id
    assert db.query(GradingResult).count() == 2
    assert db.query(StudentProfile).count() == 2
    archive_files = list((tmp_path / "archive").rglob("*.json"))
    assert len(archive_files) == 2
    assert len({path.name for path in archive_files}) == 2


def test_persist_rejects_v2_exam_without_positive_max_score(monkeypatch, tmp_path):
    import pytest
    import services.grading_persistence as gp

    monkeypatch.setattr(gp, "ARCHIVE_DIR", str(tmp_path / "archive"))
    Session = _session()
    db = Session()
    db.add(User(id=1, name="teacher", role="teacher"))
    student = Student(id=3, teacher_id=1, name="王五")
    db.add(student)
    db.commit()

    with pytest.raises(ValueError, match="positive max_score"):
        gp.persist_grading_result(
            db,
            student=student,
            teacher_id=1,
            category="exam",
            exam_name="期末考试",
            image_path="/tmp/exam2.png",
            image_hash="exam-hash-2",
            total_questions=1,
            correct_count=1,
            partial_count=0,
            wrong_count=0,
            needs_review_count=0,
            score=5.0,
            details=[{"question_no": "1", "result": "correct"}],
        )


def test_persist_rejects_removed_review_category(monkeypatch, tmp_path):
    import pytest
    import services.grading_persistence as gp

    monkeypatch.setattr(gp, "ARCHIVE_DIR", str(tmp_path / "archive"))
    Session = _session()
    db = Session()
    db.add(User(id=1, name="teacher", role="teacher"))
    student = Student(id=4, teacher_id=1, name="赵六")
    db.add(student)
    db.commit()

    with pytest.raises(ValueError, match="daily.*exam"):
        gp.persist_grading_result(
            db,
            student=student,
            teacher_id=1,
            category="review",
            exam_name="",
            image_path="/tmp/review.png",
            image_hash="review-hash",
            total_questions=1,
            correct_count=0,
            partial_count=0,
            wrong_count=1,
            needs_review_count=0,
            score=None,
            details=[{"question_no": "1", "result": "wrong"}],
        )


def test_deferred_archive_runs_only_after_outer_transaction_commit(monkeypatch):
    import services.grading_persistence as gp

    Session = _session()
    db = Session()
    db.add(User(id=1, name="teacher", role="teacher"))
    student = Student(id=31, teacher_id=1, name="Student")
    db.add(student)
    db.commit()

    events = []
    jobs = []
    original_commit = db.commit

    def tracked_commit():
        events.append("commit")
        return original_commit()

    def tracked_archive(*_args, **_kwargs):
        events.append("archive")

    monkeypatch.setattr(db, "commit", tracked_commit)
    monkeypatch.setattr(gp, "_write_filesystem_archive", tracked_archive)

    gp.persist_grading_result(
        db,
        student=student,
        teacher_id=1,
        category="daily",
        exam_name="",
        image_path="/tmp/deferred.png",
        image_hash="deferred-hash",
        total_questions=1,
        correct_count=1,
        partial_count=0,
        wrong_count=0,
        needs_review_count=0,
        score=None,
        details=[{"question_no": "1", "result": "correct"}],
        commit=False,
        archive_jobs=jobs,
    )

    assert events == []
    assert len(jobs) == 1
    db.commit()
    gp.write_deferred_filesystem_archives(jobs)
    assert events == ["commit", "archive"]
