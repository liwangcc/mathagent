"""ALLOWED_HOSTS 的默认值：Docker 里按 IP 直访不能被 Host 校验挡掉。"""
from fastapi.testclient import TestClient

import app as app_module


def test_default_keeps_localhost(monkeypatch):
    monkeypatch.setattr(app_module, "ALLOWED_HOSTS", "")
    hosts = app_module._allowed_hosts()
    assert "localhost" in hosts and "testserver" in hosts


def test_wildcard_is_accepted(monkeypatch):
    monkeypatch.setattr(app_module, "ALLOWED_HOSTS", "*")
    assert app_module._allowed_hosts()[0] == "*"


def test_custom_domain_replaces_default(monkeypatch):
    monkeypatch.setattr(app_module, "ALLOWED_HOSTS", "math.example.com")
    hosts = app_module._allowed_hosts()
    assert hosts == ["math.example.com", "testserver"]


def test_wildcard_middleware_lets_any_host_through():
    """TrustedHostMiddleware 配了 * 之后，Host 是裸 IP 也放行（Docker 默认走这条路）。"""
    from fastapi import FastAPI
    from fastapi.middleware.trustedhost import TrustedHostMiddleware

    probe = FastAPI()
    probe.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])

    @probe.get("/ping")
    def _ping():
        return {"ok": True}

    client = TestClient(probe)
    assert client.get("/ping", headers={"host": "203.0.113.9:8080"}).status_code == 200