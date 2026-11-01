"""管理员建号 + 可插拔验证码/短信开关的回归测试。

背景：私有部署最常见的两种环境——
1) 有阿里云账号：图形验证走阿里云，短信登录照旧；
2) 没有任何阿里云账号（Docker 默认）：SMS_ENABLED=0 只留密码登录，
   管理员用 /auth/admin/user 直接建号，图形验证用服务端内置算术题。
两条路径都必须真实可用，不能只做 UI 隐藏。
"""

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
from database import Base, get_db


@pytest.fixture
def client(tmp_path):
    import models  # noqa: F401
    import models_rate_limit  # noqa: F401
    import models_user  # noqa: F401
    from routers.auth import router as auth_router

    engine = create_engine(
        f"sqlite:///{tmp_path / 'auth_admin.db'}", connect_args={"check_same_thread": False}
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
    app.include_router(auth_router, prefix="/api")
    app.dependency_overrides[get_db] = _override_db
    test_client = TestClient(app)
    try:
        yield test_client
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    from services import auth_service, auth_tickets, sms_throttle

    auth_service._clear_sms_store()
    auth_tickets.reset_for_tests()
    sms_throttle.reset_for_tests()
    monkeypatch.setattr(auth_service, "DEV_MODE", True)
    monkeypatch.setattr(config, "SMS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "CAPTCHA_PROVIDER", "builtin", raising=False)
    yield
    auth_service._clear_sms_store()
    auth_tickets.reset_for_tests()
    sms_throttle.reset_for_tests()


def _create_admin(client):
    from services.auth_service import create_token, hash_password

    session = next(client.app.dependency_overrides[get_db]())
    try:
        from models_user import User

        user = User(
            phone="13900005001",
            name="管理员",
            role="admin",
            status="active",
            school="",
            password_hash=hash_password("AdminPass123"),
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user.id, create_token(user.id)
    finally:
        session.close()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------- 短信开关 ----------

def test_sms_endpoints_refuse_when_disabled(client, monkeypatch):
    monkeypatch.setattr(config, "SMS_ENABLED", False, raising=False)

    send = client.post("/api/auth/sms/send", json={"phone": "13900005101"})
    assert send.status_code == 400
    assert "未开启短信登录" in send.json()["detail"]

    login = client.post(
        "/api/auth/sms/login", json={"phone": "13900005101", "code": "123456"}
    )
    assert login.status_code == 400
    assert "未开启短信登录" in login.json()["detail"]


def test_features_endpoint_reports_capabilities(client, monkeypatch):
    body = client.get("/api/auth/features").json()
    assert body["sms_enabled"] is True
    assert body["captcha_provider"] == "builtin"
    assert body["captcha_required"] is True

    monkeypatch.setattr(config, "SMS_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "CAPTCHA_PROVIDER", "off", raising=False)
    body = client.get("/api/auth/features").json()
    assert body["sms_enabled"] is False
    assert body["captcha_required"] is False
    assert "未开启短信登录" in body["sms_hint"]


# ---------- 内置算术验证码 ----------

def _solve(problem: str) -> str:
    match = re.fullmatch(r"(\d+)\s*([+-])\s*(\d+)\s*=\s*\?", problem.strip())
    assert match, problem
    left, op, right = int(match.group(1)), match.group(2), int(match.group(3))
    return str(left + right if op == "+" else left - right)


def test_builtin_captcha_issues_single_use_ticket(client):
    challenge = client.get("/api/auth/captcha/challenge").json()
    assert challenge["provider"] == "builtin"
    assert challenge["problem"].endswith("?")
    answer = _solve(challenge["problem"])

    phone = "13900005201"
    ok = client.post(
        "/api/auth/captcha/verify",
        json={
            "captcha_verify_param": f'{{"token": "{challenge["token"]}", "answer": "{answer}"}}',
            "phone": phone,
        },
    )
    assert ok.status_code == 200, ok.text
    ticket = ok.json()["ticket"]
    assert ticket

    # 票据可用于发短信，且只能一次
    first = client.post(
        "/api/auth/sms/send", json={"phone": phone, "captcha_ticket": ticket}
    )
    assert first.status_code == 200, first.text
    second = client.post(
        "/api/auth/sms/send", json={"phone": phone, "captcha_ticket": ticket}
    )
    assert second.status_code == 400

    # 同一道题的 token 也已被核销，重放失败
    replay = client.post(
        "/api/auth/captcha/verify",
        json={
            "captcha_verify_param": f'{{"token": "{challenge["token"]}", "answer": "{answer}"}}',
            "phone": phone,
        },
    )
    assert replay.status_code == 400


def test_builtin_captcha_rejects_wrong_answer(client):
    challenge = client.get("/api/auth/captcha/challenge").json()
    right = int(_solve(challenge["problem"]))
    wrong = str(right + 1)
    resp = client.post(
        "/api/auth/captcha/verify",
        json={
            "captcha_verify_param": f'{{"token": "{challenge["token"]}", "answer": "{wrong}"}}',
        },
    )
    assert resp.status_code == 400
    assert "答案不正确" in resp.json()["detail"]


def test_builtin_captcha_rejects_forged_token(client):
    resp = client.post(
        "/api/auth/captcha/verify",
        json={"captcha_verify_param": '{"token": "a.1.b.c", "answer": "3"}'},
    )
    assert resp.status_code == 400


# ---------- 管理员建号 ----------

def test_admin_creates_teacher_and_teacher_can_password_login(client):
    _admin_id, token = _create_admin(client)

    created = client.post(
        "/api/auth/admin/user",
        json={
            "name": "王老师",
            "phone": "13900005301",
            "school": "实验中学",
            "password": "Teacher123",
        },
        headers=_auth(token),
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["login_account"] == "13900005301"
    assert body["generated_account"] is False
    assert body["user"]["role"] == "teacher"

    login = client.post(
        "/api/auth/password/login",
        json={"phone": "13900005301", "password": "Teacher123"},
    )
    assert login.status_code == 200, login.text
    assert login.json()["user"]["name"] == "王老师"


def test_admin_creates_teacher_without_phone_gets_generated_account(client):
    _admin_id, token = _create_admin(client)

    created = client.post(
        "/api/auth/admin/user",
        json={"name": "李老师", "password": "Teacher123"},
        headers=_auth(token),
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["generated_account"] is True
    account = body["login_account"]
    assert re.fullmatch(r"199\d{8}", account)

    login = client.post(
        "/api/auth/password/login", json={"phone": account, "password": "Teacher123"}
    )
    assert login.status_code == 200


def test_admin_create_user_validates_input(client):
    _admin_id, token = _create_admin(client)

    weak = client.post(
        "/api/auth/admin/user",
        json={"name": "赵老师", "phone": "13900005401", "password": "123456"},
        headers=_auth(token),
    )
    assert weak.status_code == 400
    assert "密码" in weak.json()["detail"]

    first = client.post(
        "/api/auth/admin/user",
        json={"name": "赵老师", "phone": "13900005402", "password": "Teacher123"},
        headers=_auth(token),
    )
    assert first.status_code == 200, first.text
    duplicate = client.post(
        "/api/auth/admin/user",
        json={"name": "赵老师", "phone": "13900005402", "password": "Teacher123"},
        headers=_auth(token),
    )
    assert duplicate.status_code == 400
    assert "已存在" in duplicate.json()["detail"]


def test_admin_create_user_requires_admin(client):
    from services.auth_service import create_token

    session = next(client.app.dependency_overrides[get_db]())
    try:
        from models_user import User

        teacher = User(phone="13900005501", name="普通老师", role="teacher", status="active")
        session.add(teacher)
        session.commit()
        session.refresh(teacher)
        token = create_token(teacher.id)
    finally:
        session.close()

    resp = client.post(
        "/api/auth/admin/user",
        json={"name": "甲", "password": "Teacher123"},
        headers=_auth(token),
    )
    assert resp.status_code == 403

    assert client.post("/api/auth/admin/user", json={"name": "甲", "password": "Teacher123"}).status_code == 401