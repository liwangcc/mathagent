"""The process-local pool coordinator, used by tests and explicit dev mode.

``LocalPoolCoordinator`` keeps in memory what ``RedisPoolCoordinator`` keeps in
Redis: per-pool token buckets (``_TokenBucket``) and capacity, first-attempt and
priority waiters, batch start times and retry claims, circuit state, the active
primary pool and the accelerator probe.  It is deterministic, so tests can drive it.

Moved unchanged from services/ocr_pool_scheduler.py, which still exposes these names.
"""

from __future__ import annotations

import asyncio
import math
import time
import uuid

from services.ocr_pool_scheduler_base import (
    AcquireDecision,
    PoolPermit,
    PoolSpec,
    _ACTIVE_PRIMARY_TTL_S,
)


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
