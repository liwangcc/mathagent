from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models  # noqa: F401
import models_rate_limit  # noqa: F401
import models_user  # noqa: F401
from database import Base
from models import KnowledgeEvent, QuestionBank, Student
from models_user import User
from scripts import dedup_db


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


def test_dedup_remaps_history_within_owner_scope_without_deleting_other_teacher_data(monkeypatch):
    """The maintenance CLI deduplicates one private bank without crossing teacher ownership."""
    Session = _session_factory()
    setup = Session()
    try:
        teacher_a = User(id=1, name="甲老师", role="teacher")
        teacher_b = User(id=2, name="乙老师", role="teacher")
        setup.add_all([teacher_a, teacher_b])
        setup.commit()
        student = Student(id=1, teacher_id=teacher_a.id, name="学生", student_no="001")
        setup.add(student)
        setup.commit()
        old_question = QuestionBank(
            id=1,
            teacher_id=teacher_a.id,
            is_public=False,
            question="求 x + 1 = 2 的解",
            answer="x=1",
        )
        retained_question = QuestionBank(
            id=2,
            teacher_id=teacher_a.id,
            is_public=False,
            question="求 x + 1 = 2 的解",
            answer="x=1",
        )
        other_teacher_question = QuestionBank(
            id=3,
            teacher_id=teacher_b.id,
            is_public=False,
            question="求 x + 1 = 2 的解",
            answer="x=1",
        )
        setup.add_all([old_question, retained_question, other_teacher_question])
        setup.commit()
        history = KnowledgeEvent(
            student_id=student.id,
            teacher_id=teacher_a.id,
            question_bank_id=old_question.id,
            chapter="代数",
            topic="一元一次方程",
            result="wrong",
        )
        setup.add(history)
        setup.commit()
        old_question_id = old_question.id
        retained_question_id = retained_question.id
        other_teacher_question_id = other_teacher_question.id
        history_id = history.id
    finally:
        setup.close()

    monkeypatch.setattr(dedup_db, "SessionLocal", Session)
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")

    assert dedup_db.main() == 0

    verify = Session()
    try:
        assert verify.get(QuestionBank, old_question_id) is None
        assert verify.get(QuestionBank, retained_question_id) is not None
        assert verify.get(QuestionBank, other_teacher_question_id) is not None
        assert verify.get(KnowledgeEvent, history_id).question_bank_id == retained_question_id
    finally:
        verify.close()
