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
import services.hermes_tools as hermes_tools


def _session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def test_hermes_pending_tool_excludes_soft_hidden_results(monkeypatch):
    """Hermes must not direct a teacher to pending data hidden from the normal UI."""
    Session = _session_factory()
    setup = Session()
    try:
        teacher = User(id=1, name="教师", role="teacher")
        setup.add(teacher)
        setup.commit()
        student = Student(id=1, teacher_id=teacher.id, name="学生", student_no="001")
        setup.add(student)
        setup.commit()
        hidden = GradingResult(student_id=student.id, deleted_at=datetime.now())
        hidden.set_details([{"question": "隐藏题", "model_answer": "答案"}])
        setup.add(hidden)
        setup.commit()
    finally:
        setup.close()

    monkeypatch.setattr(hermes_tools, "_get_db", lambda: Session())
    monkeypatch.setattr(hermes_tools, "_get_user_id", lambda: 1)

    assert hermes_tools._query_pending({}) == "目前没有待入库的题目。"
