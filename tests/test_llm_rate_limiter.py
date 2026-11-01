"""Regression coverage for cross-worker LLM rate coordination."""

import pytest
from fastapi import HTTPException

from services import llm_rate_limiter


@pytest.fixture(autouse=True)
def reset_limiter():
    llm_rate_limiter._reset()
    yield
    llm_rate_limiter._reset()


def test_local_backend_keeps_sliding_window_semantics(monkeypatch):
    monkeypatch.setattr(
        llm_rate_limiter,
        "_config",
        lambda: ("local", 2, 60, 0.1, "redis://unused"),
    )

    llm_rate_limiter.check_llm_rate(42)
    llm_rate_limiter.check_llm_rate(42)
    with pytest.raises(HTTPException) as exc_info:
        llm_rate_limiter.check_llm_rate(42)

    assert exc_info.value.status_code == 429
    assert exc_info.value.headers["Retry-After"]


def test_redis_backend_preserves_429_from_atomic_window(monkeypatch):
    monkeypatch.setattr(
        llm_rate_limiter,
        "_config",
        lambda: ("redis", 2, 60, 0.1, "redis://unused"),
    )
    calls = []

    def fake_check(**kwargs):
        calls.append(kwargs)
        raise HTTPException(status_code=429, detail="rate limited")

    monkeypatch.setattr(llm_rate_limiter, "_check_redis_rate", fake_check)

    with pytest.raises(HTTPException) as exc_info:
        llm_rate_limiter.check_llm_rate(7)

    assert exc_info.value.status_code == 429
    assert calls[0]["user_id"] == 7


def test_redis_backend_fails_closed_when_coordinator_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        llm_rate_limiter,
        "_config",
        lambda: ("redis", 20, 60, 0.1, "redis://unavailable"),
    )
    monkeypatch.setattr(
        llm_rate_limiter,
        "_check_redis_rate",
        lambda **_kwargs: (_ for _ in ()).throw(ConnectionError("down")),
    )

    with pytest.raises(HTTPException) as exc_info:
        llm_rate_limiter.check_llm_rate(7)

    assert exc_info.value.status_code == 503
    assert "协调服务" in exc_info.value.detail


def test_redis_rate_preflight_fails_closed_when_ping_fails(monkeypatch):
    monkeypatch.setattr(
        llm_rate_limiter,
        "_config",
        lambda: ("redis", 20, 60, 0.1, "redis://unavailable"),
    )
    monkeypatch.setattr(
        llm_rate_limiter,
        "_get_redis_client",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionError("down")),
    )

    with pytest.raises(RuntimeError, match="rate-limit coordinator"):
        llm_rate_limiter.ensure_redis_llm_rate_limiter_ready()


def test_local_rate_preflight_never_contacts_redis(monkeypatch):
    monkeypatch.setattr(
        llm_rate_limiter,
        "_config",
        lambda: ("local", 20, 60, 0.1, "redis://unused"),
    )
    monkeypatch.setattr(
        llm_rate_limiter,
        "_get_redis_client",
        lambda *_args, **_kwargs: pytest.fail("local backend must not ping Redis"),
    )

    llm_rate_limiter.ensure_redis_llm_rate_limiter_ready()


def test_redis_atomic_result_enforces_a_shared_window(monkeypatch):
    monkeypatch.setattr(
        llm_rate_limiter,
        "_config",
        lambda: ("redis", 2, 60, 0.1, "redis://fake"),
    )
    observed = []

    def fake_check(**kwargs):
        observed.append(kwargs["user_id"])
        if observed.count(kwargs["user_id"]) > 2:
            raise llm_rate_limiter._rate_limited(60)

    monkeypatch.setattr(llm_rate_limiter, "_check_redis_rate", fake_check)

    llm_rate_limiter.check_llm_rate(99)
    llm_rate_limiter.check_llm_rate(99)
    with pytest.raises(HTTPException) as exc_info:
        llm_rate_limiter.check_llm_rate(99)

    assert exc_info.value.status_code == 429
    assert observed == [99, 99, 99]