import asyncio
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
import services.chat_handlers as chat_handlers


def _session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def test_chat_pending_excludes_soft_hidden_grading_records(monkeypatch, tmp_path):
    """A hidden grade record must not tell a teacher to process an invisible item."""
    monkeypatch.setattr(chat_handlers, "FAILED_DIR", str(tmp_path))
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
            total_questions=1,
            wrong_count=1,
            score=0.0,
            deleted_at=datetime.now(),
        )
        hidden_result.set_details([{"result": "wrong", "confirmed": False}])
        db.add(hidden_result)
        db.commit()

        response = chat_handlers.handle_pending(db, teacher)

        assert response["data"].get("judging_count", 0) == 0
        assert "需确认的题目" not in response["response"]
    finally:
        db.close()


def test_chat_summary_rejects_removed_review_type():
    response = asyncio.run(
        chat_handlers.handle_summary({"type": "review"}, None, None)
    )

    assert response["data"] == {}
    assert "仅支持日常作业或考试" in response["response"]
