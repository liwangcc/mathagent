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

logger = logging.getLogger(__name__)

_RETRY_LEDGER_TTL_S = 86_400.0
# Provider finish_reason values that mean the answer stopped mid-generation.
# They must never be accepted as a complete model answer.
_TRUNCATED_FINISH_REASONS = {"length", "max_tokens", "max_output_tokens"}
_SHARED_RETRYABLE_HTTP_STATUSES = frozenset({401, 403, 404, 408, 429})
_STUDENT_TRANSCRIPTION_MARKER = "[task=student_transcription_only]"


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


def _template_failure_category_from_message(message: object) -> str:
    """Classify response validation errors without inspecting response data."""
    text = str(message or "").strip().lower()
    # These failures describe an ambiguous/low-confidence result, not a bad
    # provider lane.  Retrying on another paid model would silently turn an
    # evidentiary review condition into an attribution decision.
    semantic_markers = (
        "confidence",
        "low_confidence",
        "ambiguous",
        "needs_review",
        "semantic",
        "uncertain",
    )
    if any(marker in text for marker in semantic_markers):
        return "semantic"
    return "contract"


def _classify_http_failure(status_code: int) -> tuple[bool, bool]:
    """Return ``(retryable, shared_failure)`` for an upstream HTTP status.

    Authentication, endpoint/model, throttling, timeout, and server failures
    describe the shared pool and may be retried elsewhere. Other 4xx responses
    are request-specific and must not poison the pool-wide circuit.
    """
    shared_failure = (
        status_code in _SHARED_RETRYABLE_HTTP_STATUSES or status_code >= 500
    )
    return shared_failure, shared_failure


def _is_student_transcription_payload(payload: dict[str, Any]) -> bool:
    """Match the narrow pure-transcription contract without inspecting images."""
    for message in payload.get("input") or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str) and _STUDENT_TRANSCRIPTION_MARKER in content:
            return True
        if not isinstance(content, list):
            continue
        for item in content:
            if (
                isinstance(item, dict)
                and isinstance(item.get("text"), str)
                and _STUDENT_TRANSCRIPTION_MARKER in item["text"]
            ):
                return True
    return False


def _chat_messages_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate the internal Responses-shaped OCR payload to Chat Completions."""
    source = payload.get("input")
    if not isinstance(source, list):
        source = payload.get("messages")
    if not isinstance(source, list):
        raise OcrPoolError(
            "missing_messages",
            retryable=False,
            transport_failure=False,
            request_sent=False,
            failure_category="pre_dispatch",
        )

    messages: list[dict[str, Any]] = []
    for message in source:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        content = message.get("content")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            continue
        converted: list[dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type in {"input_text", "text"}:
                text = item.get("text")
                if isinstance(text, str):
                    converted.append({"type": "text", "text": text})
                continue
            if item_type in {"input_image", "image_url"}:
                image_url = item.get("image_url")
                if isinstance(image_url, dict):
                    image_url = image_url.get("url")
                if isinstance(image_url, str) and image_url:
                    converted.append({
                        "type": "image_url",
                        "image_url": {
                            "url": image_url,
                            "detail": item.get("detail", "high"),
                        },
                    })
        if converted:
            messages.append({"role": role, "content": converted})
    if not messages:
        raise OcrPoolError(
            "missing_messages",
            retryable=False,
            transport_failure=False,
            request_sent=False,
            failure_category="pre_dispatch",
        )
    return messages


class OcrPoolScheduler:
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

    async def _remove_first_attempt_waiter(
        self,
        teacher_hash: str,
        waiter_id: str,
    ) -> None:
        remove_waiter = getattr(self.coordinator, "remove_first_attempt_waiter", None)
        if remove_waiter is None:
            return
        try:
            await remove_waiter(teacher_hash, waiter_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The coordinator's short waiter TTL is a backstop. Cleanup must not
            # turn a completed/cancelled OCR dispatch into a user-facing failure.
            logger.warning("failed to remove OCR first-attempt waiter", exc_info=True)

    def _prefer_accelerator(self, route_key: str, paper_hash: str) -> bool:
        if self.accelerator_share <= 0:
            return False
        if self.accelerator_share >= 1:
            return True
        material = f"{route_key}\0{paper_hash}".encode("utf-8")
        bucket = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
        threshold = int(self.accelerator_share * (1 << 64))
        return bucket < threshold

    async def _pool_route_available(self, pool: PoolSpec) -> bool:
        del pool
        return True

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

    async def _acquire_template_fallback_rate_budget(self, payload: dict[str, Any]) -> None:
        """Admission seam before the in-flight permit; intentionally no rate gate.

        qwen3.8-max runs on a Token Plan subscription: Credits billing, no
        RPM/TPM quota. Its only provider limit is the tier concurrency, which
        the pool's hard_in_flight enforces (see config OCR_TEMPLATE_GPT_FALLBACK_*).
        """
        return None

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

    async def _sleep_before_retry(self, deadline_epoch: float) -> None:
        """Stagger one cross-pool retry without escaping its absolute budget."""
        remaining = deadline_epoch - time.time()
        if remaining <= 0:
            return
        delay = jittered_backoff(
            _RETRY_BACKOFF_S,
            jitter_ratio=3.0,
            max_jitter_s=0.25,
        )
        await asyncio.sleep(min(delay, remaining))

    async def _acquire_first(
        self,
        batch_hash: str,
        *,
        route_key: str,
        paper_hash: str,
        teacher_hash: str,
        deadline_epoch: float,
        waiter_id: str = "",
    ) -> tuple[PoolSpec, PoolPermit, bool]:
        route_availability: dict[str, bool] = {}

        async def route_available(pool: PoolSpec) -> bool:
            pool_key = pool.coordination_key
            available = route_availability.get(pool_key)
            if available is None:
                available = await self._pool_route_available(pool)
                route_availability[pool_key] = available
            return available

        accelerator_enabled = (
            self.accelerator_share > 0
            and await route_available(self.accelerator)
        )
        # A full accelerator share means the accelerator is the verified
        # primary lane, not an
        # opportunistic borrowed accelerator. Do not serialize a batch behind
        # one health probe before using its independent 30-stream window.
        accelerator_is_primary = self.accelerator_share >= 1.0
        prefer_accelerator = accelerator_enabled and self._prefer_accelerator(
            route_key, paper_hash
        )
        use_primary_hard_capacity = False
        probe_deferred = False
        while time.time() <= deadline_epoch:
            gate = (
                "healthy"
                if accelerator_enabled and accelerator_is_primary
                else await self.coordinator.accelerator_gate(batch_hash)
                if accelerator_enabled
                else "blocked"
            )
            accelerator_allowed = accelerator_enabled and gate == "healthy"
            probe_claimed = False
            # The first accelerator-selected attempt in a batch is a same-workload
            # health probe for borrowed accelerator capacity. Do not force
            # primary-selected papers onto the accelerator merely to probe it,
            # or small batches would exceed the
            # configured accelerator share substantially.
            if (
                accelerator_enabled
                and not accelerator_is_primary
                and prefer_accelerator
                and gate == "unknown"
                and not probe_deferred
            ):
                probe_claimed = await self.coordinator.claim_accelerator_probe(batch_hash)
                if probe_claimed:
                    try:
                        decision = await self.coordinator.try_acquire(
                            self.accelerator,
                            capacity_limit=self.accelerator.hard_in_flight,
                            borrowed=True,
                            use_global_gate=True,
                            teacher_hash=teacher_hash,
                            teacher_limit=0,
                            global_in_flight_limit=self.first_attempt_hard_in_flight,
                            global_teacher_limit=self.teacher_pool_in_flight,
                            waiter_id=waiter_id,
                        )
                    except asyncio.CancelledError:
                        try:
                            await self.coordinator.finish_accelerator_probe(
                                batch_hash, False
                            )
                        except Exception:
                            logger.exception(
                                "Failed to close cancelled accelerator probe dispatch gate"
                            )
                        raise
                    except Exception:
                        try:
                            await self.coordinator.abandon_accelerator_probe(
                                batch_hash
                            )
                        except Exception:
                            logger.exception(
                                "Failed to abandon errored accelerator probe dispatch gate"
                            )
                        raise
                    if decision.permit is not None:
                        if time.time() > deadline_epoch:
                            await decision.permit.release()
                            await self.coordinator.abandon_accelerator_probe(
                                batch_hash
                            )
                            break
                        self._stats["first_accelerator"] += 1
                        return self.accelerator, decision.permit, True
                    if decision.circuit_open:
                        await self.coordinator.finish_accelerator_probe(
                            batch_hash, False
                        )
                        gate = "blocked"
                    else:
                        # Capacity, priority, or pacing is temporarily unavailable.
                        # Release the claim and keep this paper moving through the
                        # primary route; a different paper may perform the
                        # same-workload probe.
                        await self.coordinator.abandon_accelerator_probe(batch_hash)
                        probe_deferred = True
                # Another request is already probing the accelerator. Keep the
                # batch moving through the primary pool; only future requests
                # may borrow accelerator capacity
                # after that one same-workload probe marks the gate healthy.
                accelerator_allowed = False

            choices: list[tuple[PoolSpec, int, bool, bool]] = []
            accelerator_choice = (
                self.accelerator,
                self.accelerator.hard_in_flight,
                True,
                probe_claimed,
            )
            active_name = await self.coordinator.get_active_primary(self.primary.name)
            first_pool = (
                self.failover_pool
                if self.failover_pool is not None and active_name == self.failover_pool.name
                else self.primary
            )
            standby_pool = (
                self.primary
                if self.failover_pool is not None and first_pool is self.failover_pool
                else self.failover_pool
            )
            primary_soft = (
                first_pool,
                (
                    first_pool.hard_in_flight
                    if use_primary_hard_capacity
                    else self.primary_soft_in_flight
                ),
                False,
                False,
            )
            if prefer_accelerator and accelerator_allowed:
                choices.extend((accelerator_choice, primary_soft))
            else:
                choices.append(primary_soft)
                if accelerator_allowed:
                    choices.append(accelerator_choice)
            if standby_pool is not None:
                choices.append((standby_pool, standby_pool.hard_in_flight, False, False))

            waits: list[float] = []
            all_circuits = True
            active_primary_circuit_open = not await route_available(first_pool)
            for pool, capacity, borrowed, is_probe in choices:
                if pool is standby_pool and not active_primary_circuit_open:
                    continue
                if not await route_available(pool):
                    continue
                decision = await self.coordinator.try_acquire(
                    pool,
                    capacity_limit=capacity,
                    borrowed=borrowed,
                    use_global_gate=True,
                    teacher_hash=teacher_hash,
                    teacher_limit=0,
                    global_in_flight_limit=self.first_attempt_hard_in_flight,
                    global_teacher_limit=self.teacher_pool_in_flight,
                    waiter_id=waiter_id,
                )
                if decision.permit is not None:
                    if time.time() > deadline_epoch:
                        await decision.permit.release()
                        break
                    key = "first_accelerator" if pool is self.accelerator else "first_primary"
                    self._stats[key] += 1
                    if pool is standby_pool:
                        await self.coordinator.set_active_primary(pool.name)
                    return pool, decision.permit, is_probe
                if pool is first_pool and decision.circuit_open:
                    active_primary_circuit_open = True
                all_circuits = all_circuits and decision.circuit_open
                waits.append(decision.wait_s)

            if choices and all_circuits:
                raise OcrPoolError(
                    "all_first_attempt_pools_circuit_open",
                    retryable=True,
                    transport_failure=True,
                )
            # Soft capacity means "try the accelerator first", not "leave paid primary
            # capacity idle".  A later pass may use the primary hard window.
            use_primary_hard_capacity = True
            remaining = deadline_epoch - time.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(max(0.01, min(waits) if waits else 0.05), remaining, 0.25))
        raise OcrPoolError(
            "first_attempt_dispatch_deadline",
            # No provider request was sent. The caller still has the retry
            # window reserved above, so let the one-per-paper retry pool make
            # its attempt before this paper is left pending.
            retryable=True,
            transport_failure=False,
        )

    async def _acquire_retry(self, *, deadline_epoch: float) -> PoolPermit:
        return await self._wait_for_pool(
            self.retry_pool,
            capacity_limit=self.retry_pool.hard_in_flight,
            borrowed=False,
            use_global_gate=False,
            deadline_epoch=deadline_epoch,
        )

    async def _wait_for_pool(
        self,
        pool: PoolSpec,
        *,
        capacity_limit: int,
        borrowed: bool,
        use_global_gate: bool,
        deadline_epoch: float,
        teacher_hash: str = "",
        teacher_limit: int = 0,
    ) -> PoolPermit:
        while time.time() <= deadline_epoch:
            try:
                decision = await self.coordinator.try_acquire(
                    pool,
                    capacity_limit=capacity_limit,
                    borrowed=borrowed,
                    use_global_gate=use_global_gate,
                    teacher_hash=teacher_hash,
                    teacher_limit=teacher_limit,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Redis/coordinator failures happen before provider dispatch;
                # classify them explicitly so a provider route is not poisoned
                # by an infrastructure admission outage.
                raise OcrPoolError(
                    f"pool_admission_error:{pool.name}",
                    retryable=False,
                    transport_failure=False,
                    request_sent=False,
                    failure_category="admission",
                    actual_model=pool.model,
                ) from exc
            if decision.permit is not None:
                if time.time() > deadline_epoch:
                    await decision.permit.release()
                    break
                return decision.permit
            if decision.circuit_open:
                raise OcrPoolError(
                    f"pool_circuit_open:{pool.name}",
                    retryable=False,
                    # A circuit decision is local admission state.  It does
                    # not prove that a new provider request was attempted.
                    transport_failure=False,
                    request_sent=False,
                    failure_category="admission",
                    actual_model=pool.model,
                )
            remaining = deadline_epoch - time.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(max(0.01, decision.wait_s), remaining, 0.25))
        raise OcrPoolError(
            f"pool_dispatch_deadline:{pool.name}",
            retryable=False,
            transport_failure=False,
            request_sent=False,
            failure_category="admission",
            actual_model=pool.model,
        )

    async def _execute(
        self,
        pool: PoolSpec,
        payload: dict[str, Any],
        validator: ResponseValidator | None,
    ) -> str:
        request_id = f"ocr-{uuid.uuid4().hex}"
        try:
            content = await self._transport(pool, payload, request_id)
        except OcrPoolError as exc:
            if exc.request_sent is None:
                # Reaching the transport callable means the permit/dispatch
                # boundary was crossed. Explicit pre-dispatch errors (such as
                # malformed local payload conversion) set ``False`` at source.
                exc.request_sent = True
            if exc.failure_category is None:
                exc.failure_category = (
                    "transport" if exc.transport_failure else "provider"
                )
            if exc.actual_model is None:
                exc.actual_model = pool.model
            if exc.transport_failure:
                fatal = exc.status_code in {401, 403, 404}
                # A 429 is an explicit capacity signal.  Release immediately and
                # cool the pool instead of occupying a slot while sleeping.
                threshold = 1 if exc.status_code == 429 else self.circuit_failure_threshold
                cooldown_s = max(
                    self.circuit_cooldown_s,
                    exc.retry_after_s or 0.0,
                )
                await self.coordinator.record_failure(
                    pool.coordination_key,
                    threshold=threshold,
                    cooldown_s=cooldown_s,
                    fatal=fatal,
                )
            raise
        except (httpx.TimeoutException, httpx.TransportError, TimeoutError) as exc:
            await self.coordinator.record_failure(
                pool.coordination_key,
                threshold=self.circuit_failure_threshold,
                cooldown_s=self.circuit_cooldown_s,
                fatal=False,
            )
            raise OcrPoolError(
                type(exc).__name__,
                retryable=True,
                transport_failure=True,
                request_sent=True,
                failure_category="transport",
                actual_model=pool.model,
            ) from exc

        if validator is not None:
            try:
                result = validator(content)
                if inspect.isawaitable(result):
                    await result
            except MaxTemplateRejection:
                # An explicit model refusal is a semantic stop, not a pool
                # failure. Preserve the reason and do not retry another answer.
                raise
            except Exception as exc:
                await self.coordinator.record_failure(
                    pool.coordination_key,
                    threshold=self.circuit_failure_threshold,
                    cooldown_s=self.circuit_cooldown_s,
                    fatal=False,
                )
                raise OcrPoolError(
                    "strict_contract_failure",
                    retryable=True,
                    transport_failure=False,
                    request_sent=True,
                    failure_category=_template_failure_category_from_message(exc),
                    actual_model=pool.model,
                ) from exc
        await self.coordinator.record_success(pool.coordination_key)
        logger.info("OCR pool request completed pool=%s request_id=%s", pool.name, request_id)
        return content

    async def _send_chat_completion_stream(
        self,
        pool: PoolSpec,
        payload: dict[str, Any],
        request_id: str,
    ) -> str:
        """Send the configured Chat Completions streaming request."""
        from config import OCR_TEMPLATE_GPT_REASONING_EFFORT, OCR_TRANSCRIPTION_REASONING_EFFORT

        requested_effort = str(payload.get("model_reasoning_effort") or "").strip().lower()
        if requested_effort not in {
            "none", "minimal", "low", "medium", "high", "xhigh", "max"
        }:
            requested_effort = (
                OCR_TRANSCRIPTION_REASONING_EFFORT
                if _is_student_transcription_payload(payload)
                else OCR_TEMPLATE_GPT_REASONING_EFFORT
            )
        body: dict[str, Any] = {
            "model": pool.model,
            "messages": _chat_messages_from_payload(payload),
            "temperature": 0,
            "stream": True,
        }
        max_tokens = payload.get("max_output_tokens", payload.get("max_tokens"))
        if pool.model.strip().lower().startswith("qwen"):
            # Provider parameters depend on the model contract, never on words
            # in a prompt. All new template phases use this same adapter.
            body["enable_thinking"] = requested_effort != "none"
            if payload.get("text") == {"format": {"type": "json_object"}}:
                # The internal Responses text.format is consumed here, at the
                # real Qwen wire boundary, and never forwarded as-is.
                body["response_format"] = {"type": "json_object"}
            if pool.model.strip().lower().startswith("qwen3.8"):
                # Keep the configured effort visible at the provider boundary.
                # In particular, the production template pipeline requests
                # ``high`` and must not silently become ``xhigh`` (which has a
                # materially longer latency/timeout profile).  Only the
                # provider's documented alias for minimal is normalized.
                body["reasoning_effort"] = {
                    "minimal": "low",
                }.get(requested_effort, requested_effort)
        else:
            # Preserve the separately verified legacy non-Qwen Chat gateway.
            # GPT Responses calls use the distinct adapter below.
            body["model_reasoning_effort"] = requested_effort
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        url = pool.base_url.rstrip("/")
        if url.endswith("/responses"):
            url = url[: -len("/responses")]
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        headers = {
            "Authorization": f"Bearer {pool.api_key}",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "X-Request-ID": request_id,
        }
        timeout = httpx.Timeout(
            connect=self.connect_timeout_s,
            read=max(pool.first_event_timeout_s, self.sse_idle_timeout_s),
            write=self.write_timeout_s,
            pool=self.connect_timeout_s,
        )
        response: httpx.Response | None = None
        started = time.monotonic()
        try:
            async with asyncio.timeout(pool.attempt_ttl_s):
                async with httpx.AsyncClient(
                    timeout=timeout,
                    trust_env=False,
                    http2=False,
                ) as client:
                    request = client.build_request("POST", url, headers=headers, json=body)
                    try:
                        response = await asyncio.wait_for(
                            client.send(request, stream=True),
                            timeout=pool.first_event_timeout_s,
                        )
                    except asyncio.TimeoutError as exc:
                        raise OcrPoolError(
                            "response_header_timeout",
                            retryable=True,
                            transport_failure=True,
                        ) from exc
                    if response.status_code != 200:
                        status = response.status_code
                        retryable, shared_failure = _classify_http_failure(status)
                        raise OcrPoolError(
                            f"http_{status}",
                            retryable=retryable,
                            transport_failure=shared_failure,
                            status_code=status,
                            retry_after_s=parse_retry_after_seconds(
                                response.headers.get("retry-after")
                            ),
                        )

                    iterator = response.aiter_lines()
                    chunks: list[str] = []
                    data_seen = False
                    ended: set[str] = set()
                    done_seen = False
                    while True:
                        timeout_s = (
                            max(
                                0.01,
                                started
                                + pool.first_event_timeout_s
                                - time.monotonic(),
                            )
                            if not data_seen
                            else self.sse_idle_timeout_s
                        )
                        try:
                            line = await asyncio.wait_for(anext(iterator), timeout=timeout_s)
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError as exc:
                            raise OcrPoolError(
                                "sse_idle_timeout" if data_seen else "first_sse_timeout",
                                retryable=True,
                                transport_failure=True,
                            ) from exc
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data:
                            continue
                        data_seen = True
                        if data == "[DONE]":
                            done_seen = True
                            break
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError as exc:
                            raise OcrPoolError(
                                "invalid_sse_json",
                                retryable=True,
                                transport_failure=True,
                            ) from exc
                        if not isinstance(event, dict):
                            continue
                        if event.get("error"):
                            raise OcrPoolError(
                                "stream_error",
                                retryable=True,
                                transport_failure=True,
                            )
                        for choice in event.get("choices") or []:
                            if not isinstance(choice, dict):
                                continue
                            delta = choice.get("delta") or {}
                            if isinstance(delta, dict) and delta.get("refusal"):
                                raise OcrPoolError("model_refused", retryable=False)
                            if isinstance(delta, dict) and isinstance(
                                delta.get("content"), str
                            ):
                                chunks.append(delta["content"])
                            finish_reason = choice.get("finish_reason")
                            if isinstance(finish_reason, str) and finish_reason.strip():
                                ended.add(finish_reason.strip())

                        if ended:
                            # finish_reason is the terminator: never keep the
                            # connection open until idle timeout or [DONE], and
                            # never accept a stream the provider cut short.
                            break

                    if ended & _TRUNCATED_FINISH_REASONS:
                        raise OcrPoolError(
                            "truncated_stream", retryable=True
                        )
                    if "content_filter" in ended:
                        raise OcrPoolError("content_filtered", retryable=False)
                    if ended - {"stop"}:
                        raise OcrPoolError("unsupported_finish_reason", retryable=True)
                    if not data_seen:
                        raise OcrPoolError(
                            "missing_sse", retryable=True, transport_failure=True
                        )
                    if not ended and not done_seen:
                        raise OcrPoolError(
                            "incomplete_stream", retryable=True, transport_failure=True
                        )
                    content = "".join(chunks).strip()
                    if not content:
                        raise OcrPoolError(
                            "empty_content", retryable=True, transport_failure=True
                        )
                    return content
        except TimeoutError as exc:
            raise OcrPoolError(
                "absolute_attempt_ttl",
                retryable=True,
                transport_failure=True,
            ) from exc
        finally:
            if response is not None:
                await response.aclose()

    async def _send_stream(
        self,
        pool: PoolSpec,
        payload: dict[str, Any],
        request_id: str,
    ) -> str:
        if pool.api_protocol == "chat_completions":
            return await self._send_chat_completion_stream(pool, payload, request_id)

        body = dict(payload)
        body["model"] = pool.model
        requested_effort = str(payload.get("model_reasoning_effort") or "").strip().lower()
        if requested_effort in {
            "none", "minimal", "low", "medium", "high", "xhigh", "max"
        }:
            body["reasoning"] = {"effort": requested_effort}
        elif _is_student_transcription_payload(payload):
            from config import OCR_TRANSCRIPTION_REASONING_EFFORT

            body["reasoning"] = {
                "effort": OCR_TRANSCRIPTION_REASONING_EFFORT,
            }
        else:
            # Legacy Responses callers without internal route metadata retain
            # the historical high-effort default. Active template calls set
            # ``model_reasoning_effort`` before reaching this adapter.
            body["reasoning"] = {"effort": "high"}
        body["stream"] = True
        body.pop("reasoning_effort", None)
        body.pop("model_reasoning_effort", None)
        body.pop("temperature", None)
        body.pop("max_completion_tokens", None)
        body.pop("messages", None)
        body.pop("stream_options", None)
        body.pop("enable_thinking", None)
        # The internal Responses json_object marker is forwarded only to Qwen
        # pools, where text.format is already the native field of this protocol.
        # Every non-Qwen Responses gateway keeps its byte-identical legacy body.
        if body.get("text") == {"format": {"type": "json_object"}} and not pool.model.strip().lower().startswith("qwen"):
            body.pop("text")

        url = pool.base_url.rstrip("/")
        if url.endswith("/chat/completions"):
            url = url[: -len("/chat/completions")]
        if not url.endswith("/responses"):
            url += "/responses"
        headers = {
            "Authorization": f"Bearer {pool.api_key}",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "X-Request-ID": request_id,
        }
        timeout = httpx.Timeout(
            connect=self.connect_timeout_s,
            # A provider can remain silent while reasoning before its first SSE
            # event. The stricter idle timeout is enforced below after data
            # starts flowing.
            read=max(pool.first_event_timeout_s, self.sse_idle_timeout_s),
            write=self.write_timeout_s,
            pool=self.connect_timeout_s,
        )
        response: httpx.Response | None = None
        started = time.monotonic()
        try:
            async with asyncio.timeout(pool.attempt_ttl_s):
                async with httpx.AsyncClient(
                    timeout=timeout,
                    trust_env=False,
                    http2=False,
                ) as client:
                    request = client.build_request("POST", url, headers=headers, json=body)
                    try:
                        response = await asyncio.wait_for(
                            client.send(request, stream=True),
                            timeout=pool.first_event_timeout_s,
                        )
                    except asyncio.TimeoutError as exc:
                        raise OcrPoolError(
                            "response_header_timeout",
                            retryable=True,
                            transport_failure=True,
                        ) from exc
                    if response.status_code != 200:
                        status = response.status_code
                        retryable, shared_failure = _classify_http_failure(status)
                        raise OcrPoolError(
                            f"http_{status}",
                            retryable=retryable,
                            transport_failure=shared_failure,
                            status_code=status,
                            retry_after_s=parse_retry_after_seconds(
                                response.headers.get("retry-after")
                            ),
                        )

                    iterator = response.aiter_lines()
                    chunks: list[str] = []
                    completed_seen = False
                    data_seen = False
                    while True:
                        if not data_seen:
                            remaining_first = (
                                started + pool.first_event_timeout_s - time.monotonic()
                            )
                            if remaining_first <= 0:
                                raise OcrPoolError(
                                    "first_sse_timeout",
                                    retryable=True,
                                    transport_failure=True,
                                )
                            timeout_s = remaining_first
                        else:
                            timeout_s = self.sse_idle_timeout_s
                        try:
                            line = await asyncio.wait_for(anext(iterator), timeout=timeout_s)
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError as exc:
                            raise OcrPoolError(
                                "sse_idle_timeout" if data_seen else "first_sse_timeout",
                                retryable=True,
                                transport_failure=True,
                            ) from exc
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data:
                            continue
                        data_seen = True
                        if data == "[DONE]":
                            break
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError as exc:
                            raise OcrPoolError(
                                "invalid_sse_json",
                                retryable=True,
                                transport_failure=True,
                            ) from exc
                        event_type = str(event.get("type") or "")
                        if event_type == "error" or event.get("error"):
                            raise OcrPoolError(
                                "stream_error",
                                retryable=True,
                                transport_failure=True,
                            )
                        if event_type in {"response.failed", "response.incomplete"}:
                            raise OcrPoolError(
                                event_type.replace(".", "_"),
                                retryable=True,
                                transport_failure=False,
                            )
                        if event_type == "response.output_text.delta":
                            delta = event.get("delta")
                            if isinstance(delta, str):
                                chunks.append(delta)
                            continue
                        if event_type != "response.completed":
                            continue

                        completed_response = event.get("response")
                        if not isinstance(completed_response, dict):
                            completed_response = {}
                        terminal_status = str(
                            completed_response.get("status") or ""
                        ).strip()
                        if terminal_status and terminal_status != "completed":
                            raise OcrPoolError(
                                f"response_terminal_{terminal_status}",
                                retryable=True,
                                transport_failure=False,
                            )
                        completed_seen = True
                        if not chunks:
                            for output_item in completed_response.get("output") or []:
                                if not isinstance(output_item, dict):
                                    continue
                                for part in output_item.get("content") or []:
                                    if (
                                        isinstance(part, dict)
                                        and part.get("type") == "output_text"
                                        and isinstance(part.get("text"), str)
                                    ):
                                        chunks.append(part["text"])
                        break
                    if not data_seen:
                        raise OcrPoolError(
                            "missing_sse", retryable=True, transport_failure=True
                        )
                    if not completed_seen:
                        raise OcrPoolError(
                            "missing_completed", retryable=True, transport_failure=True
                        )
                    content = "".join(chunks).strip()
                    if not content:
                        raise OcrPoolError(
                            "empty_content", retryable=True, transport_failure=True
                        )
                    return content
        except TimeoutError as exc:
            raise OcrPoolError(
                "absolute_attempt_ttl",
                retryable=True,
                transport_failure=True,
            ) from exc
        finally:
            if response is not None:
                await response.aclose()

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
