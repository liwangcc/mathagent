from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models  # noqa: F401
import models_rate_limit  # noqa: F401
import models_user  # noqa: F401
from database import Base
from models import GradingResult, Student
from models_user import User
from services.grading_persistence import persist_grading_result


def _session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def test_reupload_after_soft_hide_creates_visible_result_instead_of_reusing_hidden_row():
    """A re-upload must not mutate or revive the record that a teacher chose to hide."""
    Session = _session_factory()
    db = Session()
    try:
        teacher = User(id=1, name="教师", role="teacher")
        db.add(teacher)
        db.commit()
        student = Student(id=1, teacher_id=teacher.id, name="学生", student_no="001")
        db.add(student)
        db.commit()
        hidden = GradingResult(
            student_id=student.id,
            category="daily",
            exam_name="练习",
            image_content_hash="same-image-hash",
            deleted_at=datetime.now(),
            total_questions=1,
            score=0.0,
        )
        hidden.set_details([])
        db.add(hidden)
        db.commit()
        hidden_id = hidden.id

        visible, _profile, created = persist_grading_result(
            db,
            student=student,
            teacher_id=teacher.id,
            category="daily",
            exam_name="练习",
            image_path="",
            image_hash="same-image-hash",
            total_questions=1,
            correct_count=1,
            partial_count=0,
            wrong_count=0,
            needs_review_count=0,
            score=1.0,
            details=[{"question": "新上传题", "result": "correct"}],
        )

        assert created is True
        assert visible.id != hidden_id
        assert visible.deleted_at is None
        all_same_hash = (
            db.query(GradingResult)
            .filter(
                GradingResult.student_id == student.id,
                GradingResult.image_content_hash == "same-image-hash",
            )
            .all()
        )
        assert len(all_same_hash) == 2
        assert sum(row.deleted_at is None for row in all_same_hash) == 1
        assert db.get(GradingResult, hidden_id).deleted_at is not None
        assert db.get(GradingResult, hidden_id).get_details() == []
    finally:
        db.close()
