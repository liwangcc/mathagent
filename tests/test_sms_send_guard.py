"""短信发送接口的滥用防护：图形验证票据 + IP/全局配额。

背景：/auth/sms/send 原本只按手机号冷却，换号即可无限发送，账单直接落到阿里云账号。
这里的测试锁住三条边界：
1) 同一来源 IP 每小时/每天有上限；
2) 全局每天有上限（爆量攻击时兜底）；
3) 图形验证码已配置时，必须先拿到一次性票据，且票据与手机号绑定。
"""

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
        f"sqlite:///{tmp_path / 'sms_guard.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    app.dependency_overrides[get_db] = _override_db
    test_client = TestClient(app)
    try:
        yield test_client
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    from services import auth_service, auth_tickets, sms_throttle

    auth_service._clear_sms_store()
    auth_tickets.reset_for_tests()
    sms_throttle.reset_for_tests()
    monkeypatch.setattr(auth_service, "DEV_MODE", True)
    # 默认按「图形验证关闭」跑（票据路径由单独用例覆盖），需要时用 _enable_captcha 打开
    monkeypatch.setattr(config, "CAPTCHA_PROVIDER", "off", raising=False)
    monkeypatch.setattr(config, "CAPTCHA_APP_ID", "", raising=False)
    monkeypatch.setattr(config, "CAPTCHA_APP_KEY", "", raising=False)
    yield
    auth_service._clear_sms_store()
    auth_tickets.reset_for_tests()
    sms_throttle.reset_for_tests()


def _enable_captcha(monkeypatch):
    monkeypatch.setattr(config, "CAPTCHA_PROVIDER", "aliyun", raising=False)
    monkeypatch.setattr(config, "CAPTCHA_APP_ID", "test-captcha-id", raising=False)
    monkeypatch.setattr(config, "CAPTCHA_APP_KEY", "test-captcha-key", raising=False)


# ---------- 配额 ----------

def test_ip_hourly_limit_blocks_after_quota(client, monkeypatch):
    from services import sms_throttle

    monkeypatch.setattr(sms_throttle, "IP_HOURLY_LIMIT", 2)
    assert client.post("/api/auth/sms/send", json={"phone": "13900002001"}).status_code == 200
    assert client.post("/api/auth/sms/send", json={"phone": "13900002002"}).status_code == 200
    resp = client.post("/api/auth/sms/send", json={"phone": "13900002003"})
    assert resp.status_code == 429
    assert "频繁" in resp.json()["detail"]


def test_global_daily_limit_blocks_regardless_of_source(client, monkeypatch):
    from services import sms_throttle

    monkeypatch.setattr(sms_throttle, "IP_HOURLY_LIMIT", 100)
    monkeypatch.setattr(sms_throttle, "IP_DAILY_LIMIT", 100)
    monkeypatch.setattr(sms_throttle, "GLOBAL_DAILY_LIMIT", 1)
    assert client.post("/api/auth/sms/send", json={"phone": "13900002101"}).status_code == 200
    # 全局额度是最后的账单防线：换来源也拦得住
    resp = client.post("/api/auth/sms/send", json={"phone": "13900002102"})
    assert resp.status_code == 429


def test_client_ip_prefers_real_ip_only_from_loopback():
    """只有来自回环（nginx）时才信任 X-Real-IP，防止直连伪造头绕过限流。"""
    from starlette.requests import Request
    from routers.auth import _client_ip

    behind_proxy = Request(
        {
            "type": "http",
            "headers": [(b"x-real-ip", b"203.0.113.7")],
            "client": ("127.0.0.1", 40000),
        }
    )
    assert _client_ip(behind_proxy) == "203.0.113.7"

    direct = Request(
        {
            "type": "http",
            "headers": [(b"x-real-ip", b"198.51.100.9")],
            "client": ("198.51.100.20", 40000),
        }
    )
    assert _client_ip(direct) == "198.51.100.20"


# ---------- 图形验证票据 ----------

def test_send_requires_captcha_when_configured(client, monkeypatch):
    _enable_captcha(monkeypatch)
    resp = client.post("/api/auth/sms/send", json={"phone": "13900002201"})
    assert resp.status_code == 400
    assert "图形验证" in resp.json()["detail"]


def test_valid_ticket_allows_send_and_is_single_use(client, monkeypatch):
    from services import auth_service

    _enable_captcha(monkeypatch)
    phone = "13900002301"
    ticket = auth_service._issue_captcha_ticket(phone)

    first = client.post(
        "/api/auth/sms/send", json={"phone": phone, "captcha_ticket": ticket}
    )
    assert first.status_code == 200

    # 票据一次性：同一张票不能再发一次
    second = client.post(
        "/api/auth/sms/send", json={"phone": phone, "captcha_ticket": ticket}
    )
    assert second.status_code == 400


def test_ticket_is_bound_to_phone(client, monkeypatch):
    from services import auth_service

    _enable_captcha(monkeypatch)
    ticket = auth_service._issue_captcha_ticket("13900002401")

    resp = client.post(
        "/api/auth/sms/send",
        json={"phone": "13900002402", "captcha_ticket": ticket},
    )
    assert resp.status_code == 400
    assert "图形验证" in resp.json()["detail"]


def test_expired_ticket_is_rejected(monkeypatch):
    from services import auth_service, auth_tickets

    ticket = auth_service._issue_captcha_ticket("13900002501")
    # 让票据立即过期（票据已落库，不再是进程内字典）
    auth_tickets.expire_for_tests(auth_tickets.KIND_CAPTCHA, ticket)

    assert auth_service.consume_captcha_ticket(ticket, "13900002501") is False


def test_unknown_ticket_is_rejected():
    from services import auth_service

    assert auth_service.consume_captcha_ticket("", "13900002601") is False
    assert auth_service.consume_captcha_ticket("not-a-real-ticket", "13900002601") is False


# ---------- DEV_MODE 启动守卫 ----------

def test_dev_mode_guard_blocks_public_cors(monkeypatch):
    """DEV_MODE 会回显验证码；CORS 指向公网时必须拒绝启动。"""
    import app as app_module

    monkeypatch.setenv("DEV_MODE", "1")
    monkeypatch.delenv("ALLOW_DEV_MODE_IN_PRODUCTION", raising=False)
    monkeypatch.setattr(app_module, "CORS_ORIGINS", "https://mathagent.example.com")

    with pytest.raises(RuntimeError) as exc:
        app_module._guard_dev_mode()
    assert "DEV_MODE" in str(exc.value)


def test_dev_mode_guard_allows_localhost(monkeypatch):
    import app as app_module

    monkeypatch.setenv("DEV_MODE", "1")
    monkeypatch.delenv("ALLOW_DEV_MODE_IN_PRODUCTION", raising=False)
    monkeypatch.setattr(app_module, "CORS_ORIGINS", "http://localhost:5173")

    app_module._guard_dev_mode()  # 不抛错即通过