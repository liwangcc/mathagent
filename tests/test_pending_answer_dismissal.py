"""Dismissing a 待入库 suggestion must never rewrite formal grading records."""

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
from models import GradingResult, PendingAnswerDismissal, Student
from models_user import User
from routers import pending_answers
from routers.auth import _require_user

DETAILS = [
    {"question": "1+1=?", "model_answer": "2", "result": "correct", "student_answer": "2"},
    {"question": "求x: 2x=4", "model_answer": "x=2", "result": "wrong", "student_answer": "x=3"},
]


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup = Session()
    setup.add_all([User(id=1, name="教师", role="teacher"), User(id=2, name="另一位", role="teacher")])
    setup.commit()
    for student_id, teacher_id in ((1, 1), (2, 1), (3, 2)):
        setup.add(Student(id=student_id, teacher_id=teacher_id, name=f"学生{student_id}", student_no=f"00{student_id}"))
        setup.commit()
        record = GradingResult(student_id=student_id, total_questions=2, correct_count=1, wrong_count=1)
        record.set_details(DETAILS)
        setup.add(record)
        setup.commit()
    setup.close()

    app = FastAPI()
    app.include_router(pending_answers.router, prefix="/api")

    def override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    current = {"id": 1}
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[_require_user] = lambda: User(
        id=current["id"], name="教师", role="teacher", education_stage="senior",
    )
    try:
        yield TestClient(app), Session, current
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _pending_questions(client):
    return [item["question"] for item in client.get("/api/bank/pending-answers").json()["items"]]


def test_dismissal_hides_every_copy_and_keeps_grade_records_intact(client):
    http, Session, _current = client
    target = next(
        item for item in http.get("/api/bank/pending-answers").json()["items"]
        if item["question"] == "求x: 2x=4"
    )

    response = http.post("/api/bank/pending-answers/delete", json={
        "questions": [target["question"]],
        "sources": [{"grading_id": target["grading_id"], "detail_index": target["detail_index"]}],
    })

    assert response.status_code == 200
    assert response.json()["dismissed"] == 1
    # The copy in the second student's record must not resurface.
    assert _pending_questions(http) == ["1+1=?"]
    db = Session()
    try:
        for record in db.query(GradingResult).all():
            assert [detail["question"] for detail in record.get_details()] == ["1+1=?", "求x: 2x=4"]
            assert record.total_questions == 2
    finally:
        db.close()


def test_dismissed_question_cannot_be_confirmed_into_the_bank(client):
    http, _Session, _current = client
    http.post("/api/bank/pending-answers/delete", json={"questions": ["求x: 2x=4"]})

    response = http.post("/api/bank/confirm-answers", json={
        "answers": [{"question": "求x: 2x=4", "answer": "x=2"}],
    })

    assert response.status_code == 404


def test_dismissal_is_scoped_to_the_teacher(client):
    http, Session, current = client
    http.post("/api/bank/pending-answers/delete", json={"questions": ["求x: 2x=4"]})

    current["id"] = 2
    assert sorted(_pending_questions(http)) == ["1+1=?", "求x: 2x=4"]
    db = Session()
    try:
        assert db.query(PendingAnswerDismissal).filter(PendingAnswerDismissal.teacher_id == 2).count() == 0
    finally:
        db.close()
