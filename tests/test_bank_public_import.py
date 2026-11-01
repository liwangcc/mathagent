from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401
import models_rate_limit  # noqa: F401
import models_user  # noqa: F401
from database import Base, get_db
from models import QuestionBank
from models_user import User
from routers.auth import _require_user
from routers.bank_import import router


def _build_app(tmp_path, user: User):
    engine = create_engine(f"sqlite:///{tmp_path / 'bank.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_db():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[_require_user] = lambda: user
    return app, SessionLocal


def test_teacher_cannot_import_public_bank(tmp_path):
    app, _ = _build_app(tmp_path, User(id=1, name="teacher", role="teacher"))
    client = TestClient(app, raise_server_exceptions=False)

    r = client.post("/api/admin/bank/public-import/save", json={
        "items": [{"question_no": "1", "content": "1+1", "answer": "2"}],
    })

    assert r.status_code == 403


def test_admin_imports_public_bank_without_private_scope_dedupe(tmp_path):
    app, SessionLocal = _build_app(tmp_path, User(id=99, name="admin", role="admin", education_stage="junior"))

    db = SessionLocal()
    try:
        db.add(QuestionBank(
            teacher_id=1,
            is_public=False,
            question="1+1",
            answer="2",
            source="private_seed",
        ))
        db.commit()
    finally:
        db.close()

    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/admin/bank/public-import/save", json={
        "items": [
            {"question_no": "1", "content": "1+1", "answer": "2", "chapter": "有理数与实数", "topic": "有理数与数轴", "question_type": "fill"},
            {"question_no": "2", "content": "1+1", "answer": "2"},
        ],
    })

    assert r.status_code == 200
    assert r.json()["inserted"] == 1
    assert r.json()["skipped"] == 1

    db = SessionLocal()
    try:
        public = db.query(QuestionBank).filter(QuestionBank.is_public.is_(True)).one()
        assert public.teacher_id is None
        assert public.question == "1+1"
        assert public.chapter == "有理数与实数"
        assert public.topic == "有理数与数轴"
        assert public.question_type == "fill"
        assert public.source == "admin_public"
        assert db.query(QuestionBank).count() == 2
    finally:
        db.close()


def test_teacher_photo_save_persists_explicit_and_inferred_question_types(tmp_path):
    app, SessionLocal = _build_app(tmp_path, User(id=7, name="teacher", role="teacher", education_stage="junior"))
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/api/bank/photo-import/save", json={
        "items": [
            {
                "question_no": "1",
                "content": "函数值为（ ） A. 1 B. 2 C. 3 D. 4",
                "answer": "B",
                "question_type": "choice",
                "chapter": "有理数与实数",
                "topic": "有理数与数轴",
            },
            {
                "question_no": "2",
                "content": "函数值为________________。",
                "answer": "3",
                "chapter": "有理数与实数",
                "topic": "有理数与数轴",
            },
        ],
    })

    assert response.status_code == 200, response.text
    db = SessionLocal()
    try:
        rows = db.query(QuestionBank).order_by(QuestionBank.id).all()
        assert [row.question_type for row in rows] == ["choice", "fill"]
        assert all(row.teacher_id == 7 and not row.is_public for row in rows)
        assert rows[0].topic == "有理数与数轴"
    finally:
        db.close()
