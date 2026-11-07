"""Deadline-driven routing across primary, acceleration, and retry OCR pools.

The scheduler owns transport attempts for the explicitly enabled OpenAI-compatible
OCR route.  It keeps first-attempt pacing, pool capacity, one cross-pool retry,
and the configured batch budget in one place so legacy same-pool retry loops
cannot multiply paid calls.

No image, prompt, response, or credential is stored in Redis.  Redis receives
only opaque hashes, short-lived lease IDs, token-bucket state, circuit state, and
batch retry counters.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import logging
import math
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

from services.retry_backoff import jittered_backoff, parse_retry_after_seconds
from services.template_max_owned import MaxTemplateRejection
from services.ocr_pool_scheduler_base import (
    ResponseValidator,
    Transport,
    _ACTIVE_PRIMARY_TTL_S,
    _RETRY_BACKOFF_S,
    _RELEASE_ATTEMPTS,
    TEMPLATE_QWEN_FALLBACK_ROUTE,
    _TEMPLATE_POOL_ROUTES,
    _normalize_template_pool_route,
    OcrPoolError,
    PoolSpec,
    AcquireDecision,
    PoolPermit,
)
from services.ocr_pool_scheduler_local import (
    _TokenBucket,
    LocalPoolCoordinator,
)
from services.ocr_pool_scheduler_redis import (
    _ACQUIRE_LUA,
    _RELEASE_LUA,
    _REMOVE_FIRST_ATTEMPT_WAITER_LUA,
    _CLAIM_RETRY_LUA,
    _RELEASE_RETRY_CLAIM_LUA,
    _RECORD_FAILURE_LUA,
    _CLAIM_PROBE_LUA,
    _ABANDON_PROBE_LUA,
    RedisPoolCoordinator,
)
from services.ocr_pool_scheduler_acquire import (
    _SchedulerAcquireMixin,
)
from services.ocr_pool_scheduler_transport import (
    _SchedulerTransportMixin,
    _TRUNCATED_FINISH_REASONS,
    _SHARED_RETRYABLE_HTTP_STATUSES,
    _STUDENT_TRANSCRIPTION_MARKER,
    _template_failure_category_from_message,
    _classify_http_failure,
    _is_student_transcription_payload,
    _chat_messages_from_payload,
)
from services.ocr_pool_scheduler_requests import (
    _SchedulerRequestsMixin,
    _RETRY_LEDGER_TTL_S,
    _set_template_route_diagnostics,
)

logger = logging.getLogger(__name__)


class OcrPoolScheduler(
    _SchedulerAcquireMixin,
    _SchedulerTransportMixin,
    _SchedulerRequestsMixin,
):
    def __init__(
        self,
        *,
        primary: PoolSpec,
        failover_pool: PoolSpec | None = None,
        accelerator: PoolSpec,
        retry_pool: PoolSpec,
        template_fallback_pool: PoolSpec | None = None,
        coordinator,
        global_first_attempt_rpm: float,
        first_attempt_hard_in_flight: int | None = None,
        primary_soft_in_flight: int,
        accelerator_share: float,
        batch_deadline_s: float,
        final_guard_s: float,
        max_batch_retries: int,
        circuit_failure_threshold: int,
        circuit_cooldown_s: float,
        connect_timeout_s: float,
        write_timeout_s: float,
        sse_idle_timeout_s: float,
        batch_target_papers: int | None = None,
        batch_target_deadline_s: float | None = None,
        teacher_pool_in_flight: int | None = None,
        teacher_hash_secret: str = "test-only-scheduler-secret",
        transport: Transport | None = None,
    ) -> None:
        if not 0 <= accelerator_share <= 1:
            raise ValueError("accelerator share must be between zero and one")
        self.primary = primary
        self.failover_pool = failover_pool
        self.accelerator = accelerator
        self.retry_pool = retry_pool
        self.template_fallback_pool = template_fallback_pool
        self.coordinator = coordinator
        # The canonical template constructor sets this marker.  Keeping it
        # separate lets isolated scheduler tests retain the plain constructor
        # shape without affecting active routing.
        self._template_route = False
        self.global_first_attempt_rpm = global_first_attempt_rpm
        if first_attempt_hard_in_flight is None:
            first_attempt_hard_in_flight = (
                primary.hard_in_flight + accelerator.hard_in_flight
            )
        if not 1 <= first_attempt_hard_in_flight <= (
            primary.hard_in_flight + accelerator.hard_in_flight
        ):
            raise ValueError(
                "first-attempt aggregate capacity must fit the configured pool total"
            )
        self.first_attempt_hard_in_flight = first_attempt_hard_in_flight
        if not 1 <= primary_soft_in_flight <= primary.hard_in_flight:
            raise ValueError("primary soft capacity must be inside its hard limit")
        self.primary_soft_in_flight = primary_soft_in_flight
        self.accelerator_share = accelerator_share
        self.batch_deadline_s = batch_deadline_s
        if (batch_target_papers is None) != (batch_target_deadline_s is None):
            raise ValueError("batch deadline targets must be configured together")
        if batch_target_papers is not None and batch_target_papers < 1:
            raise ValueError("batch target paper count must be positive")
        if (
            batch_target_deadline_s is not None
            and (
                not math.isfinite(batch_target_deadline_s)
                or batch_target_deadline_s <= 0
            )
        ):
            raise ValueError("batch target deadline must be finite and positive")
        self.batch_target_papers = batch_target_papers
        self.batch_target_deadline_s = batch_target_deadline_s
        self.final_guard_s = final_guard_s
        self.max_batch_retries = max_batch_retries
        self.circuit_failure_threshold = circuit_failure_threshold
        self.circuit_cooldown_s = circuit_cooldown_s
        self.connect_timeout_s = connect_timeout_s
        self.write_timeout_s = write_timeout_s
        self.sse_idle_timeout_s = sse_idle_timeout_s
        if teacher_pool_in_flight is None:
            teacher_pool_in_flight = first_attempt_hard_in_flight
        if not 1 <= teacher_pool_in_flight <= first_attempt_hard_in_flight:
            raise ValueError(
                "teacher aggregate capacity must fit the first-attempt global limit"
            )
        self.teacher_pool_in_flight = teacher_pool_in_flight
        self._teacher_hash_secret = teacher_hash_secret.encode("utf-8")
        self._transport = transport or self._send_stream
        self._stats: dict[str, int] = {
            "first_primary": 0,
            "first_accelerator": 0,
            "retry_started": 0,
            "retry_succeeded": 0,
            "retry_rejected": 0,
        }

    @classmethod
    def from_template_config(
        cls,
        *,
        pool_route: str | None = None,
    ) -> "OcrPoolScheduler":
        """Build the scheduler for the qwen3.8-max Token Plan template route.

        The GPT canonical and YS/exemption routes are retired; asking for them
        fails closed in ``_normalize_template_pool_route``.
        """
        import config

        requested_route = _normalize_template_pool_route(pool_route)

        def configured(name: str) -> str:
            return str(getattr(config, name, "") or "").strip()

        def make_pool(
            *,
            name: str,
            base_url: str,
            api_key: str,
            model: str,
            max_rpm: float,
            hard_in_flight: int,
            first_event_timeout_s: float,
            attempt_ttl_s: float,
            burst: int,
            provider_profile: str,
            api_protocol: str,
        ) -> PoolSpec:
            return PoolSpec(
                name=name,
                base_url=base_url,
                api_key=api_key,
                model=model,
                max_rpm=max_rpm,
                hard_in_flight=hard_in_flight,
                first_event_timeout_s=first_event_timeout_s,
                attempt_ttl_s=attempt_ttl_s,
                burst=burst,
                provider_profile=provider_profile,
                execution_target="server",
                api_protocol=api_protocol,
            )

        fallback = None
        # Enablement is derived from all three fields at construction time; a
        # stale boolean or a default model must never activate this route.
        if all(
            configured(name)
            for name in (
                "OCR_TEMPLATE_GPT_FALLBACK_BASE_URL",
                "OCR_TEMPLATE_GPT_FALLBACK_API_KEY",
                "OCR_TEMPLATE_GPT_FALLBACK_MODEL",
            )
        ):
            fallback = make_pool(
                name=config.OCR_TEMPLATE_GPT_FALLBACK_NAME,
                base_url=config.OCR_TEMPLATE_GPT_FALLBACK_BASE_URL,
                api_key=config.OCR_TEMPLATE_GPT_FALLBACK_API_KEY,
                model=config.OCR_TEMPLATE_GPT_FALLBACK_MODEL,
                max_rpm=config.OCR_TEMPLATE_QWEN_FALLBACK_MAX_RPM,
                hard_in_flight=config.OCR_TEMPLATE_GPT_FALLBACK_HARD_IN_FLIGHT,
                first_event_timeout_s=config.OCR_TEMPLATE_GPT_FALLBACK_FIRST_EVENT_TIMEOUT_S,
                attempt_ttl_s=config.OCR_TEMPLATE_GPT_FALLBACK_ATTEMPT_TTL_S,
                # Token Plan has no RPM; the burst only admits the in-flight window.
                burst=config.OCR_TEMPLATE_GPT_FALLBACK_HARD_IN_FLIGHT,
                provider_profile="failover",
                # qwen3.8-max is a Chat Completions route.
                api_protocol=config.OCR_TEMPLATE_QWEN_FALLBACK_API_PROTOCOL,
            )
        if fallback is None:
            raise RuntimeError(
                "OCR template qwen3.8max fallback is not configured; set "
                "OCR_TEMPLATE_GPT_FALLBACK_BASE_URL, "
                "OCR_TEMPLATE_GPT_FALLBACK_API_KEY, and "
                "OCR_TEMPLATE_GPT_FALLBACK_MODEL"
            )
        primary = fallback
        failover_pool = None
        retry_pool = primary
        accelerator = primary
        primary_available = False

        route_pools = [primary]
        if failover_pool is not None and failover_pool is not primary:
            route_pools.append(failover_pool)
        aggregate_capacity = sum(pool.hard_in_flight for pool in route_pools)
        aggregate_rpm = sum(
            pool.max_rpm for pool in route_pools if pool.max_rpm > 0
        )
        aggregate_burst = max(pool.burst for pool in route_pools)

        if config.OCR_MULTI_POOL_COORDINATOR == "local":
            coordinator = LocalPoolCoordinator(
                global_rpm=aggregate_rpm,
                global_burst=aggregate_burst,
            )
        else:
            import redis.asyncio as redis_async

            redis_client = redis_async.from_url(
                config.REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=config.MODEL_LIMITER_REDIS_OPERATION_TIMEOUT_SECONDS,
                socket_timeout=config.MODEL_LIMITER_REDIS_OPERATION_TIMEOUT_SECONDS,
                health_check_interval=30,
            )
            coordinator = RedisPoolCoordinator(
                redis_client,
                namespace="mathagent:ocr-template-gpt:v1",
                global_rpm=aggregate_rpm,
                global_burst=aggregate_burst,
                operation_timeout_s=config.MODEL_LIMITER_REDIS_OPERATION_TIMEOUT_SECONDS,
            )

        scheduler = cls(
            primary=primary,
            failover_pool=failover_pool,
            accelerator=accelerator,
            retry_pool=retry_pool,
            template_fallback_pool=fallback,
            coordinator=coordinator,
            global_first_attempt_rpm=aggregate_rpm,
            first_attempt_hard_in_flight=aggregate_capacity,
            primary_soft_in_flight=primary.hard_in_flight,
            accelerator_share=0.0,
            batch_deadline_s=max(
                primary.attempt_ttl_s,
                fallback.attempt_ttl_s if fallback is not None else 0.0,
            ),
            final_guard_s=0.0,
            max_batch_retries=1,
            circuit_failure_threshold=config.OCR_POOL_CIRCUIT_FAILURE_THRESHOLD,
            circuit_cooldown_s=config.OCR_POOL_CIRCUIT_COOLDOWN_S,
            connect_timeout_s=config.OCR_CONNECT_TIMEOUT_S,
            write_timeout_s=config.OCR_WRITE_TIMEOUT_S,
            sse_idle_timeout_s=config.OCR_SSE_IDLE_TIMEOUT_S,
            teacher_pool_in_flight=aggregate_capacity,
            teacher_hash_secret=config.JWT_SECRET,
        )
        scheduler._template_route = True
        scheduler._template_pool_route = requested_route
        scheduler._template_primary_available = primary_available
        return scheduler

    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    async def close(self) -> None:
        await self.coordinator.close()


_template_schedulers: dict[str, OcrPoolScheduler] = {}


def get_template_ocr_pool_scheduler(
    *,
    pool_route: str | None = None,
) -> OcrPoolScheduler:
    """Return the cached qwen3.8-max Token Plan template scheduler.

    Retired route names fail closed; no route can borrow a retired pool's
    credentials.
    """
    requested_route = _normalize_template_pool_route(pool_route)
    scheduler = _template_schedulers.get(requested_route)
    if scheduler is None:
        scheduler = OcrPoolScheduler.from_template_config(
            pool_route=requested_route,
        )
        _template_schedulers[requested_route] = scheduler
    return scheduler


def _get_template_scheduler_for_readiness(pool_route: str) -> OcrPoolScheduler:
    """Resolve a readiness scheduler while preserving legacy test injectors."""
    getter = get_template_ocr_pool_scheduler
    try:
        parameters = inspect.signature(getter).parameters
    except (TypeError, ValueError):
        parameters = None
    if parameters is not None and (
        "pool_route" not in parameters
        and not any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
    ):
        return getter()
    return getter(pool_route=pool_route)


async def ensure_ocr_pool_scheduler_ready() -> None:
    import config

    # Student OCR uses the Qwen adapter. Template positioning has one route:
    # qwen3.8-max on the Token Plan. The GPT canonical and YS/exemption routes
    # are retired, so their leftover credentials are neither probed nor
    # initialized at startup.
    fallback_configured = all(
        str(getattr(config, name, "") or "").strip()
        for name in (
            "OCR_TEMPLATE_GPT_FALLBACK_BASE_URL",
            "OCR_TEMPLATE_GPT_FALLBACK_API_KEY",
            "OCR_TEMPLATE_GPT_FALLBACK_MODEL",
        )
    )
    if not fallback_configured:
        return
    scheduler = _get_template_scheduler_for_readiness("fallback")
    if isinstance(scheduler.coordinator, RedisPoolCoordinator):
        await scheduler.coordinator.ping()


async def reset_ocr_pool_scheduler_for_tests() -> None:
    template_currents = list({id(item): item for item in _template_schedulers.values()}.values())
    _template_schedulers.clear()
    for scheduler in template_currents:
        await scheduler.close()
