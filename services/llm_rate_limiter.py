"""Per-user LLM sliding-window limits shared across Gunicorn workers.

Production uses Redis so worker count cannot multiply a teacher's API quota.
``local`` is an explicit development fallback only.  Redis failures are fail
closed and every Redis command is time-bounded, preventing a coordinator outage
from turning into a permanently hung web request.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import defaultdict, deque
from typing import Any

from fastapi import HTTPException

_lock = threading.Lock()
_hits: dict[int, deque[float]] = defaultdict(deque)
_redis_client: Any | None = None
_redis_lock = threading.Lock()

_CHECK_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local cutoff = tonumber(ARGV[2])
local max_calls = tonumber(ARGV[3])
local member = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
local count = redis.call('ZCARD', key)
if count >= max_calls then
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  return {0, oldest[2] or now}
end
redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, math.max(1, math.ceil(now - cutoff) + 1))
return {1, 0}
"""


def _config() -> tuple[str, int, int, float, str]:
    # Import lazily: config loads dotenv and is intentionally process-specific.
    from config import (
        LLM_RATE_LIMITER_BACKEND,
        LLM_RATE_LIMIT_MAX_CALLS,
        LLM_RATE_LIMIT_WINDOW_SECONDS,
        MODEL_LIMITER_REDIS_OPERATION_TIMEOUT_SECONDS,
        REDIS_URL,
    )

    return (
        LLM_RATE_LIMITER_BACKEND,
        LLM_RATE_LIMIT_MAX_CALLS,
        LLM_RATE_LIMIT_WINDOW_SECONDS,
        MODEL_LIMITER_REDIS_OPERATION_TIMEOUT_SECONDS,
        REDIS_URL,
    )


def _get_redis_client(redis_url: str, *, operation_timeout: float):
    global _redis_client
    with _redis_lock:
        if _redis_client is None:
            try:
                import redis
            except ImportError as exc:
                raise RuntimeError("LLM_RATE_LIMITER_BACKEND=redis requires the redis package") from exc
            _redis_client = redis.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=operation_timeout,
                socket_timeout=operation_timeout,
                health_check_interval=30,
            )
        return _redis_client


def _check_redis_rate(
    *,
    user_id: int,
    max_calls: int,
    window: int,
    now: float,
    operation_timeout: float,
    redis_url: str,
) -> None:
    """One atomic Redis window operation, kept separate for test injection."""
    key = f"mathagent:llm-rate:v1:user:{int(user_id)}"
    result = _get_redis_client(
        redis_url,
        operation_timeout=operation_timeout,
    ).eval(
        _CHECK_LUA,
        1,
        key,
        now,
        now - window,
        max_calls,
        f"{now:.9f}:{uuid.uuid4().hex}",
    )
    allowed, oldest = int(result[0]), float(result[1])
    if not allowed:
        retry_after = max(1, int(oldest + window - now) + 1)
        raise _rate_limited(retry_after)


def ensure_redis_llm_rate_limiter_ready() -> None:
    """Fail startup closed when the shared user-rate coordinator is unavailable."""
    backend, _max_calls, _window, operation_timeout, redis_url = _config()
    if backend != "redis":
        return
    try:
        response = _get_redis_client(
            redis_url,
            operation_timeout=operation_timeout,
        ).ping()
    except Exception as exc:
        raise RuntimeError("Redis LLM rate-limit coordinator is unavailable") from exc
    if not response:
        raise RuntimeError("Redis LLM rate-limit coordinator did not acknowledge PING")


def _local_check(user_id: int, *, max_calls: int, window: int, now: float) -> None:
    with _lock:
        dq = _hits[user_id]
        cutoff = now - window
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= max_calls:
            retry_after = max(1, int(dq[0] + window - now) + 1)
            raise _rate_limited(retry_after)
        dq.append(now)


def _rate_limited(retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail=f"操作太频繁，请 {retry_after} 秒后再试",
        headers={"Retry-After": str(retry_after)},
    )


def check_llm_rate(
    user_id: int,
    *,
    max_calls: int | None = None,
    window: int | None = None,
) -> None:
    """Record one request or raise 429; Redis mode is cross-worker and fail-closed."""
    if not user_id:
        return

    backend, configured_max_calls, configured_window, operation_timeout, redis_url = _config()
    max_calls = configured_max_calls if max_calls is None else max_calls
    window = configured_window if window is None else window
    if max_calls < 1 or window < 1:
        raise ValueError("max_calls and window must be positive")

    now = time.time()
    if backend == "local":
        _local_check(user_id, max_calls=max_calls, window=window, now=now)
        return

    try:
        _check_redis_rate(
            user_id=user_id,
            max_calls=max_calls,
            window=window,
            now=now,
            operation_timeout=operation_timeout,
            redis_url=redis_url,
        )
    except HTTPException:
        raise
    except Exception as exc:
        # redis-py socket timeouts are configured on the client.  Do not replace
        # this failure with the local dictionary: doing so multiplies quota by
        # workers precisely when the shared coordinator is unavailable.
        raise HTTPException(
            status_code=503,
            detail="系统限流协调服务暂不可用，请稍后重试",
        ) from exc

def _reset() -> None:
    """Test helper: clear the explicit local fallback and lazy Redis client."""
    global _redis_client
    with _lock:
        _hits.clear()
    with _redis_lock:
        _redis_client = None