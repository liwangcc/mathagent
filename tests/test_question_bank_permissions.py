"""题库删除权限的 API 回归测试。"""

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
from models import QuestionBank
from models_user import User


@pytest.fixture
def bank_client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    session.add_all([
        User(id=1, phone="13900000001", name="本人", role="teacher"),
        User(id=2, phone="13900000002", name="他人", role="teacher"),
    ])
    session.add_all([
        QuestionBank(id=10, teacher_id=1, is_public=False, question="本人私库", answer="A"),
        QuestionBank(id=11, teacher_id=2, is_public=False, question="他人私库", answer="B"),
        QuestionBank(id=12, teacher_id=None, is_public=True, question="公库", answer="C"),
    ])
    session.commit()

    from routers import teacher
    from routers.auth import _require_user

    app = FastAPI()
    app.include_router(teacher.router, prefix="/api")

    def override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[_require_user] = lambda: User(id=1, phone="13900000001", name="本人", role="teacher")
    try:
        yield TestClient(app), Session
    finally:
        session.close()
        engine.dispose()


def test_teacher_can_delete_own_private_question(bank_client):
    client, Session = bank_client
    response = client.delete("/api/teacher/bank/10")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert Session().get(QuestionBank, 10) is None


@pytest.mark.parametrize("question_id", [11, 12])
def test_teacher_cannot_delete_other_private_or_public_question(bank_client, question_id):
    client, Session = bank_client
    response = client.delete(f"/api/teacher/bank/{question_id}")
    assert response.status_code == 403
    assert Session().get(QuestionBank, question_id) is not None