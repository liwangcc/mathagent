"""Auth contract tests with dependency stubs: no application DB, config or .env imports."""
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def auth_api(monkeypatch):
    class User:
        education_stage = None

        def __init__(self):
            self.id = 1
            self.name = "测试教师"
            self.school = ""
            self.current_class_id = 1
            self.education_stage = None

        def to_dict(self):
            return {"id": self.id, "name": self.name, "school": self.school,
                    "education_stage": "untrusted-serializer-default", "profile_complete": True}

    class DB:
        commits = 0

        def commit(self):
            self.commits += 1

        def refresh(self, user):
            pass

    database = ModuleType("database")
    database.get_db = lambda: None
    model = ModuleType("models_user")
    model.User = User
    auth_service = ModuleType("services.auth_service")
    for name in ["send_sms_code", "verify_sms_code", "create_token", "get_current_user",
                 "build_wechat_authorize_url", "hash_password", "verify_password",
                 "clear_password_login_failures", "get_password_login_lock", "record_password_login_failure"]:
        setattr(auth_service, name, lambda *args, **kwargs: None)
    ip_service = ModuleType("services.ip_rate_limit")
    for name in ["get_ip_lock", "record_ip_failure", "clear_ip_failures"]:
        setattr(ip_service, name, lambda *args, **kwargs: None)
    for module in [database, model, auth_service, ip_service]:
        monkeypatch.setitem(sys.modules, module.__name__, module)
    path = Path(__file__).resolve().parents[1] / "routers" / "auth.py"
    spec = importlib.util.spec_from_file_location("mathagent_stage_auth_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    user, db = User(), DB()
    app = FastAPI()
    app.include_router(module.router, prefix="/api")
    app.dependency_overrides[module._require_user] = lambda: user
    app.dependency_overrides[database.get_db] = lambda: db
    with TestClient(app) as client:
        yield client, user, db, module


def test_missing_stage_profile_is_incomplete_without_guessing(auth_api):
    client, user, db, _ = auth_api
    response = client.get("/api/auth/me")
    assert response.status_code == 200
    assert response.json()["user"]["education_stage"] is None
    assert response.json()["user"]["profile_complete"] is False
    assert user.education_stage is None
    assert db.commits == 0


@pytest.mark.parametrize("stage", ["primary", "junior", "senior"])
def test_save_only_stage_and_read_back_from_server(auth_api, stage):
    client, user, db, _ = auth_api
    response = client.put("/api/auth/me", json={"education_stage": stage})
    assert response.status_code == 200
    assert user.education_stage == stage
    assert db.commits == 1
    persisted = client.get("/api/auth/me").json()["user"]
    assert persisted["education_stage"] == stage
    assert persisted["profile_complete"] is True


@pytest.mark.parametrize("value", [None, "", "高中", "university", "Senior", True, [], {}])
def test_illegal_stage_rejected_without_mutation(auth_api, value):
    client, user, db, _ = auth_api
    user.education_stage = "junior"
    response = client.put("/api/auth/me", json={"education_stage": value})
    assert response.status_code == 422
    assert user.education_stage == "junior"
    assert db.commits == 0


@pytest.mark.parametrize("field", ["rules_version", "curriculum_version", "profile_complete", "role", "subject"])
def test_client_cannot_override_server_derived_fields(auth_api, field):
    client, user, db, _ = auth_api
    response = client.put("/api/auth/me", json={"education_stage": "junior", field: "forged"})
    assert response.status_code == 422
    assert user.education_stage is None
    assert db.commits == 0


def test_existing_name_edit_does_not_reset_stage(auth_api):
    client, user, _, _ = auth_api
    user.education_stage = "senior"
    response = client.put("/api/auth/me", json={"name": "测试老师", "school": "示例学校"})
    assert response.status_code == 200
    assert response.json()["user"]["education_stage"] == "senior"


def test_no_false_success_before_user_model_migration(auth_api, monkeypatch):
    client, user, db, module = auth_api
    monkeypatch.delattr(module.User, "education_stage")
    response = client.put("/api/auth/me", json={"education_stage": "junior"})
    assert response.status_code == 503
    assert user.education_stage is None
    assert db.commits == 0


def test_real_user_model_persists_stage_in_isolated_sqlite(auth_api, monkeypatch, tmp_path):
    """Use the actual ORM file and a fresh SQLite file, not an app/default database."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import declarative_base, sessionmaker

    client, _, _, auth = auth_api
    database = sys.modules["database"]
    database.Base = declarative_base()
    path = Path(__file__).resolve().parents[1] / "models_user.py"
    spec = importlib.util.spec_from_file_location("mathagent_stage_user_model_contract", path)
    model = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(model)
    engine = create_engine(f"sqlite:///{(tmp_path / 'isolated-users.db').as_posix()}", connect_args={"check_same_thread": False})
    database.Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    try:
        with TestSession() as session:
            user = model.User(name="隔离测试教师", current_class_id=1)
            session.add(user)
            session.commit()
            session.refresh(user)
            assert user.education_stage == ""
            assert user.to_dict()["profile_complete"] is False
            user_id = user.id
            monkeypatch.setattr(auth, "User", model.User)
            client.app.dependency_overrides[auth._require_user] = lambda: user
            client.app.dependency_overrides[database.get_db] = lambda: session
            for stage in ("primary", "junior", "senior"):
                response = client.put("/api/auth/me", json={"education_stage": stage})
                assert response.status_code == 200
                assert response.json()["user"]["profile_complete"] is True
                with TestSession() as independent_session:
                    saved = independent_session.scalar(select(model.User).where(model.User.id == user_id))
                    assert saved.education_stage == stage
                    assert saved.to_dict()["education_stage"] == stage
    finally:
        engine.dispose()
