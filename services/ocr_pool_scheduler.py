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


class _TokenBucket:
    def __init__(self, rpm: float, burst: int) -> None:
        self.rpm = rpm
        self.burst = float(burst)
        self.tokens = float(burst)
        self.updated = time.monotonic()

    def preview(self, now: float) -> tuple[bool, float, float]:
        if self.rpm <= 0:
            # Zero is an explicit "provider RPM unknown" setting. Capacity and
            # request deadlines still gate the route; no invented quota is
            # applied until an operator supplies a measured/local cap.
            return True, 0.0, self.burst
        rate = self.rpm / 60.0
        tokens = min(self.burst, self.tokens + max(0.0, now - self.updated) * rate)
        if tokens >= 1.0:
            return True, 0.0, tokens
        return False, (1.0 - tokens) / rate, tokens

    def update(self, now: float, tokens: float, *, consume: bool) -> None:
        self.tokens = tokens - 1.0 if consume else tokens
        self.updated = now


class LocalPoolCoordinator:
    """Deterministic process-local coordinator used by tests and explicit dev mode."""

    def __init__(self, *, global_rpm: float, global_burst: int) -> None:
        self._lock = asyncio.Lock()
        self._global_bucket = _TokenBucket(global_rpm, global_burst)
        self._pool_buckets: dict[str, _TokenBucket] = {}
        self._active: dict[str, int] = {}
        self._borrowed: dict[str, int] = {}
        self._teacher_active: dict[tuple[str, str], int] = {}
        self._global_first_active = 0
        self._global_first_teacher_active: dict[str, int] = {}
        self._global_first_waiters: dict[tuple[str, str], float] = {}
        self._circuits: dict[str, float] = {}
        self._consecutive_failures: dict[str, int] = {}
        self._batch_started: dict[str, float] = {}
        self._batch_expires_at: dict[str, float] = {}
        self._batch_retries: dict[str, int] = {}
        self._batch_retry_expires_at: dict[str, float] = {}
        self._paper_retries: set[tuple[str, str]] = set()
        self._paper_retry_expires_at: dict[tuple[str, str], float] = {}
        self._accelerator_gate: dict[str, str] = {}
        self._accelerator_gate_expires_at: dict[str, float] = {}
        self._priority_waiters: dict[str, set[str]] = {}
        self._priority_waiter_expires_at: dict[tuple[str, str], float] = {}
        self._active_primary: tuple[str, float] | None = None
        self._rate_budget_entries: dict[str, list[tuple[float, int]]] = {}

    @staticmethod
    def _validated_ttl(ttl_s: float) -> float:
        ttl = float(ttl_s)
        if not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("local OCR coordinator TTL must be finite and positive")
        return ttl

    def _prune_expired_state(self, now: float) -> None:
        expired_batches = [
            key for key, expires_at in self._batch_expires_at.items()
            if expires_at <= now
        ]
        for key in expired_batches:
            self._batch_expires_at.pop(key, None)
            self._batch_started.pop(key, None)

        expired_retry_batches = [
            key for key, expires_at in self._batch_retry_expires_at.items()
            if expires_at <= now
        ]
        for key in expired_retry_batches:
            self._batch_retry_expires_at.pop(key, None)
            self._batch_retries.pop(key, None)

        expired_papers = [
            key for key, expires_at in self._paper_retry_expires_at.items()
            if expires_at <= now
        ]
        for key in expired_papers:
            self._paper_retry_expires_at.pop(key, None)
            self._paper_retries.discard(key)

        expired_gates = [
            key for key, expires_at in self._accelerator_gate_expires_at.items()
            if expires_at <= now
        ]
        for key in expired_gates:
            self._accelerator_gate_expires_at.pop(key, None)
            self._accelerator_gate.pop(key, None)

        expired_waiters = [
            key for key, expires_at in self._priority_waiter_expires_at.items()
            if expires_at <= now
        ]
        for pool_key, waiter_id in expired_waiters:
            self._priority_waiter_expires_at.pop((pool_key, waiter_id), None)
            waiters = self._priority_waiters.get(pool_key)
            if waiters is None:
                continue
            waiters.discard(waiter_id)
            if not waiters:
                self._priority_waiters.pop(pool_key, None)

        expired_global_waiters = [
            key for key, expires_at in self._global_first_waiters.items()
            if expires_at <= now
        ]
        for key in expired_global_waiters:
            self._global_first_waiters.pop(key, None)

    def _pool_bucket(self, pool: PoolSpec) -> _TokenBucket:
        pool_key = pool.coordination_key
        bucket = self._pool_buckets.get(pool_key)
        if bucket is None:
            bucket = _TokenBucket(pool.max_rpm, pool.burst)
            self._pool_buckets[pool_key] = bucket
        return bucket

    async def try_acquire(
        self,
        pool: PoolSpec,
        *,
        capacity_limit: int,
        borrowed: bool,
        use_global_gate: bool,
        teacher_hash: str = "",
        teacher_limit: int = 0,
        global_in_flight_limit: int = 0,
        global_teacher_limit: int = 0,
        waiter_id: str = "",
        waiter_ttl_s: float = 15.0,
    ) -> AcquireDecision:
        async with self._lock:
            now = time.monotonic()
            self._prune_expired_state(now)
            global_first_attempt = use_global_gate and global_in_flight_limit > 0
            if global_first_attempt and teacher_hash and waiter_id:
                self._global_first_waiters[(teacher_hash, waiter_id)] = (
                    now + max(1.0, float(waiter_ttl_s))
                )
            pool_key = pool.coordination_key
            if self._circuits.get(pool_key, 0.0) > now:
                return AcquireDecision(wait_s=0.1, circuit_open=True)
            if borrowed and self._priority_waiters.get(pool_key):
                return AcquireDecision(wait_s=0.05)
            if self._active.get(pool_key, 0) >= capacity_limit:
                return AcquireDecision(wait_s=0.05)
            if teacher_hash and teacher_limit > 0:
                teacher_key = (pool_key, teacher_hash)
                if self._teacher_active.get(teacher_key, 0) >= teacher_limit:
                    return AcquireDecision(wait_s=0.05)
            if global_first_attempt:
                if self._global_first_active >= global_in_flight_limit:
                    return AcquireDecision(wait_s=0.05)
                teacher_active = self._global_first_teacher_active.get(teacher_hash, 0)
                if teacher_hash and global_teacher_limit > 0:
                    if teacher_active >= global_teacher_limit:
                        return AcquireDecision(wait_s=0.05)
                if teacher_hash and waiter_id and self._global_first_waiters:
                    least_waiting_active = min(
                        self._global_first_teacher_active.get(waiting_teacher, 0)
                        for waiting_teacher, _ in self._global_first_waiters
                    )
                    if teacher_active > least_waiting_active:
                        return AcquireDecision(wait_s=0.05)
            if borrowed and pool.borrowed_in_flight is not None:
                if self._borrowed.get(pool_key, 0) >= pool.borrowed_in_flight:
                    return AcquireDecision(wait_s=0.05)

            pool_bucket = self._pool_bucket(pool)
            pool_ok, pool_wait, pool_tokens = pool_bucket.preview(now)
            global_ok, global_wait, global_tokens = True, 0.0, 0.0
            if use_global_gate:
                global_ok, global_wait, global_tokens = self._global_bucket.preview(now)
            if not pool_ok or not global_ok:
                pool_bucket.update(now, pool_tokens, consume=False)
                if use_global_gate:
                    self._global_bucket.update(now, global_tokens, consume=False)
                waits = [value for value in (pool_wait, global_wait) if value > 0]
                return AcquireDecision(wait_s=min(waits) if waits else 0.05)

            pool_bucket.update(now, pool_tokens, consume=True)
            if use_global_gate:
                self._global_bucket.update(now, global_tokens, consume=True)
            self._active[pool_key] = self._active.get(pool_key, 0) + 1
            if borrowed:
                self._borrowed[pool_key] = self._borrowed.get(pool_key, 0) + 1
            if teacher_hash:
                teacher_key = (pool_key, teacher_hash)
                self._teacher_active[teacher_key] = self._teacher_active.get(teacher_key, 0) + 1
            if global_first_attempt:
                self._global_first_active += 1
                if teacher_hash:
                    self._global_first_teacher_active[teacher_hash] = (
                        self._global_first_teacher_active.get(teacher_hash, 0) + 1
                    )
                    if waiter_id:
                        self._global_first_waiters.pop((teacher_hash, waiter_id), None)
            return AcquireDecision(
                permit=PoolPermit(
                    self,
                    pool_name=pool_key,
                    lease_id=uuid.uuid4().hex,
                    borrowed=borrowed,
                    teacher_hash=teacher_hash,
                    global_first_attempt=global_first_attempt,
                ),
                wait_s=0.0,
            )

    async def release(self, permit: PoolPermit) -> None:
        async with self._lock:
            self._active[permit.pool_name] = max(
                0, self._active.get(permit.pool_name, 0) - 1
            )
            if permit.borrowed:
                self._borrowed[permit.pool_name] = max(
                    0, self._borrowed.get(permit.pool_name, 0) - 1
                )
            if permit.teacher_hash:
                teacher_key = (permit.pool_name, permit.teacher_hash)
                self._teacher_active[teacher_key] = max(
                    0, self._teacher_active.get(teacher_key, 0) - 1
                )
            if permit.global_first_attempt:
                self._global_first_active = max(0, self._global_first_active - 1)
                if permit.teacher_hash:
                    self._global_first_teacher_active[permit.teacher_hash] = max(
                        0,
                        self._global_first_teacher_active.get(permit.teacher_hash, 0) - 1,
                    )

    async def acquire_rate_budget(
        self,
        *,
        provider: str,
        requests_per_minute: float,
        tokens_per_minute: int,
        reserved_tokens: int,
        timeout_seconds: float,
    ) -> None:
        """Apply a small sliding-window budget for explicit local/test mode.

        Production workers use ``RedisModelLimiter`` below. Keeping the same
        contract locally makes fallback tests deterministic and avoids opening
        a real Redis connection merely to exercise a transport stub.
        """
        from services.model_concurrency import RateBudgetTimeout

        rpm = float(requests_per_minute or 0)
        tpm = int(tokens_per_minute or 0)
        reserved = max(1, int(reserved_tokens or 0))
        if rpm < 0 or tpm < 0:
            raise ValueError("rate budget limits must be nonnegative")
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        key = str(provider or "local")
        while True:
            async with self._lock:
                now = time.monotonic()
                entries = [
                    (timestamp, tokens)
                    for timestamp, tokens in self._rate_budget_entries.get(key, [])
                    if timestamp > now - 60.0
                ]
                used_tokens = sum(tokens for _timestamp, tokens in entries)
                request_ok = rpm <= 0 or len(entries) + 1 <= rpm
                token_ok = tpm <= 0 or used_tokens + reserved <= tpm
                if request_ok and token_ok:
                    entries.append((now, reserved))
                    self._rate_budget_entries[key] = entries
                    return
                wait_candidates = [timestamp + 60.0 - now for timestamp, _ in entries]
                wait_seconds = min((value for value in wait_candidates if value > 0), default=0.05)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RateBudgetTimeout("timed out waiting for provider rate budget")
            await asyncio.sleep(min(max(0.01, wait_seconds), remaining))

    async def remove_first_attempt_waiter(
        self,
        teacher_hash: str,
        waiter_id: str,
    ) -> None:
        if not teacher_hash or not waiter_id:
            return
        async with self._lock:
            self._global_first_waiters.pop((teacher_hash, waiter_id), None)

    async def get_batch_start(self, batch_hash: str, ttl_s: float) -> float:
        async with self._lock:
            ttl = self._validated_ttl(ttl_s)
            monotonic_now = time.monotonic()
            self._prune_expired_state(monotonic_now)
            started = self._batch_started.get(batch_hash)
            if started is None:
                now = time.time()
                self._batch_started[batch_hash] = now
                self._batch_expires_at[batch_hash] = monotonic_now + ttl
                self._batch_retries.pop(batch_hash, None)
                self._batch_retry_expires_at.pop(batch_hash, None)
                self._paper_retries = {
                    item for item in self._paper_retries if item[0] != batch_hash
                }
                self._paper_retry_expires_at = {
                    item: expires_at
                    for item, expires_at in self._paper_retry_expires_at.items()
                    if item[0] != batch_hash
                }
                self._accelerator_gate.pop(batch_hash, None)
                self._accelerator_gate_expires_at.pop(batch_hash, None)
                return now
            return started

    async def claim_retry(
        self,
        batch_hash: str,
        paper_hash: str,
        maximum: int,
        ttl_s: float,
    ) -> bool:
        async with self._lock:
            ttl = self._validated_ttl(ttl_s)
            now = time.monotonic()
            self._prune_expired_state(now)
            paper_key = (batch_hash, paper_hash)
            if paper_key in self._paper_retries:
                return False
            current = self._batch_retries.get(batch_hash, 0)
            if current >= maximum:
                return False
            self._batch_retries[batch_hash] = current + 1
            self._batch_retry_expires_at[batch_hash] = now + ttl
            self._paper_retries.add(paper_key)
            self._paper_retry_expires_at[paper_key] = now + ttl
            return True

    async def release_retry_claim(
        self,
        batch_hash: str,
        paper_hash: str,
    ) -> bool:
        """Roll back a retry reservation that never reached provider dispatch."""
        async with self._lock:
            self._prune_expired_state(time.monotonic())
            paper_key = (batch_hash, paper_hash)
            if paper_key not in self._paper_retries:
                return False
            self._paper_retries.remove(paper_key)
            self._paper_retry_expires_at.pop(paper_key, None)
            remaining = max(0, self._batch_retries.get(batch_hash, 0) - 1)
            if remaining:
                self._batch_retries[batch_hash] = remaining
            else:
                self._batch_retries.pop(batch_hash, None)
                self._batch_retry_expires_at.pop(batch_hash, None)
            return True

    async def record_success(self, pool_name: str) -> None:
        async with self._lock:
            self._consecutive_failures[pool_name] = 0

    async def record_failure(
        self,
        pool_name: str,
        *,
        threshold: int,
        cooldown_s: float,
        fatal: bool,
    ) -> None:
        async with self._lock:
            if fatal:
                self._circuits[pool_name] = math.inf
                return
            count = self._consecutive_failures.get(pool_name, 0) + 1
            self._consecutive_failures[pool_name] = count
            if count >= threshold:
                self._circuits[pool_name] = max(
                    self._circuits.get(pool_name, 0.0),
                    time.monotonic() + cooldown_s,
                )
                self._consecutive_failures[pool_name] = 0

    async def is_circuit_open(self, pool_name: str) -> bool:
        async with self._lock:
            return self._circuits.get(pool_name, 0.0) > time.monotonic()

    async def get_active_primary(self, default: str) -> str:
        async with self._lock:
            active = self._active_primary
            if active is None:
                return default
            name, expires_at = active
            if expires_at <= time.monotonic():
                self._active_primary = None
                return default
            return name

    async def set_active_primary(self, name: str) -> None:
        async with self._lock:
            self._active_primary = (
                name,
                time.monotonic() + _ACTIVE_PRIMARY_TTL_S,
            )

    async def accelerator_gate(self, batch_hash: str) -> str:
        async with self._lock:
            self._prune_expired_state(time.monotonic())
            return self._accelerator_gate.get(batch_hash, "unknown")

    async def claim_accelerator_probe(self, batch_hash: str) -> bool:
        async with self._lock:
            now = time.monotonic()
            self._prune_expired_state(now)
            if self._accelerator_gate.get(batch_hash, "unknown") != "unknown":
                return False
            self._accelerator_gate[batch_hash] = "probing"
            self._accelerator_gate_expires_at[batch_hash] = now + 180.0
            return True

    async def finish_accelerator_probe(self, batch_hash: str, healthy: bool) -> None:
        async with self._lock:
            self._accelerator_gate[batch_hash] = "healthy" if healthy else "blocked"
            self._accelerator_gate_expires_at[batch_hash] = time.monotonic() + 600.0

    async def abandon_accelerator_probe(self, batch_hash: str) -> None:
        async with self._lock:
            if self._accelerator_gate.get(batch_hash) == "probing":
                self._accelerator_gate.pop(batch_hash, None)
                self._accelerator_gate_expires_at.pop(batch_hash, None)

    async def add_priority_waiter(self, pool_name: str, waiter_id: str, ttl_s: float) -> None:
        async with self._lock:
            ttl = self._validated_ttl(ttl_s)
            now = time.monotonic()
            self._prune_expired_state(now)
            self._priority_waiters.setdefault(pool_name, set()).add(waiter_id)
            self._priority_waiter_expires_at[(pool_name, waiter_id)] = now + ttl

    async def remove_priority_waiter(self, pool_name: str, waiter_id: str) -> None:
        async with self._lock:
            self._priority_waiter_expires_at.pop((pool_name, waiter_id), None)
            waiters = self._priority_waiters.get(pool_name)
            if waiters is None:
                return
            waiters.discard(waiter_id)
            if not waiters:
                self._priority_waiters.pop(pool_name, None)

    async def close(self) -> None:
        return None


_ACQUIRE_LUA = r"""
local now = tonumber(ARGV[1])
local global_rpm = tonumber(ARGV[2])
local global_burst = tonumber(ARGV[3])
local pool_rpm = tonumber(ARGV[4])
local pool_burst = tonumber(ARGV[5])
local capacity = tonumber(ARGV[6])
local borrow_capacity = tonumber(ARGV[7])
local borrowed = tonumber(ARGV[8])
local lease_id = ARGV[9]
local lease_expiry = tonumber(ARGV[10])
local use_global = tonumber(ARGV[11])
local teacher_limit = tonumber(ARGV[12])
local global_in_flight_limit = tonumber(ARGV[13])
local global_teacher_limit = tonumber(ARGV[14])
local teacher_hash = ARGV[15]
local waiter_id = ARGV[16]
local waiter_expiry = tonumber(ARGV[17])
local global_teacher_prefix = ARGV[18]
local first_attempt_enabled = use_global == 1 and global_in_flight_limit > 0
local waiter_member = teacher_hash .. '\31' .. waiter_id

redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', now)
redis.call('ZREMRANGEBYSCORE', KEYS[4], '-inf', now)
redis.call('ZREMRANGEBYSCORE', KEYS[6], '-inf', now)
redis.call('ZREMRANGEBYSCORE', KEYS[7], '-inf', now)
if first_attempt_enabled then
  redis.call('ZREMRANGEBYSCORE', KEYS[8], '-inf', now)
  redis.call('ZREMRANGEBYSCORE', KEYS[9], '-inf', now)
  redis.call('ZREMRANGEBYSCORE', KEYS[10], '-inf', now)
  if teacher_hash ~= '' and waiter_id ~= '' then
    redis.call('ZADD', KEYS[10], waiter_expiry, waiter_member)
    redis.call('EXPIRE', KEYS[10], 600)
  end
end
if redis.call('EXISTS', KEYS[5]) == 1 then
  return {0, '0.1', 1}
end
if borrowed == 1 and redis.call('ZCARD', KEYS[6]) > 0 then
  return {0, '0.05', 0}
end
if redis.call('ZCARD', KEYS[3]) >= capacity then
  return {0, '0.05', 0}
end
if teacher_limit > 0 and redis.call('ZCARD', KEYS[7]) >= teacher_limit then
  return {0, '0.05', 0}
end
if first_attempt_enabled then
  if redis.call('ZCARD', KEYS[8]) >= global_in_flight_limit then
    return {0, '0.05', 0}
  end
  local teacher_active = 0
  if teacher_hash ~= '' then
    teacher_active = redis.call('ZCARD', KEYS[9])
    if global_teacher_limit > 0 and teacher_active >= global_teacher_limit then
      return {0, '0.05', 0}
    end
  end
  if teacher_hash ~= '' and waiter_id ~= '' then
    local least_waiting_active = nil
    local seen_teachers = {}
    local waiters = redis.call('ZRANGEBYSCORE', KEYS[10], now, '+inf')
    for _, waiter in ipairs(waiters) do
      local separator = string.find(waiter, '\31', 1, true)
      local waiting_teacher = separator and string.sub(waiter, 1, separator - 1) or waiter
      if seen_teachers[waiting_teacher] == nil then
        seen_teachers[waiting_teacher] = true
        local waiting_teacher_key = global_teacher_prefix .. waiting_teacher
        redis.call('ZREMRANGEBYSCORE', waiting_teacher_key, '-inf', now)
        local waiting_active = redis.call('ZCARD', waiting_teacher_key)
        if least_waiting_active == nil or waiting_active < least_waiting_active then
          least_waiting_active = waiting_active
        end
      end
    end
    if least_waiting_active ~= nil and teacher_active > least_waiting_active then
      return {0, '0.05', 0}
    end
  end
end
if borrowed == 1 and borrow_capacity > 0 and redis.call('ZCARD', KEYS[4]) >= borrow_capacity then
  return {0, '0.05', 0}
end

local function preview_bucket(key, rpm, burst)
  if rpm <= 0 then
    return burst, 0
  end
  local values = redis.call('HMGET', key, 'tokens', 'updated')
  local tokens = tonumber(values[1])
  local updated = tonumber(values[2])
  if tokens == nil then tokens = burst end
  if updated == nil then updated = now end
  local rate = rpm / 60.0
  tokens = math.min(burst, tokens + math.max(0, now - updated) * rate)
  local wait = 0
  if tokens < 1 then wait = (1 - tokens) / rate end
  return tokens, wait
end

local pool_tokens, pool_wait = preview_bucket(KEYS[2], pool_rpm, pool_burst)
local global_tokens = 1
local global_wait = 0
if use_global == 1 then
  global_tokens, global_wait = preview_bucket(KEYS[1], global_rpm, global_burst)
end
if pool_tokens < 1 or global_tokens < 1 then
  redis.call('HMSET', KEYS[2], 'tokens', pool_tokens, 'updated', now)
  redis.call('EXPIRE', KEYS[2], 600)
  if use_global == 1 then
    redis.call('HMSET', KEYS[1], 'tokens', global_tokens, 'updated', now)
    redis.call('EXPIRE', KEYS[1], 600)
  end
  local wait = pool_wait
  if wait <= 0 or (global_wait > 0 and global_wait < wait) then wait = global_wait end
  if wait <= 0 then wait = 0.05 end
  return {0, tostring(wait), 0}
end

redis.call('HMSET', KEYS[2], 'tokens', pool_tokens - 1, 'updated', now)
redis.call('EXPIRE', KEYS[2], 600)
if use_global == 1 then
  redis.call('HMSET', KEYS[1], 'tokens', global_tokens - 1, 'updated', now)
  redis.call('EXPIRE', KEYS[1], 600)
end
redis.call('ZADD', KEYS[3], lease_expiry, lease_id)
redis.call('EXPIRE', KEYS[3], 600)
if teacher_limit > 0 then
  redis.call('ZADD', KEYS[7], lease_expiry, lease_id)
  redis.call('EXPIRE', KEYS[7], 600)
end
if first_attempt_enabled then
  redis.call('ZADD', KEYS[8], lease_expiry, lease_id)
  redis.call('EXPIRE', KEYS[8], 600)
  if teacher_hash ~= '' then
    redis.call('ZADD', KEYS[9], lease_expiry, lease_id)
    redis.call('EXPIRE', KEYS[9], 600)
    if waiter_id ~= '' then
      redis.call('ZREM', KEYS[10], waiter_member)
    end
  end
end
if borrowed == 1 then
  redis.call('ZADD', KEYS[4], lease_expiry, lease_id)
  redis.call('EXPIRE', KEYS[4], 600)
end
return {1, '0', 0}
"""

_RELEASE_LUA = r"""
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('ZREM', KEYS[3], ARGV[1])
if tonumber(ARGV[2]) == 1 then
  redis.call('ZREM', KEYS[4], ARGV[1])
  redis.call('ZREM', KEYS[5], ARGV[1])
end
return 1
"""

_REMOVE_FIRST_ATTEMPT_WAITER_LUA = r"""
return redis.call('ZREM', KEYS[1], ARGV[1])
"""

_CLAIM_RETRY_LUA = r"""
if redis.call('EXISTS', KEYS[2]) == 1 then return 0 end
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local maximum = tonumber(ARGV[1])
if current >= maximum then return 0 end
redis.call('SET', KEYS[1], current + 1, 'EX', tonumber(ARGV[2]))
redis.call('SET', KEYS[2], '1', 'EX', tonumber(ARGV[2]))
return 1
"""

_RELEASE_RETRY_CLAIM_LUA = r"""
if redis.call('DEL', KEYS[2]) == 0 then return 0 end
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current <= 1 then
  redis.call('DEL', KEYS[1])
else
  redis.call('DECR', KEYS[1])
end
return 1
"""

_RECORD_FAILURE_LUA = r"""
if tonumber(ARGV[1]) == 1 then
  redis.call('SET', KEYS[1], 'fatal')
  return 1
end
if redis.call('GET', KEYS[1]) == 'fatal' then return -1 end
local count = redis.call('INCR', KEYS[2])
local requested_ttl_ms = math.ceil(tonumber(ARGV[3]) * 1000)
local failure_ttl_ms = redis.call('PTTL', KEYS[2])
if failure_ttl_ms < requested_ttl_ms then
  redis.call('PEXPIRE', KEYS[2], requested_ttl_ms)
end
if count >= tonumber(ARGV[2]) then
  local circuit_ttl_ms = redis.call('PTTL', KEYS[1])
  if circuit_ttl_ms < requested_ttl_ms then
    redis.call('SET', KEYS[1], 'open', 'PX', requested_ttl_ms)
  end
  redis.call('DEL', KEYS[2])
end
return count
"""

_CLAIM_PROBE_LUA = r"""
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
redis.call('SET', KEYS[1], 'probing', 'EX', tonumber(ARGV[1]))
return 1
"""

_ABANDON_PROBE_LUA = r"""
if redis.call('GET', KEYS[1]) ~= 'probing' then return 0 end
redis.call('DEL', KEYS[1])
return 1
"""


class RedisPoolCoordinator:
    """Cross-worker token, capacity, circuit, and retry coordination."""

    def __init__(
        self,
        redis_client,
        *,
        namespace: str,
        global_rpm: float,
        global_burst: int,
        operation_timeout_s: float,
    ) -> None:
        self._redis = redis_client
        self._namespace = namespace.rstrip(":")
        self._global_rpm = global_rpm
        self._global_burst = global_burst
        self._operation_timeout_s = operation_timeout_s

    async def _eval(self, script: str, numkeys: int, *args):
        return await asyncio.wait_for(
            self._redis.eval(script, numkeys, *args),
            timeout=self._operation_timeout_s,
        )

    def _pool_base(self, pool_name: str) -> str:
        digest = hashlib.sha256(pool_name.encode("utf-8")).hexdigest()[:16]
        return f"{self._namespace}:pool:{digest}"

    def _first_attempt_base(self) -> str:
        return f"{self._namespace}:gate:first:inflight"

    async def try_acquire(
        self,
        pool: PoolSpec,
        *,
        capacity_limit: int,
        borrowed: bool,
        use_global_gate: bool,
        teacher_hash: str = "",
        teacher_limit: int = 0,
        global_in_flight_limit: int = 0,
        global_teacher_limit: int = 0,
        waiter_id: str = "",
        waiter_ttl_s: float = 15.0,
    ) -> AcquireDecision:
        now = time.time()
        lease_id = uuid.uuid4().hex
        base = self._pool_base(pool.coordination_key)
        first_attempt_base = self._first_attempt_base()
        global_first_attempt = use_global_gate and global_in_flight_limit > 0
        normalized_waiter_id = str(waiter_id or "")
        waiter_expiry = now + max(1.0, float(waiter_ttl_s))
        result = await self._eval(
            _ACQUIRE_LUA,
            10,
            f"{self._namespace}:gate:first",
            f"{base}:gate",
            f"{base}:leases",
            f"{base}:borrowed",
            f"{base}:circuit",
            f"{base}:priority",
            f"{base}:teacher:{teacher_hash or 'anonymous'}",
            f"{first_attempt_base}:leases",
            f"{first_attempt_base}:teacher:{teacher_hash or 'anonymous'}",
            f"{first_attempt_base}:waiters",
            now,
            self._global_rpm,
            self._global_burst,
            pool.max_rpm,
            pool.burst,
            capacity_limit,
            pool.borrowed_in_flight or 0,
            1 if borrowed else 0,
            lease_id,
            now + pool.attempt_ttl_s + 30.0,
            1 if use_global_gate else 0,
            teacher_limit if teacher_hash else 0,
            global_in_flight_limit if global_first_attempt else 0,
            global_teacher_limit if teacher_hash else 0,
            teacher_hash,
            normalized_waiter_id,
            waiter_expiry,
            f"{first_attempt_base}:teacher:",
        )
        granted, wait_s, circuit = int(result[0]), float(result[1]), int(result[2])
        if not granted:
            return AcquireDecision(wait_s=max(0.01, wait_s), circuit_open=bool(circuit))
        return AcquireDecision(
            permit=PoolPermit(
                self,
                pool_name=pool.coordination_key,
                lease_id=lease_id,
                borrowed=borrowed,
                teacher_hash=teacher_hash,
                global_first_attempt=global_first_attempt,
            ),
            wait_s=0.0,
        )

    async def release(self, permit: PoolPermit) -> None:
        base = self._pool_base(permit.pool_name)
        first_attempt_base = self._first_attempt_base()
        await self._eval(
            _RELEASE_LUA,
            5,
            f"{base}:leases",
            f"{base}:borrowed",
            f"{base}:teacher:{permit.teacher_hash or 'anonymous'}",
            f"{first_attempt_base}:leases",
            f"{first_attempt_base}:teacher:{permit.teacher_hash or 'anonymous'}",
            permit.lease_id,
            1 if permit.global_first_attempt else 0,
        )

    async def remove_first_attempt_waiter(
        self,
        teacher_hash: str,
        waiter_id: str,
    ) -> None:
        if not teacher_hash or not waiter_id:
            return
        await self._eval(
            _REMOVE_FIRST_ATTEMPT_WAITER_LUA,
            1,
            f"{self._first_attempt_base()}:waiters",
            f"{teacher_hash}\x1f{waiter_id}",
        )

    async def get_batch_start(self, batch_hash: str, ttl_s: float) -> float:
        key = f"{self._namespace}:batch:{batch_hash}:started"
        now = time.time()
        # One atomic NX write freezes the deadline for every worker in this
        # logical phase. Phase-specific routing keys give teacher-confirmed
        # recovery work a new budget without allowing one phase to reset itself.
        await asyncio.wait_for(
            self._redis.set(
                key,
                str(now),
                ex=max(86400, math.ceil(ttl_s)),
                nx=True,
            ),
            timeout=self._operation_timeout_s,
        )
        value = await asyncio.wait_for(self._redis.get(key), timeout=self._operation_timeout_s)
        return float(value)

    async def claim_retry(
        self,
        batch_hash: str,
        paper_hash: str,
        maximum: int,
        ttl_s: float,
    ) -> bool:
        result = await self._eval(
            _CLAIM_RETRY_LUA,
            2,
            f"{self._namespace}:batch:{batch_hash}:retries",
            f"{self._namespace}:batch:{batch_hash}:paper:{paper_hash}:retried",
            maximum,
            max(60, math.ceil(ttl_s)),
        )
        return bool(int(result))

    async def release_retry_claim(
        self,
        batch_hash: str,
        paper_hash: str,
    ) -> bool:
        result = await self._eval(
            _RELEASE_RETRY_CLAIM_LUA,
            2,
            f"{self._namespace}:batch:{batch_hash}:retries",
            f"{self._namespace}:batch:{batch_hash}:paper:{paper_hash}:retried",
        )
        return bool(int(result))

    async def record_success(self, pool_name: str) -> None:
        base = self._pool_base(pool_name)
        await asyncio.wait_for(
            self._redis.delete(f"{base}:failures"), timeout=self._operation_timeout_s
        )

    async def record_failure(
        self,
        pool_name: str,
        *,
        threshold: int,
        cooldown_s: float,
        fatal: bool,
    ) -> None:
        base = self._pool_base(pool_name)
        await self._eval(
            _RECORD_FAILURE_LUA,
            2,
            f"{base}:circuit",
            f"{base}:failures",
            1 if fatal else 0,
            threshold,
            cooldown_s,
        )

    async def is_circuit_open(self, pool_name: str) -> bool:
        return bool(
            await asyncio.wait_for(
                self._redis.exists(f"{self._pool_base(pool_name)}:circuit"),
                timeout=self._operation_timeout_s,
            )
        )

    async def get_active_primary(self, default: str) -> str:
        key = f"{self._namespace}:active-primary"
        value = await asyncio.wait_for(
            self._redis.get(key),
            timeout=self._operation_timeout_s,
        )
        if value is None:
            return default
        ttl = await asyncio.wait_for(
            self._redis.ttl(key),
            timeout=self._operation_timeout_s,
        )
        if ttl == -2:
            return default
        if ttl < 0:
            # Bound keys written by pre-TTL deployments without extending the
            # lifetime of already-migrated selections on every read.
            await asyncio.wait_for(
                self._redis.expire(key, math.ceil(_ACTIVE_PRIMARY_TTL_S)),
                timeout=self._operation_timeout_s,
            )
        return str(value)

    async def set_active_primary(self, name: str) -> None:
        await asyncio.wait_for(
            self._redis.set(
                f"{self._namespace}:active-primary",
                name,
                ex=math.ceil(_ACTIVE_PRIMARY_TTL_S),
            ),
            timeout=self._operation_timeout_s,
        )

    async def accelerator_gate(self, batch_hash: str) -> str:
        value = await asyncio.wait_for(
            self._redis.get(f"{self._namespace}:batch:{batch_hash}:accelerator"),
            timeout=self._operation_timeout_s,
        )
        return str(value or "unknown")

    async def claim_accelerator_probe(self, batch_hash: str) -> bool:
        result = await self._eval(
            _CLAIM_PROBE_LUA,
            1,
            f"{self._namespace}:batch:{batch_hash}:accelerator",
            180,
        )
        return bool(int(result))

    async def finish_accelerator_probe(self, batch_hash: str, healthy: bool) -> None:
        await asyncio.wait_for(
            self._redis.set(
                f"{self._namespace}:batch:{batch_hash}:accelerator",
                "healthy" if healthy else "blocked",
                ex=600,
            ),
            timeout=self._operation_timeout_s,
        )

    async def abandon_accelerator_probe(self, batch_hash: str) -> None:
        await self._eval(
            _ABANDON_PROBE_LUA,
            1,
            f"{self._namespace}:batch:{batch_hash}:accelerator",
        )

    async def add_priority_waiter(self, pool_name: str, waiter_id: str, ttl_s: float) -> None:
        base = self._pool_base(pool_name)
        now = time.time()
        key = f"{base}:priority"
        await asyncio.wait_for(
            self._redis.zadd(key, {waiter_id: now + ttl_s}),
            timeout=self._operation_timeout_s,
        )
        await asyncio.wait_for(self._redis.expire(key, 600), timeout=self._operation_timeout_s)

    async def remove_priority_waiter(self, pool_name: str, waiter_id: str) -> None:
        base = self._pool_base(pool_name)
        await asyncio.wait_for(
            self._redis.zrem(f"{base}:priority", waiter_id),
            timeout=self._operation_timeout_s,
        )

    async def ping(self) -> None:
        response = await asyncio.wait_for(
            self._redis.ping(), timeout=self._operation_timeout_s
        )
        if not response:
            raise RuntimeError("OCR pool Redis coordinator did not acknowledge PING")

    async def close(self) -> None:
        await self._redis.aclose()


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
