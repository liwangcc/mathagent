from datetime import datetime

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
from models import GradingResult, Student
from models_user import User
from routers.auth import _require_user
from routers import pending_answers


@pytest.fixture
def pending_answers_client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    setup = Session()
    try:
        teacher = User(id=1, name="教师", role="teacher")
        setup.add(teacher)
        setup.commit()
        student = Student(id=1, teacher_id=teacher.id, name="学生", student_no="001")
        setup.add(student)
        setup.commit()
        hidden = GradingResult(student_id=student.id, deleted_at=datetime.now())
        hidden.set_details([{
            "question": "隐藏待入库题",
            "model_answer": "标准答案",
            "result": "wrong",
        }])
        setup.add(hidden)
        setup.commit()
        hidden_id = hidden.id
    finally:
        setup.close()

    app = FastAPI()
    app.include_router(pending_answers.router, prefix="/api")

    def override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[_require_user] = lambda: User(id=1, name="教师", role="teacher")
    client = TestClient(app)
    try:
        yield client, Session, hidden_id
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_pending_answers_ignores_hidden_results_and_never_rewrites_them(pending_answers_client):
    """Hidden grade records must not appear in or be mutated by pending-answer cleanup."""
    client, Session, hidden_id = pending_answers_client

    listed = client.get("/api/bank/pending-answers")
    assert listed.status_code == 200
    assert listed.json()["count"] == 0

    deleted = client.request(
        "POST",
        "/api/bank/pending-answers/delete",
        json={"questions": ["隐藏待入库题"]},
    )
    assert deleted.status_code == 200
    assert deleted.json()["removed"] == 0

    db = Session()
    try:
        assert db.get(GradingResult, hidden_id).get_details()[0]["question"] == "隐藏待入库题"
    finally:
        db.close()
