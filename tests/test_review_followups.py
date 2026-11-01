"""复审后修复的回归：验证码票据优先于配额、换地址必须配新密钥、同源 CORS、微信 state 绑 Cookie。"""

import pytest
from fastapi import HTTPException
from starlette.requests import Request


def _req(ip="203.0.113.9"):
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/sms/send",
            "headers": [],
            "client": (ip, 12345),
            "query_string": b"",
        }
    )


# ---------- 短信：没有图形票据的请求不得消耗配额 ----------

def test_sms_without_ticket_does_not_consume_quota(monkeypatch):
    import routers.auth as auth_mod
    import services.auth_service as auth_service
    import services.sms_throttle as throttle

    calls = {"throttle": 0}

    def fake_check(ip):
        calls["throttle"] += 1
        return {"ok": True}

    monkeypatch.setattr(auth_mod, "_require_sms_enabled", lambda: None)
    monkeypatch.setattr(auth_mod, "_captcha_provider", lambda: "builtin")
    monkeypatch.setattr(throttle, "check_and_record", fake_check)
    monkeypatch.setattr(auth_service, "consume_captcha_ticket", lambda t, p: False)

    body = auth_mod.SmsSendRequest(phone="13800000000", captcha_ticket="")
    with pytest.raises(HTTPException) as exc:
        auth_mod.api_send_sms(body, _req())

    assert exc.value.status_code == 400
    assert calls["throttle"] == 0, "没带票据的请求不应写入配额，否则可被用来刷光全局日额度"


def test_sms_with_ticket_then_quota(monkeypatch):
    import routers.auth as auth_mod
    import services.auth_service as auth_service
    import services.sms_throttle as throttle

    order = []

    monkeypatch.setattr(auth_mod, "_require_sms_enabled", lambda: None)
    monkeypatch.setattr(auth_mod, "_captcha_provider", lambda: "builtin")
    monkeypatch.setattr(
        auth_service, "consume_captcha_ticket", lambda t, p: order.append("ticket") or True
    )
    monkeypatch.setattr(
        throttle, "check_and_record", lambda ip: order.append("quota") or {"ok": False, "message": "超限"}
    )

    body = auth_mod.SmsSendRequest(phone="13800000000", captcha_ticket="t")
    with pytest.raises(HTTPException) as exc:
        auth_mod.api_send_sms(body, _req())

    assert exc.value.status_code == 429
    assert order == ["ticket", "quota"]


# ---------- 设置：只改地址不带密钥 = 把已存密钥送去新地址 ----------

def test_save_settings_rejects_url_change_without_key(monkeypatch):
    import routers.settings as settings_mod

    monkeypatch.setenv("LLM_API_KEY", "stored-server-key")
    data = settings_mod.SettingsSaveRequest(
        api_key=None, base_url="https://evil.example.com/v1", model=None
    )
    with pytest.raises(HTTPException) as exc:
        settings_mod.save_settings(data, user=object())

    assert exc.value.status_code == 400
    assert "密钥" in str(exc.value.detail)


def test_save_settings_allows_url_change_with_key(monkeypatch):
    import routers.settings as settings_mod

    monkeypatch.setenv("LLM_API_KEY", "stored-server-key")
    monkeypatch.setattr(settings_mod, "_guard_base_url", lambda url: None)
    monkeypatch.setattr(settings_mod, "_update_config_file", lambda k, v: None)

    data = settings_mod.SettingsSaveRequest(
        api_key="new-key", base_url="https://api.example.com/v1", model=None
    )
    out = settings_mod.save_settings(data, user=object())
    assert out["ok"] is True
    assert "LLM_BASE_URL" in out["changed"]


# ---------- 同源部署：CORS 白名单为空是合法状态 ----------

def test_empty_cors_is_allowed_for_same_origin(monkeypatch):
    import app as app_mod

    monkeypatch.setattr(app_mod, "CORS_ALLOW_ALL", False)
    monkeypatch.setattr(app_mod, "CORS_ORIGINS", "")
    app_mod._validate_cors_config()  # 不抛异常即通过


# ---------- 微信：state 必须与发起端 Cookie 一致 ----------

def test_wechat_url_sets_state_cookie():
    from routers.auth import router as auth_router
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app_ = FastAPI()
    app_.include_router(auth_router, prefix="/api")
    client = TestClient(app_)

    resp = client.get("/api/auth/wechat/url")
    if resp.status_code == 501:  # 未配置微信登录的环境跳过
        pytest.skip("微信登录未配置")
    assert resp.status_code == 200
    state = resp.json()["state"]
    assert resp.cookies.get("wechat_oauth_state") == state


def test_wechat_callback_rejects_state_without_cookie():
    from routers.auth import router as auth_router
    from routers import auth as auth_mod
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app_ = FastAPI()
    app_.include_router(auth_router, prefix="/api")
    client = TestClient(app_)

    state = auth_mod._issue_wechat_state()
    # 只带 state 参数、不带 Cookie：这正是登录 CSRF 的形态，必须拒绝
    resp = client.get("/api/auth/wechat/callback", params={"code": "x", "state": state})
    assert resp.status_code == 400