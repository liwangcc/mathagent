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

logger = logging.getLogger(__name__)

_RETRY_LEDGER_TTL_S = 86_400.0


def _set_template_route_diagnostics(
    diagnostics: dict[str, object] | None,
    *,
    route: str,
    reasoning_effort: str,
) -> None:
    """Record the provider lane actually used by a template request.

    A mutable, caller-owned diagnostics object keeps the route visible without
    changing the string response contract or leaking provider credentials.
    """
    if not isinstance(diagnostics, dict):
        return
    diagnostics["actual_route"] = str(route)
    diagnostics["actual_reasoning_effort"] = str(reasoning_effort)


class OcrPoolScheduler(_SchedulerAcquireMixin, _SchedulerTransportMixin):
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

    def _batch_deadline_for_papers(self, paper_count: int | None) -> float:
        """Use the actual batch size without weakening one complete retry."""
        if paper_count is None or self.batch_target_papers is None:
            return self.batch_deadline_s
        if isinstance(paper_count, bool) or not isinstance(paper_count, int):
            raise ValueError("batch paper count must be a positive integer")
        if paper_count < 1:
            raise ValueError("batch paper count must be a positive integer")
        assert self.batch_target_deadline_s is not None
        target_windows = math.ceil(paper_count / self.batch_target_papers)
        target_deadline = target_windows * self.batch_target_deadline_s
        complete_retry_reserve = (
            max(
                self.primary.attempt_ttl_s,
                self.failover_pool.attempt_ttl_s if self.failover_pool else 0,
                self.accelerator.attempt_ttl_s,
            )
            + self.retry_pool.attempt_ttl_s
            + self.final_guard_s
        )
        return max(
            complete_retry_reserve,
            min(target_deadline, self.batch_deadline_s),
        )

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

    def _teacher_hash(self, teacher_id: str) -> str:
        if not str(teacher_id or "").strip():
            raise OcrPoolError(
                "missing_teacher_identity", retryable=False, transport_failure=False
            )
        return hmac.new(
            self._teacher_hash_secret,
            str(teacher_id).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:24]

    def _batch_hash(self, batch_key: str) -> str:
        return hashlib.sha256(batch_key.encode("utf-8")).hexdigest()[:24]

    async def request(
        self,
        payload: dict[str, Any],
        *,
        batch_key: str,
        paper_key: str,
        teacher_id: str,
        validator: ResponseValidator | None = None,
        priority: str = "batch",
        retry_batch_key: str | None = None,
        retry_paper_key: str | None = None,
        batch_paper_count: int | None = None,
        batch_deadline_s: float | None = None,
    ) -> str:
        if getattr(self, "_template_route", False):
            # Template calls must enter request_template_fallback_pool().
            # Keeping the generic batch graph closed prevents an old caller
            # from silently reactivating a retired pool route.
            if not getattr(self, "_template_primary_available", False):
                raise OcrPoolError(
                    "template_primary_pool_unconfigured",
                    retryable=False,
                    transport_failure=False,
                )
        teacher_hash = self._teacher_hash(teacher_id)
        batch_hash = self._batch_hash(batch_key)
        paper_hash = hashlib.sha256(paper_key.encode("utf-8")).hexdigest()[:24]
        computed_batch_deadline_s = self._batch_deadline_for_papers(
            batch_paper_count
        )
        if batch_deadline_s is None:
            batch_deadline_s = computed_batch_deadline_s
        else:
            try:
                batch_deadline_s = float(batch_deadline_s)
            except (TypeError, ValueError) as exc:
                raise ValueError("batch_deadline_s must be finite and positive") from exc
            if not math.isfinite(batch_deadline_s) or batch_deadline_s <= 0:
                raise ValueError("batch_deadline_s must be finite and positive")
            batch_deadline_s = max(
                computed_batch_deadline_s,
                batch_deadline_s,
            )
        batch_started = await self.coordinator.get_batch_start(
            batch_hash, batch_deadline_s
        )
        phase_hash = self._batch_hash(f"{batch_hash}:{batch_started:.6f}")
        retry_batch_hash = (
            self._batch_hash(retry_batch_key)
            if retry_batch_key is not None
            else phase_hash
        )
        retry_paper_hash = (
            hashlib.sha256(retry_paper_key.encode("utf-8")).hexdigest()[:24]
            if retry_paper_key is not None
            else paper_hash
        )
        if priority == "interactive" and self.accelerator_share > 0:
            return await self._interactive_request(
                payload,
                retry_batch_hash,
                retry_paper_hash,
                teacher_hash,
                batch_started,
                validator,
                batch_deadline_s,
            )

        # A healthy first attempt must not be held behind capacity for every
        # hypothetical retry in the batch.  Each actual retry is separately
        # rate-limited and deadline-bound in _run_retry.  Reserving the whole
        # batch's retry token span here can place this deadline before the
        # batch even begins when the retry pool has a lower RPM.
        latest_first_start = (
            batch_started
            + batch_deadline_s
            - max(
                self.primary.attempt_ttl_s,
                self.failover_pool.attempt_ttl_s if self.failover_pool else 0,
                self.accelerator.attempt_ttl_s,
            )
        )
        first_waiter_id = uuid.uuid4().hex
        try:
            try:
                pool, permit, accelerator_probe = await self._acquire_first(
                    phase_hash,
                    route_key=paper_key,
                    paper_hash=paper_hash,
                    teacher_hash=teacher_hash,
                    waiter_id=first_waiter_id,
                    deadline_epoch=latest_first_start,
                )
            except BaseException:
                await self._remove_first_attempt_waiter(
                    teacher_hash,
                    first_waiter_id,
                )
                raise
        except OcrPoolError as first_error:
            if not first_error.retryable:
                raise
            return await self._run_retry(
                payload,
                validator=validator,
                retry_batch_hash=retry_batch_hash,
                retry_paper_hash=retry_paper_hash,
                batch_started=batch_started,
                batch_deadline_s=batch_deadline_s,
                first_error=first_error,
            )
        try:
            content = await self._execute(pool, payload, validator)
        except asyncio.CancelledError:
            if accelerator_probe:
                try:
                    await self.coordinator.finish_accelerator_probe(
                        phase_hash, False
                    )
                except Exception:
                    logger.exception(
                        "Failed to close cancelled accelerator health probe gate"
                    )
            raise
        except OcrPoolError as first_error:
            if accelerator_probe:
                await self.coordinator.finish_accelerator_probe(phase_hash, False)
            if not first_error.retryable:
                raise
            # Do not hold a failed first-attempt slot while waiting for the
            # configured retry lane. Upstream work may linger, but our bounded lease has
            # already ended and keeping the local/Redis slot would deadlock bursts.
            await permit.release()
            if (
                first_error.transport_failure
                and pool is self.primary
                and self.failover_pool is not None
                and await self.coordinator.is_circuit_open(
                    self.primary.coordination_key
                )
            ):
                await self.coordinator.set_active_primary(self.failover_pool.name)
            return await self._run_retry(
                payload,
                validator=validator,
                retry_batch_hash=retry_batch_hash,
                retry_paper_hash=retry_paper_hash,
                batch_started=batch_started,
                batch_deadline_s=batch_deadline_s,
                first_error=first_error,
            )
        else:
            if accelerator_probe:
                await self.coordinator.finish_accelerator_probe(phase_hash, True)
            return content
        finally:
            await permit.release()

    async def _run_retry(
        self,
        payload: dict[str, Any],
        *,
        validator: ResponseValidator | None,
        retry_batch_hash: str,
        retry_paper_hash: str,
        batch_started: float,
        batch_deadline_s: float,
        first_error: OcrPoolError,
    ) -> str:
        latest_retry_start = (
            batch_started
            + batch_deadline_s
            - self.retry_pool.attempt_ttl_s
            - self.final_guard_s
        )
        if time.time() > latest_retry_start:
            self._stats["retry_rejected"] += 1
            raise OcrPoolError(
                "retry_budget_exhausted",
                retryable=False,
                transport_failure=False,
            ) from first_error
        await self._sleep_before_retry(latest_retry_start)
        if not await self.coordinator.claim_retry(
            retry_batch_hash,
            retry_paper_hash,
            self.max_batch_retries,
            max(_RETRY_LEDGER_TTL_S, batch_deadline_s),
        ):
            self._stats["retry_rejected"] += 1
            raise OcrPoolError(
                "batch_retry_limit_reached",
                retryable=False,
                transport_failure=False,
            ) from first_error
        try:
            retry_permit = await self._acquire_retry(
                deadline_epoch=latest_retry_start
            )
        except BaseException:
            try:
                await self.coordinator.release_retry_claim(
                    retry_batch_hash,
                    retry_paper_hash,
                )
            except Exception:
                logger.exception("Failed to release unused OCR retry reservation")
            raise
        try:
            self._stats["retry_started"] += 1
            content = await self._execute(self.retry_pool, payload, validator)
            self._stats["retry_succeeded"] += 1
            return content
        finally:
            await retry_permit.release()

    async def request_template_fallback_pool(
        self,
        payload: dict[str, Any],
        *,
        request_key: str,
        validator: ResponseValidator | None = None,
        route_diagnostics: dict[str, object] | None = None,
        reasoning_effort: str = "high",
    ) -> str:
        """Run one template request on the qwen3.8-max Token Plan pool.

        This is intentionally a direct pool request: only the Token Plan
        in-flight gate (and an optional rate budget) admits it.
        """
        if not request_key:
            raise ValueError("request_key is required")
        if reasoning_effort not in {"low", "high"}:
            raise ValueError("template Qwen reasoning must be low or high")
        fallback_pool = self.template_fallback_pool
        if fallback_pool is None:
            raise OcrPoolError(
                "template_fallback_pool_unconfigured",
                retryable=False,
                transport_failure=False,
            )
        if isinstance(route_diagnostics, dict):
            route_diagnostics["fallback_attempted"] = True
            route_diagnostics["replacement_attempted"] = True
            route_diagnostics["actual_model"] = str(fallback_pool.model)
            # Route selection is visible immediately for timeout budgeting,
            # but the paid-request one-shot marker is set only after TPM and
            # in-flight admission succeed.  This lets a rejected admission
            # retry without making diagnostics claim that Qwen was contacted.
            route_diagnostics["_fallback_admission_pending"] = True
        _set_template_route_diagnostics(
            route_diagnostics,
            route=TEMPLATE_QWEN_FALLBACK_ROUTE,
            reasoning_effort=reasoning_effort,
        )
        permit = None
        try:
            await self._acquire_template_fallback_rate_budget(payload)
            fallback_payload = dict(payload)
            # Emergency callers retain high; the dedicated transcription leg
            # explicitly requests low instead of inheriting payload metadata.
            fallback_payload["model_reasoning_effort"] = reasoning_effort
            deadline = time.time() + fallback_pool.attempt_ttl_s
            permit = await self._wait_for_pool(
                fallback_pool,
                capacity_limit=fallback_pool.hard_in_flight,
                borrowed=False,
                use_global_gate=False,
                deadline_epoch=deadline,
            )
            # This marker means the Qwen request crossed both TPM admission and
            # the in-flight permit boundary and is about to enter provider
            # transport. Keep it unset while either admission step is pending,
            # so diagnostics cannot claim a request was sent when it was only
            # queued or rejected locally.
            if isinstance(route_diagnostics, dict):
                route_diagnostics["_fallback_admission_pending"] = False
                route_diagnostics["_fallback_request_started"] = True
            return await self._execute(fallback_pool, fallback_payload, validator)
        finally:
            if isinstance(route_diagnostics, dict) and not route_diagnostics.get(
                "_fallback_request_started"
            ):
                route_diagnostics["_fallback_admission_pending"] = False
            if permit is not None:
                await permit.release()

    async def _interactive_request(
        self,
        payload: dict[str, Any],
        retry_batch_hash: str,
        retry_paper_hash: str,
        teacher_hash: str,
        batch_started: float,
        validator: ResponseValidator | None,
        batch_deadline_s: float,
    ) -> str:
        waiter_id = uuid.uuid4().hex
        await self.coordinator.add_priority_waiter(
            self.accelerator.coordination_key,
            waiter_id,
            self.accelerator.attempt_ttl_s + 30.0,
        )
        deadline = time.time() + self.accelerator.attempt_ttl_s
        try:
            permit = await self._wait_for_pool(
                self.accelerator,
                capacity_limit=self.accelerator.hard_in_flight,
                borrowed=False,
                use_global_gate=False,
                teacher_hash=teacher_hash,
                teacher_limit=self.teacher_pool_in_flight,
                deadline_epoch=deadline,
            )
            try:
                return await self._execute(self.accelerator, payload, validator)
            except OcrPoolError as first_error:
                if not first_error.retryable:
                    raise
                await permit.release()
                return await self._run_retry(
                    payload,
                    validator=validator,
                    retry_batch_hash=retry_batch_hash,
                    retry_paper_hash=retry_paper_hash,
                    batch_started=batch_started,
                    batch_deadline_s=batch_deadline_s,
                    first_error=first_error,
                )
            finally:
                await permit.release()
        finally:
            await self.coordinator.remove_priority_waiter(
                self.accelerator.coordination_key, waiter_id
            )

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
