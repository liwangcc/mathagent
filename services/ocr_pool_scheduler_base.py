"""What every part of the OCR pool scheduler shares: pools, permits, errors and template routes.

- ``PoolSpec`` describes one pool (endpoint, model and protocol, rate and
  in-flight limits, timeouts);
  ``AcquireDecision`` is a coordinator's answer to a capacity request and
  ``PoolPermit`` the capacity a caller holds until it releases it
  (``_RELEASE_ATTEMPTS``, ``_RETRY_BACKOFF_S``).
- ``OcrPoolError`` carries a failure's category and whether another pool may retry it.
- ``ResponseValidator`` and ``Transport`` are the call signatures the scheduler
  takes; ``_ACTIVE_PRIMARY_TTL_S`` is how long both coordinators remember the active primary pool.
- ``TEMPLATE_QWEN_FALLBACK_ROUTE`` and ``_normalize_template_pool_route``
  (``_TEMPLATE_POOL_ROUTES``) name the template pool routes.

Moved unchanged from services/ocr_pool_scheduler.py, which still exposes these names.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


ResponseValidator = Callable[[str], Any]
Transport = Callable[["PoolSpec", dict[str, Any], str], Awaitable[str]]
_ACTIVE_PRIMARY_TTL_S = 300.0
_RETRY_BACKOFF_S = 0.05


_RELEASE_ATTEMPTS = 3


TEMPLATE_QWEN_FALLBACK_ROUTE = "qwen3.8max"
# The qwen3.8-max Token Plan route is the only template route; the GPT
# canonical and YS/exemption routes are retired.
_TEMPLATE_POOL_ROUTES = frozenset({"fallback", "qwen", "qwen3.8max"})


def _normalize_template_pool_route(pool_route: str | None) -> str:
    route = str(pool_route if pool_route is not None else "fallback").strip().lower()
    if route not in _TEMPLATE_POOL_ROUTES:
        raise ValueError(
            f"OCR template route {route!r} is retired; only the qwen3.8-max "
            "Token Plan route (pool_route='fallback') remains"
        )
    return "fallback"


class OcrPoolError(RuntimeError):
    """A classified pool failure safe for routing decisions."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        transport_failure: bool = False,
        status_code: int | None = None,
        retry_after_s: float | None = None,
        request_sent: bool | None = None,
        failure_category: str | None = None,
        actual_model: str | None = None,
        route_revision: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.transport_failure = transport_failure
        self.status_code = status_code
        self.retry_after_s = retry_after_s
        # ``request_sent`` is intentionally tri-state.  ``False`` means the
        # failure happened before provider dispatch (for example, while
        # waiting for a lease); ``True`` means a provider attempt was entered;
        # ``None`` preserves compatibility for older injected transports.
        self.request_sent = request_sent
        self.failure_category = failure_category
        self.actual_model = actual_model
        self.route_revision = route_revision


@dataclass(frozen=True)
class PoolSpec:
    name: str
    base_url: str
    api_key: str
    model: str
    max_rpm: float
    hard_in_flight: int
    first_event_timeout_s: float
    attempt_ttl_s: float
    burst: int = 1
    borrowed_in_flight: int | None = None
    provider_profile: str = "primary"
    execution_target: str = "server"
    api_protocol: str = "responses"

    def __post_init__(self) -> None:
        if not self.name or not self.model:
            raise ValueError("OCR pool requires name, endpoint, credential, and model")
        if self.execution_target != "server":
            raise ValueError("OCR model requests must execute on the server")
        if self.provider_profile not in {
            "primary",
            "failover",
            "accelerator",
            "retry",
        }:
            raise ValueError("OCR pool provider profile is invalid")
        if self.api_protocol not in {"chat_completions", "responses"}:
            raise ValueError("OCR pool API protocol is invalid")
        if not self.base_url or not self.api_key:
            raise ValueError("OCR pool requires name, endpoint, credential, and model")
        if (
            not math.isfinite(self.max_rpm)
            or self.max_rpm < 0
            or self.hard_in_flight < 1
        ):
            raise ValueError(
                "OCR pool RPM must be nonnegative and in-flight capacity positive"
            )
        if (
            not math.isfinite(self.first_event_timeout_s)
            or not math.isfinite(self.attempt_ttl_s)
            or self.first_event_timeout_s <= 0
            or self.attempt_ttl_s <= 0
        ):
            raise ValueError("OCR pool timeouts must be positive")
        if self.burst < 1:
            raise ValueError("OCR pool burst must be positive")
        if self.borrowed_in_flight is not None and not (
            1 <= self.borrowed_in_flight <= self.hard_in_flight
        ):
            raise ValueError("borrowed capacity must be inside the pool hard limit")

    @property
    def coordination_key(self) -> str:
        """Opaque key that changes when routing or provider configuration rotates."""
        material = "\0".join((
            self.name,
            self.execution_target,
            self.provider_profile,
            self.api_protocol,
            self.base_url.rstrip("/"),
            self.model,
            self.api_key,
        ))
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
        return f"{self.name}:{digest}"


@dataclass
class AcquireDecision:
    permit: "PoolPermit | None" = None
    wait_s: float = 0.05
    circuit_open: bool = False


class PoolPermit:
    def __init__(
        self,
        coordinator,
        *,
        pool_name: str,
        lease_id: str,
        borrowed: bool,
        teacher_hash: str = "",
        global_first_attempt: bool = False,
    ) -> None:
        self._coordinator = coordinator
        self.pool_name = pool_name
        self.lease_id = lease_id
        self.borrowed = borrowed
        self.teacher_hash = teacher_hash
        self.global_first_attempt = global_first_attempt
        self._released = False
        self._release_lock = asyncio.Lock()

    async def release(self) -> None:
        async with self._release_lock:
            if self._released:
                return
            # Redis ZREM is idempotent.  Mark the local permit released only
            # after the coordinator acknowledges it. Retry transient coordinator
            # failures here because callers generally lose the permit reference
            # when their request-level finally block exits.
            for attempt in range(_RELEASE_ATTEMPTS):
                try:
                    await self._coordinator.release(self)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if attempt + 1 >= _RELEASE_ATTEMPTS:
                        raise
                    await asyncio.sleep(_RETRY_BACKOFF_S * (2**attempt))
                else:
                    self._released = True
                    return
