"""设置页「连接测试」的安全边界。

历史问题：`/api/settings/test` 在 api_key 留空时使用服务器保存的模型 Key，同时
仍然接受调用方传入的 base_url，并把上游响应正文回显出来——任何拿到管理员 token
的人都能把生产 Key 骗到自己的服务器上，顺带做内网探测。

本文件锁住三条：
1) 用服务器 Key 时，目标地址必须来自配置，调用方的 base_url 被忽略；
2) 调用方自带 Key 时，才允许指定地址；
3) 上游正文不回显，且内网地址一律拒绝。
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.auth import _require_admin
from routers.settings import router as settings_router
from routers import settings as settings_module

RECORDED: list[tuple] = []


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeClient:
    def __init__(self, *_args, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, url, headers=None, **_kwargs):
        RECORDED.append(("GET", url, dict(headers or {})))
        return _FakeResponse(404, text="UPSTREAM-BODY-SECRET")

    async def post(self, url, headers=None, json=None, **_kwargs):
        RECORDED.append(("POST", url, dict(headers or {})))
        return _FakeResponse(503, text="UPSTREAM-BODY-SECRET")


@pytest.fixture
def client(monkeypatch):
    RECORDED.clear()
    monkeypatch.setattr(settings_module.httpx, "AsyncClient", _FakeClient)
    # 网络解析不作为本文件的测试对象，单独在 test_ensure_public_url_* 覆盖
    monkeypatch.setattr(settings_module, "_ensure_public_url", lambda url: None)
    # 用服务器 Key 时必须走配置地址：这里显式固定一个可识别的配置值
    monkeypatch.setenv("LLM_BASE_URL", "https://configured.example.net/v1")
    monkeypatch.setenv("LLM_API_KEY", "stored-server-key")

    app = FastAPI()
    app.include_router(settings_router, prefix="/api")
    app.dependency_overrides[_require_admin] = lambda: SimpleNamespace(id=1, role="admin")
    return TestClient(app)


def test_stored_key_rejects_switching_base_url(client):
    """用服务器 Key 时不允许改地址，否则管理员 token 就能把 Key 发走。"""
    resp = client.post(
        "/api/settings/test",
        json={"api_key": "", "base_url": "https://attacker.example.com/collect"},
    )
    assert resp.status_code == 400
    assert "不能切换到其它地址" in resp.json()["detail"]
    assert not RECORDED, "被拒绝的请求不应发出任何外呼"


def test_stored_key_is_used_when_api_key_blank(client):
    resp = client.post(
        "/api/settings/test",
        json={"api_key": "", "base_url": "https://configured.example.net/v1"},
    )
    assert resp.status_code == 200
    auths = [h.get("Authorization") for _, _, h in RECORDED]
    assert auths and all(a == "Bearer stored-server-key" for a in auths)


def test_caller_supplied_key_may_choose_base_url(client):
    resp = client.post(
        "/api/settings/test",
        json={"api_key": "my-own-key", "base_url": "https://mine.example.org/v1"},
    )
    assert resp.status_code == 200
    urls = [u for _, u, _ in RECORDED]
    assert any("mine.example.org" in u for u in urls), urls
    auths = [h.get("Authorization") for _, _, h in RECORDED]
    assert all("stored-server-key" not in (a or "") for a in auths)


def test_upstream_body_is_not_echoed(client):
    resp = client.post("/api/settings/test", json={"api_key": "my-own-key", "base_url": "https://mine.example.org/v1"})
    body = resp.text
    assert "UPSTREAM-BODY-SECRET" not in body
    assert resp.json()["success"] is False


# ---------- SSRF ----------

@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/v1",
        "http://localhost:8080/v1",
        "http://10.0.0.5/v1",
        "http://192.168.1.10/v1",
        "http://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
    ],
)
def test_ensure_public_url_rejects_internal_targets(url):
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        settings_module._ensure_public_url(url)


def test_ensure_public_url_allows_public_host(monkeypatch):
    """公网域名放行（DNS 结果打桩，避免测试依赖网络）。"""
    import socket as _socket

    monkeypatch.setattr(
        settings_module.socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    settings_module._ensure_public_url("https://api.example.com/v1")  # 不抛错即通过