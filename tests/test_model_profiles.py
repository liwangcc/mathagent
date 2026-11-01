"""模型密钥「职责组」配置：解析顺序、两层写入、连接测试的安全边界。

关键约束：
- 解析顺序必须是 组级 → 全局环境变量 → config 默认值；
- 简单模式填一个 key 要写满该槽内所有组（学生槽 = 转录 + 批阅）；
- 专家模式只改指定组，留空字段继续继承；
- 用已保存的密钥测试连接时不能改 base_url（防把服务器密钥发到外部地址），
  自填 base_url 时仍要过 SSRF 检查。
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import database
import models  # noqa: F401
import models_model_profile  # noqa: F401
import models_rate_limit  # noqa: F401
import models_user  # noqa: F401
from database import Base, get_db
from models_user import User


@pytest.fixture(autouse=True)
def _clean_profiles():
    from services import model_profiles

    Base.metadata.create_all(bind=database.engine)
    session = database.SessionLocal()
    try:
        session.query(models_model_profile.ModelProfile).delete()
        session.commit()
    finally:
        session.close()
    model_profiles.invalidate_cache()
    yield
    session = database.SessionLocal()
    try:
        session.query(models_model_profile.ModelProfile).delete()
        session.commit()
    finally:
        session.close()
    model_profiles.invalidate_cache()


@pytest.fixture
def client(tmp_path):
    from routers.settings import router as settings_router

    engine = create_engine(
        f"sqlite:///{tmp_path / 'model_profiles.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()
    app.include_router(settings_router, prefix="/api")
    app.dependency_overrides[get_db] = _override_db
    test_client = TestClient(app)
    try:
        yield test_client
    finally:
        engine.dispose()


@pytest.fixture
def admin_headers(client):
    from services.auth_service import create_token, hash_password

    session = next(client.app.dependency_overrides[get_db]())
    try:
        user = User(
            phone="13900006001",
            name="管理员",
            role="admin",
            status="active",
            school="",
            password_hash=hash_password("AdminPass123"),
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return {"Authorization": f"Bearer {create_token(user.id)}"}
    finally:
        session.close()


# ---------- 解析顺序 ----------

def test_group_value_beats_env(monkeypatch):
    from services import model_profiles

    monkeypatch.setenv("DASHSCOPE_API_KEY", "env-key-xxxxxxxx")
    monkeypatch.setattr(config, "DASHSCOPE_BASE_URL", "https://env.example.com/v1", raising=False)

    model_profiles.write_group(
        "student_transcribe",
        api_key="group-key-yyyyyyyy",
        base_url="https://group.example.com/v1",
        model="qwen3.8-flash",
    )
    resolved = model_profiles.resolve("student_transcribe")
    assert resolved["api_key"] == "group-key-yyyyyyyy"
    assert resolved["base_url"] == "https://group.example.com/v1"
    assert resolved["source"] == "group"


def test_env_used_when_group_empty(monkeypatch):
    from services import model_profiles

    monkeypatch.setenv("DASHSCOPE_API_KEY", "env-key-xxxxxxxx")
    resolved = model_profiles.resolve("student_grade")
    assert resolved["api_key"] == "env-key-xxxxxxxx"
    assert resolved["source"] == "env"


@pytest.mark.parametrize("group", ["template_preprocess", "bank_classify"])
def test_removed_groups_without_consumers_are_unknown(group):
    """Nothing read these two groups, so they are gone rather than kept as dead knobs."""
    from services import model_profiles

    assert group not in model_profiles.GROUPS
    with pytest.raises(ValueError):
        model_profiles.resolve(group)


def test_api_key_is_encrypted_at_rest():
    """库里不能出现明文 Key；解析时再解密。"""
    from services import model_profiles
    from database import SessionLocal
    from models_model_profile import ModelProfile

    plaintext = "sk-plaintext-should-never-be-stored"
    model_profiles.write_group("student_grade", api_key=plaintext)

    session = SessionLocal()
    try:
        row = session.query(ModelProfile).filter(ModelProfile.group == "student_grade").first()
        stored = row.api_key
    finally:
        session.close()

    assert stored.startswith("enc:v1:")
    assert plaintext not in stored
    assert model_profiles.resolve("student_grade")["api_key"] == plaintext


def test_tampered_ciphertext_is_unconfigured(monkeypatch):
    import base64

    from services import model_profiles, secret_box
    from database import SessionLocal
    from models_model_profile import ModelProfile

    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    model_profiles.write_group("student_grade", api_key="sk-to-be-tampered-0001")

    session = SessionLocal()
    try:
        row = session.query(ModelProfile).filter(ModelProfile.group == "student_grade").first()
        raw = row.api_key[len(secret_box.PREFIX):]
        broken = bytearray(base64.b64decode(raw))
        broken[-1] ^= 0xFF
        row.api_key = secret_box.PREFIX + base64.b64encode(bytes(broken)).decode()
        session.commit()
    finally:
        session.close()

    model_profiles.invalidate_cache()
    resolved = model_profiles.resolve("student_grade")
    assert resolved["api_key"] == ""
    assert resolved["source"] == "default"


# ---------- HTTP 两层写入 ----------

def test_simple_mode_writes_every_group(client, admin_headers):
    resp = client.post(
        "/api/settings/model-profiles/simple",
        json={
            "template": {"api_key": "template-key-1111", "model": "qwen3.8-max"},
            "student": {"api_key": "student-key-2222", "model": "qwen3.8-flash"},
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body["changed"]) == {
        "template_solve",
        "student_transcribe",
        "student_grade",
    }
    assert body["needs_setup"] is False

    snapshot = client.get("/api/settings/model-profiles", headers=admin_headers).json()
    groups = {item["group"]: item for item in snapshot["groups"]}
    assert groups["template_solve"]["api_key_masked"].startswith("temp")
    assert groups["student_grade"]["api_key_masked"].startswith("stud")
    # 明文密钥永不出现在响应里
    assert "student-key-2222" not in resp.text


def test_expert_mode_updates_single_group(client, admin_headers):
    client.post(
        "/api/settings/model-profiles",
        json={"group": "student_grade", "api_key": "grade-only-key-3333"},
        headers=admin_headers,
    )
    snapshot = client.get("/api/settings/model-profiles", headers=admin_headers).json()
    groups = {item["group"]: item for item in snapshot["groups"]}
    assert groups["student_grade"]["configured"] is True
    assert groups["student_transcribe"]["configured"] is False


def test_expert_mode_rejects_unknown_group(client, admin_headers):
    resp = client.post(
        "/api/settings/model-profiles",
        json={"group": "made_up_group", "api_key": "x"},
        headers=admin_headers,
    )
    assert resp.status_code == 400


def test_profile_endpoints_require_admin(client):
    assert client.get("/api/settings/model-profiles").status_code == 401
    assert (
        client.post(
            "/api/settings/model-profiles/simple",
            json={"student": {"api_key": "x"}},
        ).status_code
        == 401
    )


def test_needs_setup_flag_on_settings(client, admin_headers, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("TEMPLATE_CUT_API_KEY", raising=False)
    body = client.get("/api/settings", headers=admin_headers).json()
    assert body["model_profiles"]["needs_setup"] is True

    client.post(
        "/api/settings/model-profiles/simple",
        json={"student": {"api_key": "student-key-4444"}},
        headers=admin_headers,
    )
    body = client.get("/api/settings", headers=admin_headers).json()
    assert body["model_profiles"]["needs_setup"] is False


# ---------- 连接测试的安全边界 ----------

def test_test_connection_rejects_url_change_with_stored_key(
    client, admin_headers, monkeypatch
):
    import routers.settings as settings_module

    # 测试环境禁网，DNS 解析会被拦截；这里只验证「不允许换地址」这条业务规则，
    # SSRF 解析规则由下面的 loopback 用例覆盖。
    monkeypatch.setattr(settings_module, "_ensure_public_url", lambda url: None)
    saved = client.post(
        "/api/settings/model-profiles",
        json={
            "group": "student_grade",
            "api_key": "grade-key-5555",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        },
        headers=admin_headers,
    )
    assert saved.status_code == 200, saved.text
    resp = client.post(
        "/api/settings/model-profiles/test",
        json={"group": "student_grade", "base_url": "https://attacker.example.com/v1"},
        headers=admin_headers,
    )
    assert resp.status_code == 400
    assert "不能切换到其它地址" in resp.json()["detail"]


def test_test_connection_blocks_private_address(client, admin_headers):
    resp = client.post(
        "/api/settings/model-profiles/test",
        json={
            "group": "student_grade",
            "api_key": "supplied-key-6666",
            "base_url": "http://127.0.0.1:8080/v1",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 400
    assert "内网" in resp.json()["detail"]


def test_test_connection_requires_a_key(client, admin_headers, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    resp = client.post(
        "/api/settings/model-profiles/test",
        json={"group": "student_grade"},
        headers=admin_headers,
    )
    assert resp.status_code == 400
    assert "API Key" in resp.json()["detail"]