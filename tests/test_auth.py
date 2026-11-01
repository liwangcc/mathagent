"""Tests for 用户认证模块 — 短信、JWT、注册/登录、微信。"""

import os
import time
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import database
from database import Base


@pytest.fixture
def db_session(tmp_path, monkeypatch):
    """每个测试用独立的 SQLite 文件，避免污染主库。"""
    db_url = f"sqlite:///{tmp_path / 'test_auth.db'}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})

    # 关键：必须 import 后 Base 才能感知到 User
    import models  # noqa: F401
    import models_user  # noqa: F401
    import models_rate_limit  # noqa: F401  (teacher_classes 表，User.current_class_id 外键依赖)

    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _clear_sms(monkeypatch):
    """每个测试前清空验证码/票据，开启DEV_MODE。

    图形验证默认走 builtin（算术题），本文件的用例都聚焦短信/JWT 语义，
    因此这里显式关掉图形验证；内置算术题的行为在
    tests/test_auth_admin_and_captcha.py 里单独覆盖。
    """
    import config
    from services import auth_service, auth_tickets
    from services.ip_rate_limit import clear_ip_failures
    from services import sms_throttle
    from routers import auth as auth_router_mod
    auth_service._clear_sms_store()
    auth_service._clear_password_login_failures()
    auth_tickets.reset_for_tests()
    sms_throttle.reset_for_tests()
    clear_ip_failures("testclient")
    auth_router_mod._clear_revoked_tokens()
    auth_service.DEV_MODE = True  # 测试环境下开启
    monkeypatch.setattr(config, "CAPTCHA_PROVIDER", "off", raising=False)
    monkeypatch.setattr(config, "SMS_ENABLED", True, raising=False)
    yield
    auth_service._clear_sms_store()
    auth_service._clear_password_login_failures()
    auth_tickets.reset_for_tests()
    clear_ip_failures("testclient")
    auth_router_mod._clear_revoked_tokens()


# ---------- 短信验证码 ----------

class TestSmsCode:
    def test_send_returns_dev_code_when_unconfigured(self):
        from services.auth_service import send_sms_code
        result = send_sms_code("13800138000")
        assert result["ok"] is True
        assert "dev_code" in result
        assert len(result["dev_code"]) == 6
        assert result["dev_code"].isdigit()

    def test_send_rejects_empty_phone(self):
        from services.auth_service import send_sms_code
        result = send_sms_code("")
        assert result["ok"] is False

    def test_send_rate_limit(self):
        from services.auth_service import send_sms_code
        r1 = send_sms_code("13800138001")
        assert r1["ok"] is True
        r2 = send_sms_code("13800138001")
        assert r2["ok"] is False
        assert "秒" in r2["message"]

    def test_production_sms_does_not_return_code(self, monkeypatch):
        """关闭 DEV_MODE 后，即便短信发送成功也绝不回显验证码。"""
        from services import auth_service
        monkeypatch.setattr(auth_service, "DEV_MODE", False)
        monkeypatch.setattr(auth_service, "SMS_ACCESS_KEY_ID", "test-key")
        monkeypatch.setattr(auth_service, "SMS_ACCESS_KEY_SECRET", "test-secret")
        monkeypatch.setattr(auth_service, "SMS_SIGN_NAME", "test-sign")
        monkeypatch.setattr(auth_service, "SMS_TEMPLATE_CODE", "test-template")
        monkeypatch.setattr(auth_service, "_send_via_aliyun", lambda phone, code: None)

        result = auth_service.send_sms_code("13800138999")

        assert result["ok"] is True
        assert "dev_code" not in result

    def test_verify_correct_code(self):
        from services.auth_service import send_sms_code, verify_sms_code
        r = send_sms_code("13800138002")
        assert verify_sms_code("13800138002", r["dev_code"]) is True

    def test_verify_wrong_code(self):
        from services.auth_service import send_sms_code, verify_sms_code
        send_sms_code("13800138003")
        assert verify_sms_code("13800138003", "000000") is False

    def test_verify_consumes_code(self):
        from services.auth_service import send_sms_code, verify_sms_code
        r = send_sms_code("13800138004")
        code = r["dev_code"]
        assert verify_sms_code("13800138004", code) is True
        # 二次验证应该失败 — 验证码一次性消费
        assert verify_sms_code("13800138004", code) is False

    def test_verify_no_code_sent(self):
        from services.auth_service import verify_sms_code
        assert verify_sms_code("13800138005", "123456") is False

    def test_verify_expired_code(self):
        from services.auth_service import send_sms_code, verify_sms_code
        from services import auth_service
        r = send_sms_code("13800138006")
        # 手动让验证码过期（SQLite 存储下需用 update 写回，就地改副本不生效）
        auth_service._sms_code_store.update("13800138006", expires_at=time.time() - 1)
        assert verify_sms_code("13800138006", r["dev_code"]) is False


# ---------- JWT ----------

class TestJwt:
    def test_create_and_verify(self):
        from services.auth_service import create_token, verify_token
        token = create_token(42)
        assert isinstance(token, str)
        assert len(token) > 0
        assert verify_token(token) == 42

    def test_verify_invalid_token(self):
        from services.auth_service import verify_token
        assert verify_token("not-a-token") is None
        assert verify_token("") is None

    def test_verify_none(self):
        from services.auth_service import verify_token
        assert verify_token(None) is None

    def test_expired_token(self):
        from services.auth_service import create_token, verify_token
        token = create_token(1, expires_hours=-1)
        assert verify_token(token) is None


# ---------- 用户增删 ----------

class TestUserCreation:
    def test_create_user_basic(self, db_session):
        from services.auth_service import create_user
        u = create_user(db_session, phone="13900000001", name="李老师", school="第一中学")
        assert u.id is not None
        assert u.phone == "13900000001"
        assert u.name == "李老师"
        assert u.school == "第一中学"
        assert u.role == "teacher"
        assert u.status == "active"

    def test_get_user_by_phone(self, db_session):
        from services.auth_service import create_user, get_user_by_phone
        create_user(db_session, phone="13900000002", name="A")
        u = get_user_by_phone(db_session, "13900000002")
        assert u is not None
        assert u.name == "A"

    def test_create_user_to_dict(self, db_session):
        from services.auth_service import create_user
        u = create_user(db_session, phone="13900000003", name="B", invite_code="VIP")
        d = u.to_dict()
        assert d["phone"] == "13900000003"
        assert d["name"] == "B"
        assert d["invite_code"] == "VIP"
        assert d["role"] == "teacher"


# ---------- 微信登录 (mocked) ----------

class TestWechatFlow:
    def test_get_or_create_creates_new(self, db_session):
        from services.auth_service import get_or_create_wechat_user
        u = get_or_create_wechat_user(db_session, openid="ox_abc123", nickname="昵称")
        assert u.id is not None
        assert u.wechat_openid == "ox_abc123"
        assert u.name == "昵称"

    def test_get_or_create_reuses_existing(self, db_session):
        from services.auth_service import get_or_create_wechat_user
        u1 = get_or_create_wechat_user(db_session, openid="ox_xyz", nickname="X")
        u2 = get_or_create_wechat_user(db_session, openid="ox_xyz", nickname="忽略")
        assert u1.id == u2.id

    def test_build_wechat_url_unconfigured(self):
        from services import auth_service
        with patch.object(auth_service, "WECHAT_APP_ID", ""):
            assert auth_service.build_wechat_authorize_url() == ""

    def test_build_wechat_url_configured(self):
        from services import auth_service
        with patch.object(auth_service, "WECHAT_APP_ID", "wx123"), \
             patch.object(auth_service, "WECHAT_REDIRECT_URI", "https://example.com/cb"):
            url = auth_service.build_wechat_authorize_url(state="abc")
            assert "wx123" in url
            assert "state=abc" in url
            assert "open.weixin.qq.com" in url


# ---------- get_current_user ----------

class TestGetCurrentUser:
    def test_resolves_valid_token(self, db_session):
        from services.auth_service import create_user, create_token, get_current_user
        u = create_user(db_session, phone="13900000010", name="Z")
        token = create_token(u.id)
        resolved = get_current_user(db_session, token)
        assert resolved is not None
        assert resolved.id == u.id

    def test_rejects_disabled_user(self, db_session):
        from services.auth_service import create_user, create_token, get_current_user
        u = create_user(db_session, phone="13900000011", name="D")
        u.status = "disabled"
        db_session.commit()
        token = create_token(u.id)
        assert get_current_user(db_session, token) is None

    def test_no_token_returns_none(self, db_session):
        from services.auth_service import get_current_user
        assert get_current_user(db_session, None) is None
        assert get_current_user(db_session, "") is None


# ---------- 完整登录流程 (端到端) ----------

class TestLoginFlow:
    def test_phone_register_then_login(self, db_session):
        from services.auth_service import (
            send_sms_code,
            verify_sms_code,
            get_user_by_phone,
            create_user,
            create_token,
            verify_token,
        )
        phone = "13900000020"
        sent = send_sms_code(phone)
        assert verify_sms_code(phone, sent["dev_code"]) is True

        # 首次登录 — 注册
        assert get_user_by_phone(db_session, phone) is None
        user = create_user(db_session, phone=phone, name="新用户", school="实验中学")
        token = create_token(user.id)
        assert verify_token(token) == user.id

        # 验证码已消费 — 二次必须重新发送
        from services import auth_service
        from services.auth_service import _clear_sms_store
        _clear_sms_store()
        sent2 = send_sms_code(phone)
        assert verify_sms_code(phone, sent2["dev_code"]) is True
        existing = get_user_by_phone(db_session, phone)
        assert existing is not None
        assert existing.id == user.id


# ---------- 微信 exchange_wechat_code (mocked httpx) ----------

class TestWechatExchange:
    def test_exchange_unconfigured_raises(self):
        import asyncio
        from services import auth_service
        with patch.object(auth_service, "WECHAT_APP_ID", ""), \
             patch.object(auth_service, "WECHAT_APP_SECRET", ""):
            with pytest.raises(RuntimeError):
                asyncio.run(auth_service.exchange_wechat_code("any-code"))


# ---------- HTTP 路由 (FastAPI TestClient) ----------

@pytest.fixture
def http_client(tmp_path):
    """提供 FastAPI TestClient，独立 SQLite，覆盖 get_db 依赖。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import models  # noqa: F401
    import models_user  # noqa: F401
    import models_rate_limit  # noqa: F401  (teacher_classes 表，登录时 ensure_current_class 会查)
    from database import Base, get_db
    from routers.auth import router as auth_router

    db_url = f"sqlite:///{tmp_path / 'test_http.db'}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        s = TestSession()
        try:
            yield s
        finally:
            s.close()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    app.dependency_overrides[get_db] = _override_db

    client = TestClient(app)
    try:
        yield client
    finally:
        engine.dispose()


def test_send_sms_code(http_client):
    resp = http_client.post("/api/auth/sms/send", json={"phone": "13900001001"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "dev_code" in body  # 开发模式下返回


def test_login_with_code(http_client):
    phone = "13900001002"
    sent = http_client.post("/api/auth/sms/send", json={"phone": phone}).json()
    code = sent["dev_code"]

    resp = http_client.post(
        "/api/auth/sms/login",
        json={"phone": phone, "code": code, "name": "张老师", "school": "测试中学"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert isinstance(body.get("token"), str) and len(body["token"]) > 0
    assert body["user"]["phone"] == phone
    assert body["user"]["name"] == "张老师"


def test_new_user_two_step_register_keeps_code_valid(http_client):
    """复现并锁定登录 bug：新用户首次不带姓名提交，后端不应消费验证码。

    旧实现先核销验证码、再因缺姓名报错，导致补填姓名再次提交时验证码已失效，
    必然得到「验证码错误或已过期」。修复后：第一次返回「需要填写姓名」且不消费，
    第二次带姓名仍能用同一验证码完成注册。
    """
    phone = "13900001099"
    sent = http_client.post("/api/auth/sms/send", json={"phone": phone}).json()
    code = sent["dev_code"]

    # 第一次：不带姓名 → 要求填姓名，验证码不被消费
    first = http_client.post("/api/auth/sms/login", json={"phone": phone, "code": code})
    assert first.status_code == 400
    assert "姓名" in first.json()["detail"]

    # 第二次：补填姓名，沿用同一验证码 → 注册并登录成功
    second = http_client.post(
        "/api/auth/sms/login",
        json={"phone": phone, "code": code, "name": "新老师"},
    )
    assert second.status_code == 200
    body = second.json()
    assert body["ok"] is True
    assert body["user"]["name"] == "新老师"


def test_get_me(http_client):
    phone = "13900001003"
    sent = http_client.post("/api/auth/sms/send", json={"phone": phone}).json()
    login = http_client.post(
        "/api/auth/sms/login",
        json={"phone": phone, "code": sent["dev_code"], "name": "李老师"},
    ).json()
    token = login["token"]

    resp = http_client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["phone"] == phone
    assert body["user"]["name"] == "李老师"


def test_invalid_code(http_client):
    phone = "13900001004"
    http_client.post("/api/auth/sms/send", json={"phone": phone})
    resp = http_client.post(
        "/api/auth/sms/login",
        json={"phone": phone, "code": "000000", "name": "X"},
    )
    assert resp.status_code == 400


def test_password_login_locks_after_failures(http_client):
    phone = "13900001005"
    sent = http_client.post("/api/auth/sms/send", json={"phone": phone}).json()
    login = http_client.post(
        "/api/auth/sms/login",
        json={"phone": phone, "code": sent["dev_code"], "name": "王老师"},
    ).json()
    token = login["token"]
    set_resp = http_client.post(
        "/api/auth/password/set",
        json={"password": "secret1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert set_resp.status_code == 200

    for _ in range(5):
        resp = http_client.post(
            "/api/auth/password/login",
            json={"phone": phone, "password": "wrongpw"},
        )
        assert resp.status_code == 400

    locked = http_client.post(
        "/api/auth/password/login",
        json={"phone": phone, "password": "secret1"},
    )
    assert locked.status_code == 429


def test_change_password_requires_old_password(http_client):
    """已设密码后，修改密码必须提供正确的旧密码（FE-C1）。"""
    phone = "13900001006"
    sent = http_client.post("/api/auth/sms/send", json={"phone": phone}).json()
    login = http_client.post(
        "/api/auth/sms/login",
        json={"phone": phone, "code": sent["dev_code"], "name": "赵老师"},
    ).json()
    token = login["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 首次设置密码：无需旧密码
    assert http_client.post(
        "/api/auth/password/set",
        json={"password": "secret1"},
        headers=headers,
    ).status_code == 200

    # 修改密码缺旧密码 → 400
    assert http_client.post(
        "/api/auth/password/set",
        json={"password": "secret2"},
        headers=headers,
    ).status_code == 400

    # 修改密码旧密码错误 → 400
    assert http_client.post(
        "/api/auth/password/set",
        json={"password": "secret2", "old_password": "wrongpw"},
        headers=headers,
    ).status_code == 400

    # 修改密码旧密码正确 → 200，且新密码可登录
    assert http_client.post(
        "/api/auth/password/set",
        json={"password": "secret2", "old_password": "secret1"},
        headers=headers,
    ).status_code == 200
    assert http_client.post(
        "/api/auth/password/login",
        json={"phone": phone, "password": "secret2"},
    ).status_code == 200


def test_logout_revokes_token(http_client):
    """登出后，同一 token 立即失效（新-M：token 吊销）。"""
    phone = "13900001007"
    sent = http_client.post("/api/auth/sms/send", json={"phone": phone}).json()
    login = http_client.post(
        "/api/auth/sms/login",
        json={"phone": phone, "code": sent["dev_code"], "name": "钱老师"},
    ).json()
    token = login["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 登出前 token 有效
    assert http_client.get("/api/auth/me", headers=headers).status_code == 200

    # 登出
    assert http_client.post("/api/auth/logout", headers=headers).status_code == 200

    # 登出后同一 token 立即失效 → 401
    assert http_client.get("/api/auth/me", headers=headers).status_code == 401


def test_logout_requires_auth(http_client):
    """无 token 登出 → 401（依赖 _require_user）。"""
    assert http_client.post("/api/auth/logout").status_code == 401


# ---------- 当前班级不变式：总有选中班级 ----------

def _login(http_client, phone, name="老师"):
    sent = http_client.post("/api/auth/sms/send", json={"phone": phone}).json()
    login = http_client.post(
        "/api/auth/sms/login",
        json={"phone": phone, "code": sent["dev_code"], "name": name},
    ).json()
    return login["token"]


def test_login_auto_selects_first_class_when_none(http_client):
    """有班级但未选中时，登录/取班级列表应默认选中第一个（服务端保证不变式）。"""
    token = _login(http_client, "13900002001", "默认班老师")
    headers = {"Authorization": f"Bearer {token}"}

    # 新老师还没有班级 → current_class_id 为空
    me = http_client.get("/api/auth/me", headers=headers).json()
    assert me["user"]["current_class_id"] is None

    # 建两个班（创建不自动选中）
    c1 = http_client.post("/api/auth/class/create", json={"class_name": "高一(1)"}, headers=headers).json()
    http_client.post("/api/auth/class/create", json={"class_name": "高一(2)"}, headers=headers)

    # 取班级列表触发默认选中第一个
    classes = http_client.get("/api/auth/classes", headers=headers).json()
    assert classes["current_class_id"] == c1["class"]["id"]


def test_switch_to_null_then_me_reselects(http_client):
    """即便显式切到"无班级"（旧全部班级模式），下次 /auth/me 也会重新默认选中，
    保证批改入口拿到的一定是某个具体班级。"""
    token = _login(http_client, "13900002002", "回填老师")
    headers = {"Authorization": f"Bearer {token}"}
    c1 = http_client.post("/api/auth/class/create", json={"class_name": "班A"}, headers=headers).json()
    cid = c1["class"]["id"]

    # 切到具体班
    http_client.post("/api/auth/class/switch", json={"class_id": cid}, headers=headers)
    # 显式切空
    r = http_client.post("/api/auth/class/switch", json={"class_id": None}, headers=headers).json()
    assert r["current_class_id"] is None
    # /auth/me 重新默认选回
    me = http_client.get("/api/auth/me", headers=headers).json()
    assert me["user"]["current_class_id"] == cid


def test_delete_current_class_falls_back_to_another(http_client):
    """删除当前选中班级时，应回退到另一个剩余班级，而不是置空。"""
    token = _login(http_client, "13900002003", "删班老师")
    headers = {"Authorization": f"Bearer {token}"}
    a = http_client.post("/api/auth/class/create", json={"class_name": "甲班"}, headers=headers).json()["class"]
    b = http_client.post("/api/auth/class/create", json={"class_name": "乙班"}, headers=headers).json()["class"]

    # 选中甲班
    http_client.post("/api/auth/class/switch", json={"class_id": a["id"]}, headers=headers)
    # 删甲班 → 应回退到乙班
    dele = http_client.delete(f"/api/auth/class/{a['id']}", headers=headers)
    assert dele.status_code == 200
    me = http_client.get("/api/auth/me", headers=headers).json()
    assert me["user"]["current_class_id"] == b["id"]
