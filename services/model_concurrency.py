"""Shared model-concurrency leases with teacher fair sharing.

Redis is the production coordinator because gunicorn workers do not share
in-memory semaphores. Every lease occupies provider, teacher, and credential
capacity atomically. Credential buckets use one-way fingerprints so API keys
never appear in Redis keys or values.

The scheduler gives every teacher a base allocation.  A teacher may borrow idle
capacity only when no other teacher is waiting, so a busy teacher cannot starve
newly active teachers.  Redis lease TTLs and renewal make worker crashes self-
healing without retaining student content in Redis.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import math
import random
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

# Provider namespaces are deliberately explicit.  Keep the historical shared
# name as a compatibility alias for callers that pre-date route packages; new
# route-aware callers must use the subscription constant.
MIMO_SHARED_PROVIDER = "xiaomi_mimo"
MIMO_SUBSCRIPTION_PROVIDER = "xiaomi_mimo_subscription"
MIMO_SUBSCRIPTION_HARD_CONCURRENCY = 8


def canonical_provider(provider: str) -> str:
    """Normalize legacy provider aliases before touching shared state.

    Older template/Hermes callers still pass ``xiaomi_mimo``.  That alias is
    the historical name for the subscription account, so retaining it as a
    separate Redis namespace would let two workers exceed the subscription
    eight-slot ceiling.  Other explicit provider names remain independent.
    """
    value = str(provider or "provider").strip() or "provider"
    if value == MIMO_SHARED_PROVIDER:
        return MIMO_SUBSCRIPTION_PROVIDER
    return value


def provider_hard_concurrency(provider: str) -> int | None:
    """Return a known provider-wide in-flight ceiling, if one exists."""
    return {
        MIMO_SUBSCRIPTION_PROVIDER: MIMO_SUBSCRIPTION_HARD_CONCURRENCY,
    }.get(canonical_provider(provider))


def provider_default_reserve_capacity(provider: str) -> int | None:
    """Return the route default reserve without changing legacy callers.

    DashScope Flash and MiMo subscription use all idle slots, so their default
    reserve is zero. ``None`` keeps the configured reserve for generic callers.
    """
    if canonical_provider(provider) in {"dashscope", MIMO_SUBSCRIPTION_PROVIDER}:
        return 0
    return None


def _cap_provider_request(
    provider: str,
    global_capacity: int,
    credential_capacity: int | None,
    teacher_base_capacity: int,
    teacher_burst_capacity: int | None,
) -> tuple[int, int | None, int, int | None]:
    """Clamp caller-provided capacities to a known provider hard ceiling."""
    hard = provider_hard_concurrency(provider)
    if hard is not None:
        global_capacity = min(int(global_capacity), hard)
    credential_capacity = (
        None if credential_capacity is None else min(int(credential_capacity), global_capacity)
    )
    teacher_base_capacity = min(int(teacher_base_capacity), global_capacity)
    teacher_burst_capacity = (
        None
        if teacher_burst_capacity is None
        else min(int(teacher_burst_capacity), global_capacity)
    )
    return (
        global_capacity,
        credential_capacity,
        teacher_base_capacity,
        teacher_burst_capacity,
    )


def teacher_fingerprint(provider: str, teacher_id: str) -> str:
    """Derive a provider-scoped opaque teacher identifier for Redis keys."""
    # Keep identity private in Redis and prevent a teacher's fairness bucket on
    # one provider from accidentally being reused by another route.
    import config

    raw_provider = canonical_provider(provider)
    raw_teacher = str(teacher_id or "system").strip() or "system"
    secret = str(getattr(config, "JWT_SECRET", "") or "").encode("utf-8")
    if not secret:
        # Local test/development configurations may intentionally omit JWT. A
        # deterministic digest still avoids writing the raw identity.
        secret = b"mathagent-local-teacher-fingerprint"
    material = raw_provider.encode("utf-8") + b"\0" + raw_teacher.encode("utf-8")
    return hmac.new(secret, material, hashlib.sha256).hexdigest()[:24]


class RateBudgetTimeout(TimeoutError):
    """A provider-wide request or token budget did not free up in time."""


_RATE_BUDGET_POLL_SECONDS = 0.1
_RATE_BUDGET_POLL_FANOUT = 16
_RATE_BUDGET_POLL_MAX_SECONDS = 1.0


@dataclass(frozen=True)
class RateBudgetReservation:
    """Opaque identity of one opt-in request in a provider's rolling window."""

    provider: str
    reservation_id: str


class _RateBudgetReservationMixin:
    async def reserve_rate_budget(
        self,
        *,
        provider: str,
        requests_per_minute: float,
        tokens_per_minute: int,
        reserved_tokens: int,
        timeout_seconds: float,
    ) -> RateBudgetReservation | None:
        """Reserve RPM/TPM without acquiring a concurrency lease.

        A candidate ID exists before any Redis await, so an interrupted or lost
        admission response can be refunded safely. Only callers certain that
        no request was sent may subsequently cancel a returned reservation.
        """
        _validate_rate_budget(
            requests_per_minute=requests_per_minute,
            tokens_per_minute=tokens_per_minute,
            reserved_tokens=reserved_tokens,
            timeout_seconds=timeout_seconds,
        )
        if requests_per_minute <= 0 and tokens_per_minute <= 0:
            return None
        reservation = RateBudgetReservation(canonical_provider(provider), uuid.uuid4().hex)
        try:
            await self._acquire_rate_budget(
                provider=reservation.provider,
                requests_per_minute=requests_per_minute,
                tokens_per_minute=tokens_per_minute,
                reserved_tokens=reserved_tokens,
                timeout_seconds=timeout_seconds,
                reservation=reservation,
            )
        except BaseException:
            try:
                # The shield lets the refund finish even if this caller is
                # cancelled again; that new cancellation then propagates.
                await asyncio.shield(self.cancel_rate_budget(reservation))
            except Exception:
                # Coordinator failure leaves the conservative reservation to
                # its original bounded window; preserve the admission error.
                logger.warning("failed to cancel interrupted rate reservation", exc_info=True)
            raise
        return reservation

    async def _rate_budget_sleep(
        self, key: str, until_expiry: float, remaining: float, *, poll: bool,
    ) -> None:
        """Sleep between admission probes without fixed request pacing.

        Legacy callers sleep until the oldest entry expires. Reservation
        waiters poll, because a refund or settlement can free budget long
        before that. Any waiter may claim freed capacity, so refill latency
        depends on the aggregate probe rate: the per-waiter interval grows
        with this worker's sleeping waiters (capped) to keep a saturated
        worker near a constant Redis probe rate, and jitter keeps a blocked
        burst from probing in lockstep.
        """
        delay = until_expiry
        if not poll:
            await asyncio.sleep(min(delay, remaining))
            return
        waiters = self.__dict__.setdefault("_rate_budget_local_waiters", {})
        interval = min(
            _RATE_BUDGET_POLL_MAX_SECONDS,
            _RATE_BUDGET_POLL_SECONDS
            * max(1.0, (waiters.get(key, 0) + 1) / _RATE_BUDGET_POLL_FANOUT),
        )
        delay = min(delay, interval * random.uniform(0.5, 1.0))
        waiters[key] = waiters.get(key, 0) + 1
        try:
            await asyncio.sleep(min(delay, remaining))
        finally:
            count = waiters.get(key, 0) - 1
            if count > 0:
                waiters[key] = count
            else:
                waiters.pop(key, None)


@dataclass(frozen=True)
class FairSharePolicy:
    global_capacity: int
    teacher_base_capacity: int
    # Optional so callers using the original two-argument policy continue to
    # work.  Route callers can explicitly set reserve=0 and a burst equal to
    # the provider capacity when a sole teacher may use every free slot.
    teacher_burst_capacity: int | None = None
    global_reserve_capacity: int | None = None

    def __post_init__(self) -> None:
        if self.global_capacity < 1:
            raise ValueError("global_capacity must be at least 1")
        if self.teacher_base_capacity < 1:
            raise ValueError("teacher_base_capacity must be at least 1")
        if self.teacher_base_capacity > self.global_capacity:
            raise ValueError("teacher_base_capacity must not exceed global capacity")

        burst_capacity = self.teacher_burst_capacity
        if burst_capacity is None:
            burst_capacity = (
                self.global_capacity
                if self.global_reserve_capacity == 0
                else min(self.global_capacity, self.teacher_base_capacity * 2)
            )

        # Keep a base-sized slice available for another teacher whenever the
        # global pool is large enough.  Tiny test/development pools naturally
        # clamp this reserve to zero instead of making all burst capacity idle.
        reserve_capacity = self.global_reserve_capacity
        if reserve_capacity is None:
            reserve_capacity = min(
                self.teacher_base_capacity,
                max(0, self.global_capacity - burst_capacity),
            )

        object.__setattr__(self, "teacher_burst_capacity", burst_capacity)
        object.__setattr__(self, "global_reserve_capacity", reserve_capacity)

        if not self.teacher_base_capacity <= burst_capacity <= self.global_capacity:
            raise ValueError("teacher_burst_capacity must be between base and global capacity")
        if not 0 <= reserve_capacity < self.global_capacity:
            raise ValueError("global_reserve_capacity must be below global capacity")


def configured_fair_share_policy(
    global_capacity: int,
    *,
    provider: str | None = None,
    global_reserve_capacity: int | None = None,
    teacher_base_capacity: int | None = None,
    teacher_burst_capacity: int | None = None,
) -> FairSharePolicy:
    """Resolve the configured per-teacher base/burst policy for a pool."""
    from config import (
        MODEL_GLOBAL_RESERVE_CONCURRENCY,
        MODEL_TEACHER_BASE_CONCURRENCY,
        MODEL_TEACHER_BURST_CONCURRENCY,
    )

    if global_capacity < 1:
        raise ValueError("global_capacity must be at least 1")

    # Keep a base slice for another teacher in small pools.  The Lua
    # coordinator can still use the configured burst when this is the only
    # participating teacher.  Route callers may override these values without
    # changing global configuration (for example, a provider with reserve=0).
    configured_base = (
        MODEL_TEACHER_BASE_CONCURRENCY
        if teacher_base_capacity is None
        else int(teacher_base_capacity)
    )
    teacher_base = min(
        configured_base,
        max(1, global_capacity // 2),
    )
    configured_burst = (
        MODEL_TEACHER_BURST_CONCURRENCY
        if teacher_burst_capacity is None
        else int(teacher_burst_capacity)
    )
    teacher_burst = min(configured_burst, global_capacity)

    if global_reserve_capacity is None:
        route_reserve = (
            provider_default_reserve_capacity(provider)
            if provider
            else None
        )
        global_reserve = (
            route_reserve
            if route_reserve is not None
            else min(
                MODEL_GLOBAL_RESERVE_CONCURRENCY,
                max(0, global_capacity - teacher_burst),
            )
        )
    else:
        global_reserve = int(global_reserve_capacity)

    # Explicit reserve=0 is the route contract for a provider where a sole
    # teacher may consume all idle capacity.  Expand its burst ceiling unless
    # the caller deliberately supplied a narrower one.
    if global_reserve == 0 and teacher_burst_capacity is None:
        teacher_burst = global_capacity

    # A hard provider cap can reduce the effective base after caller values
    # are clamped.  Do not let an oversized explicit reserve strand capacity
    # below that base; preserve ordinary non-zero reserves while keeping the
    # policy identical to the Redis and local limiter normalization paths.
    global_reserve = min(
        global_reserve,
        max(0, global_capacity - teacher_base),
    )

    return FairSharePolicy(
        global_capacity=global_capacity,
        teacher_base_capacity=teacher_base,
        teacher_burst_capacity=teacher_burst,
        global_reserve_capacity=global_reserve,
    )


class ModelLease:
    """A shared permit with an explicitly bounded lifetime.

    A one-TTL request has no heartbeat.  A slow request may use a heartbeat,
    but only until a finite hard deadline; a stuck request therefore cannot
    renew a Redis permit forever.
    """

    def __init__(
        self,
        release: Callable[[], Awaitable[None]],
        renew: Callable[[float], Awaitable[bool]],
        *,
        lease_seconds: int,
        max_hold_seconds: int | None = None,
    ) -> None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        if max_hold_seconds is None:
            max_hold_seconds = lease_seconds
        if max_hold_seconds < lease_seconds:
            raise ValueError("max_hold_seconds must be at least lease_seconds")
        self._release = release
        self._renew = renew
        self._lease_seconds = lease_seconds
        self._max_hold_seconds = max_hold_seconds
        self._renew_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "ModelLease":
        if self._max_hold_seconds > self._lease_seconds:
            self._renew_task = asyncio.create_task(
                self._renew_loop(
                    deadline_monotonic=time.monotonic() + self._max_hold_seconds,
                    deadline_epoch=time.time() + self._max_hold_seconds,
                )
            )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._renew_task:
            self._renew_task.cancel()
            await asyncio.gather(self._renew_task, return_exceptions=True)
            self._renew_task = None
        try:
            await self._release()
        except asyncio.CancelledError:
            raise
        except Exception:
            # An explicit release failure cannot make a permanent lock: the
            # Redis score has a bounded TTL and the next acquire cleans it.
            logger.exception("failed to release shared model lease; awaiting TTL cleanup")

    async def _renew_loop(self, *, deadline_monotonic: float, deadline_epoch: float) -> None:
        try:
            while True:
                remaining = deadline_monotonic - time.monotonic()
                if remaining <= 0:
                    logger.warning("shared model lease reached max hold time; renewal stopped")
                    return
                await asyncio.sleep(min(max(1, self._lease_seconds // 3), remaining))
                remaining = deadline_monotonic - time.monotonic()
                if remaining <= 0:
                    logger.warning("shared model lease reached max hold time; renewal stopped")
                    return

                # A stalled event loop must never revive an already expired
                # lease, nor write an expiry beyond the original hard deadline.
                expiry_epoch = min(time.time() + self._lease_seconds, deadline_epoch)
                if not await self._renew(expiry_epoch):
                    logger.warning("shared model lease disappeared before request completion")
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("failed to renew shared model lease; renewal stopped")


class NoopModelLimiter(_RateBudgetReservationMixin):
    """Explicit local-development fallback; production must configure Redis."""

    def __init__(self) -> None:
        self._rate_budget_lock = threading.Lock()
        self._rate_budget_windows: dict[str, list[tuple]] = {}

    async def acquire_rate_budget(
        self,
        *,
        provider: str,
        requests_per_minute: float,
        tokens_per_minute: int,
        reserved_tokens: int,
        timeout_seconds: float,
    ) -> None:
        """Apply the same rolling one-minute quota in explicit local mode."""
        await self._acquire_rate_budget(
            provider=provider,
            requests_per_minute=requests_per_minute,
            tokens_per_minute=tokens_per_minute,
            reserved_tokens=reserved_tokens,
            timeout_seconds=timeout_seconds,
        )

    async def _acquire_rate_budget(
        self,
        *,
        provider: str,
        requests_per_minute: float,
        tokens_per_minute: int,
        reserved_tokens: int,
        timeout_seconds: float,
        reservation: RateBudgetReservation | None = None,
    ) -> None:
        _validate_rate_budget(
            requests_per_minute=requests_per_minute,
            tokens_per_minute=tokens_per_minute,
            reserved_tokens=reserved_tokens,
            timeout_seconds=timeout_seconds,
        )
        if requests_per_minute <= 0 and tokens_per_minute <= 0:
            return
        deadline = time.monotonic() + timeout_seconds
        key = canonical_provider(provider)
        while True:
            now = time.time()
            cutoff = now - 60.0
            rate_lock = getattr(self, "_rate_budget_lock", None)
            if rate_lock is None:
                rate_lock = threading.Lock()
                self._rate_budget_lock = rate_lock
            with rate_lock:
                windows = getattr(self, "_rate_budget_windows", None)
                if windows is None:
                    # Test doubles sometimes omit super().__init__.
                    self._rate_budget_windows = {}
                    windows = self._rate_budget_windows
                entries = [item for item in windows.get(key, []) if item[0] > cutoff]
                used_requests = len(entries)
                used_tokens = sum(item[1] for item in entries)
                request_blocked = (
                    requests_per_minute > 0
                    and used_requests >= math.floor(requests_per_minute)
                )
                token_blocked = (
                    tokens_per_minute > 0
                    and used_tokens + reserved_tokens > tokens_per_minute
                )
                if not request_blocked and not token_blocked:
                    # Opt-in entries: (time, tokens, id, settled); legacy
                    # entries stay (time, tokens) and are never reconciled.
                    entries.append((now, reserved_tokens, reservation.reservation_id, False)
                                   if reservation else (now, reserved_tokens))
                    windows[key] = entries
                    return
                windows[key] = entries
                oldest = entries[0][0] if entries else now
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RateBudgetTimeout("timed out waiting for provider rate budget")
            wait_seconds = max(0.01, oldest + 60.0 - now)
            await self._rate_budget_sleep(
                key, wait_seconds, remaining, poll=reservation is not None,
            )

    async def settle_rate_budget(
        self, reservation: RateBudgetReservation, *, actual_tokens: int,
    ) -> bool:
        """Replace an estimate once; unknown usage must not call this method."""
        _validate_actual_tokens(actual_tokens)
        return self._reconcile_rate_budget(reservation, actual_tokens=actual_tokens)

    async def cancel_rate_budget(self, reservation: RateBudgetReservation) -> bool:
        """Refund both budgets only for an untransmitted, pending request."""
        return self._reconcile_rate_budget(reservation, actual_tokens=None)

    def _reconcile_rate_budget(
        self, reservation: RateBudgetReservation, *, actual_tokens: int | None,
    ) -> bool:
        _validate_reservation(reservation)
        rate_lock = getattr(self, "_rate_budget_lock", None)
        if rate_lock is None:
            return False
        key = canonical_provider(reservation.provider)
        with rate_lock:
            windows = self._rate_budget_windows
            if key not in windows:
                return False
            cutoff = time.time() - 60
            entries = [item for item in windows[key] if item[0] > cutoff]
            windows[key] = entries
            for index, item in enumerate(entries):
                if len(item) < 4 or item[2] != reservation.reservation_id or item[3]:
                    continue
                if actual_tokens is None:
                    entries.pop(index)
                else:
                    entries[index] = (item[0], actual_tokens, item[2], True)
                return True
        return False

    async def acquire(
        self,
        *,
        provider: str,
        api_key: str,
        teacher_id: str,
        global_capacity: int,
        credential_capacity: int | None = None,
        teacher_base_capacity: int,
        teacher_burst_capacity: int | None = None,
        global_reserve_capacity: int | None = None,
    ) -> ModelLease:
        # Validate and normalize the optional policy knobs even in local mode;
        # this keeps the fallback's API and boundary behavior identical to the
        # Redis coordinator.
        burst_was_omitted = teacher_burst_capacity is None
        reserve_was_omitted = global_reserve_capacity is None
        (
            global_capacity,
            credential_capacity,
            teacher_base_capacity,
            teacher_burst_capacity,
        ) = _cap_provider_request(
            provider,
            global_capacity,
            credential_capacity,
            teacher_base_capacity,
            teacher_burst_capacity,
        )
        if global_reserve_capacity is None:
            global_reserve_capacity = provider_default_reserve_capacity(provider)
        if (
            burst_was_omitted
            and reserve_was_omitted
            and global_reserve_capacity == 0
            and canonical_provider(provider) in {"dashscope", MIMO_SUBSCRIPTION_PROVIDER}
        ):
            teacher_burst_capacity = global_capacity
        if global_reserve_capacity is not None:
            global_reserve_capacity = min(
                int(global_reserve_capacity),
                max(0, global_capacity - teacher_base_capacity),
            )
        policy = FairSharePolicy(
            global_capacity,
            teacher_base_capacity,
            teacher_burst_capacity,
            global_reserve_capacity,
        )
        if credential_capacity is None:
            credential_capacity = policy.global_capacity
        if not 1 <= credential_capacity <= policy.global_capacity:
            raise ValueError(
                "credential_capacity must be between 1 and global capacity"
            )

        async def release() -> None:
            return None

        async def renew(_expiry_epoch: float) -> bool:
            return True

        return ModelLease(release, renew, lease_seconds=300, max_hold_seconds=300)

    async def is_credential_invalid(
        self, *, provider: str, api_key: str, base_url: str, model: str,
    ) -> bool:
        return False

    async def mark_credential_invalid(
        self,
        *,
        provider: str,
        api_key: str,
        base_url: str,
        model: str,
        status_code: int,
    ) -> None:
        return None

    async def credential_cooldown_seconds(
        self, *, provider: str, api_key: str, base_url: str, model: str,
    ) -> float:
        return 0.0

    async def mark_credential_cooldown(
        self,
        *,
        provider: str,
        api_key: str,
        base_url: str,
        model: str,
        cooldown_seconds: float,
    ) -> None:
        return None

    async def is_provider_failure(self, provider: str) -> bool:
        return False

    async def mark_provider_failure(self, provider: str, cooldown_seconds: int) -> None:
        return None

    async def clear_provider_failure(self, provider: str) -> None:
        return None


def _validate_rate_budget(
    *,
    requests_per_minute: float,
    tokens_per_minute: int,
    reserved_tokens: int,
    timeout_seconds: float,
) -> None:
    if isinstance(requests_per_minute, bool) or not isinstance(requests_per_minute, (int, float)):
        raise ValueError("requests_per_minute must be a number, not bool")
    if isinstance(tokens_per_minute, bool) or not isinstance(tokens_per_minute, int):
        raise ValueError("tokens_per_minute must be an integer, not bool")
    if isinstance(reserved_tokens, bool) or not isinstance(reserved_tokens, int):
        raise ValueError("reserved_tokens must be an integer, not bool")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise ValueError("timeout_seconds must be a number, not bool")
    if (
        not math.isfinite(requests_per_minute)
        or requests_per_minute < 0
        or (0 < requests_per_minute < 1)
    ):
        raise ValueError("requests_per_minute must be zero or at least one")
    if tokens_per_minute < 0:
        raise ValueError("tokens_per_minute must not be negative")
    if reserved_tokens < 1:
        raise ValueError("reserved_tokens must be positive")
    if tokens_per_minute and reserved_tokens > tokens_per_minute:
        raise ValueError("one request exceeds the provider token budget")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and positive")


def _validate_actual_tokens(actual_tokens: int) -> None:
    if isinstance(actual_tokens, bool) or not isinstance(actual_tokens, int) or actual_tokens < 0:
        raise ValueError("actual_tokens must be a nonnegative integer, not bool")


def _validate_reservation(reservation: RateBudgetReservation) -> None:
    if (
        not isinstance(reservation, RateBudgetReservation)
        or not isinstance(reservation.provider, str)
        or not reservation.provider.strip()
        or not isinstance(reservation.reservation_id, str)
        or not reservation.reservation_id
    ):
        raise ValueError("a valid RateBudgetReservation is required")


_ACQUIRE_LUA = """
local now = tonumber(ARGV[1])
local lease_expiry = tonumber(ARGV[2])
local waiter_expiry = tonumber(ARGV[3])
local capacity = tonumber(ARGV[4])
local teacher_base = tonumber(ARGV[5])
local teacher_burst = tonumber(ARGV[6])
local global_reserve = tonumber(ARGV[7])
local credential_capacity = tonumber(ARGV[8])
local lease_id = ARGV[9]
local teacher_id = ARGV[10]
local separator = string.char(31)
local teacher_prefix = teacher_id .. separator
local waiter_id = teacher_prefix .. lease_id
local global_member = waiter_id

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', now)
redis.call('ZREMRANGEBYSCORE', KEYS[4], '-inf', now)
redis.call('ZADD', KEYS[4], waiter_expiry, waiter_id)
local waiter_ttl_ms = math.max(1000, math.ceil((waiter_expiry - now + 60) * 1000))
local current_waiter_ttl_ms = redis.call('PTTL', KEYS[4])
if current_waiter_ttl_ms < waiter_ttl_ms then
  redis.call('PEXPIRE', KEYS[4], waiter_ttl_ms)
end

local global_active = redis.call('ZCARD', KEYS[1])
local teacher_active = redis.call('ZCARD', KEYS[2])
local credential_active = redis.call('ZCARD', KEYS[3])
-- Global members carry their teacher prefix so the coordinator can derive
-- active teacher counts without maintaining a second, crash-prone registry.
local active_counts = {}
local active_teachers = {}
local active_members = redis.call('ZRANGE', KEYS[1], 0, -1)
for _, member in ipairs(active_members) do
  local separator_at = string.find(member, separator, 1, true)
  local active_teacher = separator_at and string.sub(member, 1, separator_at - 1) or member
  active_counts[active_teacher] = (active_counts[active_teacher] or 0) + 1
  active_teachers[active_teacher] = true
end
local waiters = redis.call('ZRANGEBYSCORE', KEYS[4], now, '+inf')
local other_waiting = false
local waiting_teachers = {}
for _, value in ipairs(waiters) do
  local separator_at = string.find(value, separator, 1, true)
  local waiting_teacher = separator_at and string.sub(value, 1, separator_at - 1) or value
  waiting_teachers[waiting_teacher] = true
  if waiting_teacher ~= teacher_id then
    other_waiting = true
  end
end

-- Count distinct active or waiting teachers.  A waiter makes the teacher a
-- participant before it receives its first lease, so a late teacher gets a
-- fair turn rather than waiting behind the first teacher's burst.
local participant_count = 0
for active_teacher, _ in pairs(active_teachers) do
  participant_count = participant_count + 1
end
for waiting_teacher, _ in pairs(waiting_teachers) do
  if not active_teachers[waiting_teacher] then
    participant_count = participant_count + 1
  end
end
if participant_count < 1 then
  participant_count = 1
end

-- The reserve is an explicit policy value.  Do not synthesize a hidden slot
-- for a single participant: a route configured with reserve=0 must be able to
-- consume every healthy provider slot while it is the only waiter.  Once
-- another teacher joins, the fair-share calculation still rotates grants.
local fair_capacity = math.max(1, capacity - global_reserve)
local fair_limit = math.ceil(fair_capacity / participant_count)
-- teacher_burst remains a hard per-teacher safety ceiling.  The fair limit is
-- the dynamic share within that ceiling, so existing burst policy still holds
-- while two or more teachers converge to equal active counts.
fair_limit = math.min(teacher_burst, fair_limit)
local min_active = teacher_active
for active_teacher, active_count in pairs(active_counts) do
  if active_count < min_active then
    min_active = active_count
  end
end
for waiting_teacher, _ in pairs(waiting_teachers) do
  if not active_teachers[waiting_teacher] then
    min_active = 0
    break
  end
end
local turn_grant = not other_waiting or teacher_active <= min_active
local fair_grant = teacher_active < fair_limit and turn_grant
-- Flash has one shared work window: only teachers actually requesting a slot
-- compete. An active teacher with no backlog must not strand its unused share.
-- Compare with other waiting teachers only; do not count this teacher's own
-- waiter, otherwise it would compare against itself and could starve peers.
if global_reserve == 0 and teacher_burst == capacity
    and ARGV[11] == "work_conserving" then
  if not other_waiting then
    fair_grant = true
  else
    local min_other_waiting_active = nil
    for waiting_teacher, _ in pairs(waiting_teachers) do
      if waiting_teacher ~= teacher_id then
        local waiting_active = active_counts[waiting_teacher] or 0
        if min_other_waiting_active == nil
            or waiting_active < min_other_waiting_active then
          min_other_waiting_active = waiting_active
        end
      end
    end
    fair_grant = min_other_waiting_active ~= nil
        and teacher_active <= min_other_waiting_active
  end
end

if global_active < capacity
    and credential_active < credential_capacity
    and fair_grant
    and global_active < fair_capacity then
  redis.call('ZADD', KEYS[1], lease_expiry, global_member)
  redis.call('ZADD', KEYS[2], lease_expiry, lease_id)
  redis.call('ZADD', KEYS[3], lease_expiry, lease_id)
  local lease_ttl_ms = math.max(1000, math.ceil((lease_expiry - now + 60) * 1000))
  for key_index = 1, 3 do
    local current_ttl_ms = redis.call('PTTL', KEYS[key_index])
    if current_ttl_ms < lease_ttl_ms then
      redis.call('PEXPIRE', KEYS[key_index], lease_ttl_ms)
    end
  end
  redis.call('ZREM', KEYS[4], waiter_id)
  return 1
end
return 0
"""

_RELEASE_LUA = """
redis.call('ZREM', KEYS[1], ARGV[2])
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('ZREM', KEYS[3], ARGV[1])
return 1
"""

_RENEW_LUA = """
local lease_id = ARGV[1]
local now = tonumber(ARGV[2])
local expiry = tonumber(ARGV[3])
local global_member = ARGV[4]
local current_expiry = redis.call('ZSCORE', KEYS[1], global_member)
if current_expiry == false or tonumber(current_expiry) <= now or expiry <= now then
  redis.call('ZREM', KEYS[1], global_member)
  redis.call('ZREM', KEYS[2], lease_id)
  redis.call('ZREM', KEYS[3], lease_id)
  return 0
end
redis.call('ZADD', KEYS[1], expiry, global_member)
redis.call('ZADD', KEYS[2], expiry, lease_id)
redis.call('ZADD', KEYS[3], expiry, lease_id)
local lease_ttl_ms = math.max(1000, math.ceil((expiry - now + 60) * 1000))
for key_index = 1, 3 do
  local current_ttl_ms = redis.call('PTTL', KEYS[key_index])
  if current_ttl_ms < lease_ttl_ms then
    redis.call('PEXPIRE', KEYS[key_index], lease_ttl_ms)
  end
end
return 1
"""

_MARK_CREDENTIAL_COOLDOWN_LUA = """
local requested_ttl_ms = tonumber(ARGV[1])
local current_ttl_ms = redis.call('PTTL', KEYS[1])
if current_ttl_ms < requested_ttl_ms then
  redis.call('SET', KEYS[1], '1', 'PX', requested_ttl_ms)
  return requested_ttl_ms
end
return current_ttl_ms
"""


_RATE_BUDGET_WINDOW_LUA = """
local now = tonumber(ARGV[1])
local cutoff = tonumber(ARGV[2])
local totals_initialized = redis.call('HGET', KEYS[3], 'initialized') == '1'
-- Keep each unpack below Redis/Lua's argument-stack limit. Qwen's 30K RPM
-- window can legitimately contain tens of thousands of expired members.
local cleanup_batch = 256
while true do
  local expired = redis.call(
    'ZRANGEBYSCORE', KEYS[1], '-inf', cutoff,
    'LIMIT', 0, cleanup_batch
  )
  if #expired == 0 then
    break
  end
  if totals_initialized then
    local expired_count = 0
    local expired_token_total = 0
    local expired_values = redis.call('HMGET', KEYS[2], unpack(expired))
    for value_index, _ in ipairs(expired) do
      local expired_tokens = tonumber(expired_values[value_index] or '0')
      expired_count = expired_count + 1
      expired_token_total = expired_token_total + expired_tokens
    end
    redis.call('HINCRBY', KEYS[3], 'requests', -expired_count)
    redis.call('HINCRBY', KEYS[3], 'tokens', -expired_token_total)
  end
  redis.call('ZREM', KEYS[1], unpack(expired))
  redis.call('HDEL', KEYS[2], unpack(expired))
  redis.call('HDEL', KEYS[4], unpack(expired))
  if #expired < cleanup_batch then
    break
  end
end
-- Older deployments stored one hash field per request but had no aggregate
-- counter. Build the counter once after stale entries are removed; steady-state
-- acquisitions then read two O(1) fields instead of scanning the full window.
if not totals_initialized then
  local members = redis.call('ZRANGE', KEYS[1], 0, -1)
  local used_requests = 0
  local used_tokens = 0
  for _, existing_member in ipairs(members) do
    used_requests = used_requests + 1
    used_tokens = used_tokens + tonumber(redis.call('HGET', KEYS[2], existing_member) or '0')
  end
  if used_requests > 0 then
    redis.call('HSET', KEYS[3],
      'initialized', '1',
      'requests', used_requests,
      'tokens', used_tokens)
    -- Migration during reconciliation may create an aggregate, but must not
    -- outlive the existing window. Admission separately installs its 61s TTL.
    local window_ttl = redis.call('PTTL', KEYS[1])
    if window_ttl > 0 then redis.call('PEXPIRE', KEYS[3], window_ttl) end
  end
end
local used_requests = tonumber(redis.call('HGET', KEYS[3], 'requests') or '0')
local used_tokens = tonumber(redis.call('HGET', KEYS[3], 'tokens') or '0')
-- Clamp drift defensively, but keep blocked polls read-only otherwise.
if used_requests < 0 or used_tokens < 0 then
  if used_requests < 0 then used_requests = 0 end
  if used_tokens < 0 then used_tokens = 0 end
  if redis.call('EXISTS', KEYS[3]) == 1 then
    redis.call('HSET', KEYS[3], 'requests', used_requests, 'tokens', used_tokens)
  end
end
"""

_ACQUIRE_RATE_BUDGET_LUA = _RATE_BUDGET_WINDOW_LUA + """
local max_requests = tonumber(ARGV[3])
local max_tokens = tonumber(ARGV[4])
local reserved_tokens = tonumber(ARGV[5])
local member = ARGV[6]
-- Stable candidate IDs make an admission retry safe if a response is lost.
if redis.call('ZSCORE', KEYS[1], member) ~= false then return {1, 0} end
local requests_blocked = max_requests > 0 and used_requests >= max_requests
local tokens_blocked = max_tokens > 0 and used_tokens + reserved_tokens > max_tokens
if requests_blocked or tokens_blocked then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  return {0, tonumber(oldest[2] or now) + 60 - now}
end
redis.call('ZADD', KEYS[1], now, member)
redis.call('HSET', KEYS[2], member, reserved_tokens)
redis.call('HSET', KEYS[3], 'initialized', '1')
redis.call('HINCRBY', KEYS[3], 'requests', 1)
redis.call('HINCRBY', KEYS[3], 'tokens', reserved_tokens)
if ARGV[7] == 'pending' then
  redis.call('HSET', KEYS[4], member, 'pending')
end
redis.call('EXPIRE', KEYS[1], 61)
redis.call('EXPIRE', KEYS[2], 61)
redis.call('EXPIRE', KEYS[3], 61)
if redis.call('EXISTS', KEYS[4]) == 1 then redis.call('EXPIRE', KEYS[4], 61) end
return {1, 0}
"""


_RECONCILE_RATE_BUDGET_LUA = _RATE_BUDGET_WINDOW_LUA + """
local member = ARGV[3]
local mode = ARGV[4]
local score = redis.call('ZSCORE', KEYS[1], member)
local previous_tokens = redis.call('HGET', KEYS[2], member)
if score == false or tonumber(score) <= cutoff or previous_tokens == false
    or redis.call('HGET', KEYS[4], member) ~= 'pending' then
  return 0
end
previous_tokens = tonumber(previous_tokens)
if mode == 'cancel' then
  redis.call('ZREM', KEYS[1], member)
  redis.call('HDEL', KEYS[2], member)
  redis.call('HDEL', KEYS[4], member)
  redis.call('HSET', KEYS[3],
    'requests', math.max(0, used_requests - 1),
    'tokens', math.max(0, used_tokens - previous_tokens))
else
  local actual_tokens = tonumber(ARGV[5])
  redis.call('HSET', KEYS[2], member, actual_tokens)
  redis.call('HSET', KEYS[4], member, 'settled')
  redis.call('HSET', KEYS[3], 'tokens', math.max(0, used_tokens - previous_tokens + actual_tokens))
end
-- Never change the request timestamp or extend any TTL during reconciliation.
return 1
"""


class RedisModelLimiter(_RateBudgetReservationMixin):
    """Cross-worker fair model limiter backed by Redis ZSET leases."""

    def __init__(
        self,
        redis_client,
        *,
        namespace: str = "mathagent:model-limiter:v2",
        lease_seconds: int = 300,
        max_hold_seconds: int | None = None,
        waiter_seconds: int = 10,
        acquire_timeout_seconds: float = 30,
        operation_timeout_seconds: float = 5,
        teacher_burst_capacity: int | None = None,
        global_reserve_capacity: int | None = None,
    ) -> None:
        if max_hold_seconds is None:
            max_hold_seconds = lease_seconds
        if max_hold_seconds < lease_seconds:
            raise ValueError("max_hold_seconds must be at least lease_seconds")
        if acquire_timeout_seconds <= 0:
            raise ValueError("acquire_timeout_seconds must be positive")
        if operation_timeout_seconds <= 0:
            raise ValueError("operation_timeout_seconds must be positive")
        self._redis = redis_client
        self._namespace = namespace.rstrip(":")
        self._lease_seconds = max(30, lease_seconds)
        self._max_hold_seconds = max_hold_seconds
        if self._max_hold_seconds < self._lease_seconds:
            raise ValueError("max_hold_seconds must be at least normalized lease_seconds")
        self._waiter_seconds = max(2, waiter_seconds)
        self._acquire_timeout_seconds = acquire_timeout_seconds
        self._operation_timeout_seconds = operation_timeout_seconds
        self._teacher_burst_capacity = teacher_burst_capacity
        self._global_reserve_capacity = global_reserve_capacity

    def _keys(
        self,
        provider: str,
        api_key: str,
        teacher_id: str,
    ) -> tuple[str, str, str, str]:
        provider = canonical_provider(provider)
        base = f"{self._namespace}:{provider}"
        normalized_teacher = teacher_fingerprint(provider, teacher_id)
        credential_hash = hashlib.sha256(
            f"{provider}\0{api_key}".encode("utf-8")
        ).hexdigest()
        return (
            f"{base}:leases",
            f"{base}:teacher:{normalized_teacher}",
            f"{base}:credential:{credential_hash}",
            f"{base}:waiters",
        )

    @staticmethod
    def teacher_fingerprint(provider: str, teacher_id: str) -> str:
        """Return the opaque teacher namespace used by Redis coordination."""
        return teacher_fingerprint(provider, teacher_id)

    def _rate_budget_keys(self, provider: str) -> tuple[str, str]:
        base = f"{self._namespace}:rate-budget:{canonical_provider(provider)}"
        return f"{base}:requests", f"{base}:tokens"

    def _rate_budget_totals_key(self, provider: str) -> str:
        base = f"{self._namespace}:rate-budget:{canonical_provider(provider)}"
        return f"{base}:totals"

    def _rate_budget_states_key(self, provider: str) -> str:
        base = f"{self._namespace}:rate-budget:{canonical_provider(provider)}"
        return f"{base}:states"

    def _credential_health_key(
        self,
        *,
        provider: str,
        api_key: str,
        base_url: str,
        model: str,
    ) -> str:
        provider = canonical_provider(provider)
        material = "\0".join((provider, base_url.rstrip("/"), model, api_key))
        fingerprint = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return f"{self._namespace}:health:{provider}:credential:{fingerprint}"

    def _provider_failure_key(self, provider: str) -> str:
        return f"{self._namespace}:health:{canonical_provider(provider)}:runtime-failure"

    def _credential_cooldown_key(
        self,
        *,
        provider: str,
        api_key: str,
        base_url: str,
        model: str,
    ) -> str:
        return self._credential_health_key(
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            model=model,
        ) + ":cooldown"

    async def _redis_call(self, awaitable):
        return await asyncio.wait_for(
            awaitable,
            timeout=self._operation_timeout_seconds,
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
        """Reserve one provider-wide rolling 60-second request/token slot."""
        await self._acquire_rate_budget(
            provider=provider,
            requests_per_minute=requests_per_minute,
            tokens_per_minute=tokens_per_minute,
            reserved_tokens=reserved_tokens,
            timeout_seconds=timeout_seconds,
        )

    async def _acquire_rate_budget(
        self,
        *,
        provider: str,
        requests_per_minute: float,
        tokens_per_minute: int,
        reserved_tokens: int,
        timeout_seconds: float,
        reservation: RateBudgetReservation | None = None,
    ) -> None:
        _validate_rate_budget(
            requests_per_minute=requests_per_minute,
            tokens_per_minute=tokens_per_minute,
            reserved_tokens=reserved_tokens,
            timeout_seconds=timeout_seconds,
        )
        if requests_per_minute <= 0 and tokens_per_minute <= 0:
            return
        requests, tokens = self._rate_budget_keys(provider)
        totals = self._rate_budget_totals_key(provider)
        states = self._rate_budget_states_key(provider)
        member = reservation.reservation_id if reservation else uuid.uuid4().hex
        deadline = time.monotonic() + timeout_seconds
        while True:
            now = time.time()
            granted, wait_seconds = await self._eval(
                _ACQUIRE_RATE_BUDGET_LUA,
                4,
                requests,
                tokens,
                totals,
                states,
                now,
                now - 60.0,
                int(math.floor(requests_per_minute)),
                int(tokens_per_minute),
                int(reserved_tokens),
                member,
                "pending" if reservation else "legacy",
            )
            if int(granted) == 1:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RateBudgetTimeout("timed out waiting for provider rate budget")
            await self._rate_budget_sleep(
                canonical_provider(provider), max(0.01, float(wait_seconds)), remaining,
                poll=reservation is not None,
            )

    async def settle_rate_budget(
        self, reservation: RateBudgetReservation, *, actual_tokens: int,
    ) -> bool:
        """Atomically replace the estimate once, even when usage exceeds it."""
        _validate_actual_tokens(actual_tokens)
        return await self._reconcile_rate_budget(reservation, actual_tokens=actual_tokens)

    async def cancel_rate_budget(self, reservation: RateBudgetReservation) -> bool:
        """Atomically refund RPM/TPM for a pending, untransmitted request."""
        return await self._reconcile_rate_budget(reservation, actual_tokens=None)

    async def _reconcile_rate_budget(
        self, reservation: RateBudgetReservation, *, actual_tokens: int | None,
    ) -> bool:
        _validate_reservation(reservation)
        provider = reservation.provider
        requests, tokens = self._rate_budget_keys(provider)
        now = time.time()
        changed = await self._eval(
            _RECONCILE_RATE_BUDGET_LUA, 4,
            requests, tokens, self._rate_budget_totals_key(provider),
            self._rate_budget_states_key(provider),
            now, now - 60, reservation.reservation_id,
            "cancel" if actual_tokens is None else "settle",
            0 if actual_tokens is None else actual_tokens,
        )
        return bool(int(changed))

    async def is_credential_invalid(
        self, *, provider: str, api_key: str, base_url: str, model: str,
    ) -> bool:
        key = self._credential_health_key(
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            model=model,
        )
        return bool(await self._redis_call(self._redis.exists(key)))

    async def mark_credential_invalid(
        self,
        *,
        provider: str,
        api_key: str,
        base_url: str,
        model: str,
        status_code: int,
    ) -> None:
        if status_code not in {401, 403}:
            raise ValueError("only HTTP 401/403 can invalidate a credential")
        key = self._credential_health_key(
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            model=model,
        )
        # No TTL is intentional. Credential rotation changes the fingerprint;
        # an invalid static credential must not silently re-enter after a timer.
        await self._redis_call(self._redis.set(key, str(status_code)))

    async def credential_cooldown_seconds(
        self, *, provider: str, api_key: str, base_url: str, model: str,
    ) -> float:
        key = self._credential_cooldown_key(
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            model=model,
        )
        ttl_ms = int(await self._redis_call(self._redis.pttl(key)))
        return max(0.0, ttl_ms / 1000.0)

    async def mark_credential_cooldown(
        self,
        *,
        provider: str,
        api_key: str,
        base_url: str,
        model: str,
        cooldown_seconds: float,
    ) -> None:
        cooldown = float(cooldown_seconds)
        if not 0 < cooldown < float("inf"):
            raise ValueError("credential cooldown must be finite and positive")
        key = self._credential_cooldown_key(
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            model=model,
        )
        await self._eval(
            _MARK_CREDENTIAL_COOLDOWN_LUA,
            1,
            key,
            max(1, int(cooldown * 1000 + 0.999)),
        )

    async def is_provider_failure(self, provider: str) -> bool:
        return bool(
            await self._redis_call(
                self._redis.exists(self._provider_failure_key(provider))
            )
        )

    async def mark_provider_failure(self, provider: str, cooldown_seconds: int) -> None:
        ttl = max(1, int(cooldown_seconds))
        await self._redis_call(
            self._redis.set(
                self._provider_failure_key(provider),
                "1",
                ex=ttl,
            )
        )

    async def clear_provider_failure(self, provider: str) -> None:
        await self._redis_call(
            self._redis.delete(self._provider_failure_key(provider))
        )

    async def _eval(self, script: str, numkeys: int, *args):
        """Bound one Redis round-trip so coordinator outages cannot hang a worker."""
        return await asyncio.wait_for(
            self._redis.eval(script, numkeys, *args),
            timeout=self._operation_timeout_seconds,
        )

    async def ping(self) -> None:
        """Fail closed when the production Redis coordinator is unavailable."""
        response = await asyncio.wait_for(
            self._redis.ping(),
            timeout=self._operation_timeout_seconds,
        )
        if not response:
            raise RuntimeError("Redis coordinator did not acknowledge PING")

    async def acquire(
        self,
        *,
        provider: str,
        api_key: str,
        teacher_id: str,
        global_capacity: int,
        credential_capacity: int | None = None,
        teacher_base_capacity: int,
        teacher_burst_capacity: int | None = None,
        global_reserve_capacity: int | None = None,
    ) -> ModelLease:
        burst_was_omitted = teacher_burst_capacity is None
        reserve_was_omitted = global_reserve_capacity is None
        if teacher_burst_capacity is None:
            teacher_burst_capacity = self._teacher_burst_capacity
        if global_reserve_capacity is None:
            # Route-specific defaults take precedence over the historical
            # generic reserve held by the limiter instance.  Active MiMo
            # routes therefore use reserve=0 even when a legacy caller built
            # the shared limiter with MODEL_GLOBAL_RESERVE_CONCURRENCY=30.
            route_reserve = provider_default_reserve_capacity(provider)
            global_reserve_capacity = (
                route_reserve
                if route_reserve is not None
                else self._global_reserve_capacity
            )
        (
            global_capacity,
            credential_capacity,
            teacher_base_capacity,
            teacher_burst_capacity,
        ) = _cap_provider_request(
            provider,
            global_capacity,
            credential_capacity,
            teacher_base_capacity,
            teacher_burst_capacity,
        )
        if (
            burst_was_omitted
            and reserve_was_omitted
            and global_reserve_capacity == 0
            and canonical_provider(provider) in {"dashscope", MIMO_SUBSCRIPTION_PROVIDER}
        ):
            teacher_burst_capacity = global_capacity
        if global_reserve_capacity is not None:
            global_reserve_capacity = min(
                int(global_reserve_capacity),
                max(0, global_capacity - teacher_base_capacity),
            )
        policy = FairSharePolicy(
            global_capacity,
            teacher_base_capacity,
            teacher_burst_capacity,
            global_reserve_capacity,
        )
        if credential_capacity is None:
            credential_capacity = policy.global_capacity
        if not 1 <= credential_capacity <= policy.global_capacity:
            raise ValueError(
                "credential_capacity must be between 1 and global capacity"
            )
        lease_id = uuid.uuid4().hex
        all_leases, teacher_leases, credential_leases, waiters = self._keys(
            provider, api_key, teacher_id
        )
        normalized_teacher = teacher_fingerprint(provider, teacher_id)
        waiter_id = f"{normalized_teacher}\x1f{lease_id}"
        global_member = waiter_id

        deadline = time.monotonic() + self._acquire_timeout_seconds
        try:
            while True:
                now = time.time()
                granted = await self._eval(
                    _ACQUIRE_LUA,
                    4,
                    all_leases,
                    teacher_leases,
                    credential_leases,
                    waiters,
                    now,
                    now + self._lease_seconds,
                    now + self._waiter_seconds,
                    policy.global_capacity,
                    policy.teacher_base_capacity,
                    policy.teacher_burst_capacity,
                    policy.global_reserve_capacity,
                    credential_capacity,
                    lease_id,
                    normalized_teacher,
                    "work_conserving" if canonical_provider(provider) == "dashscope" else "fair_share",
                )
                if int(granted) == 1:
                    break

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("timed out waiting for shared model capacity")
                await asyncio.sleep(min(0.1, remaining))
        except BaseException:
            # A timed-out/cancelled caller must not leave a fairness waiter that
            # delays capacity borrowing.  Its Lua TTL is a second backstop.
            try:
                await self._eval(
                    "return redis.call('ZREM', KEYS[1], ARGV[1])",
                    1,
                    waiters,
                    waiter_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("failed to remove model-capacity waiter", exc_info=True)
            raise

        async def release() -> None:
            await self._eval(
                _RELEASE_LUA,
                3,
                all_leases,
                teacher_leases,
                credential_leases,
                lease_id,
                global_member,
            )

        async def renew(expiry_epoch: float) -> bool:
            renewed = await self._eval(
                _RENEW_LUA,
                3,
                all_leases,
                teacher_leases,
                credential_leases,
                lease_id,
                time.time(),
                expiry_epoch,
                global_member,
            )
            return bool(int(renewed))

        return ModelLease(
            release,
            renew,
            lease_seconds=self._lease_seconds,
            max_hold_seconds=self._max_hold_seconds,
        )


_limiter: RedisModelLimiter | NoopModelLimiter | None = None


def get_model_limiter() -> RedisModelLimiter | NoopModelLimiter:
    """Build the configured limiter lazily after dotenv has loaded configuration."""
    global _limiter
    if _limiter is not None:
        return _limiter

    from config import (
        MODEL_LIMITER_ACQUIRE_TIMEOUT_SECONDS,
        MODEL_LIMITER_BACKEND,
        MODEL_LIMITER_LEASE_SECONDS,
        MODEL_LIMITER_MAX_HOLD_SECONDS,
        MODEL_LIMITER_REDIS_OPERATION_TIMEOUT_SECONDS,
        MODEL_GLOBAL_RESERVE_CONCURRENCY,
        MODEL_TEACHER_BURST_CONCURRENCY,
        REDIS_URL,
    )

    if MODEL_LIMITER_BACKEND != "redis":
        _limiter = NoopModelLimiter()
        return _limiter

    try:
        import redis.asyncio as redis_async
    except ImportError as exc:
        raise RuntimeError("MODEL_LIMITER_BACKEND=redis requires the redis package") from exc

    _limiter = RedisModelLimiter(
        redis_async.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=MODEL_LIMITER_REDIS_OPERATION_TIMEOUT_SECONDS,
            socket_timeout=MODEL_LIMITER_REDIS_OPERATION_TIMEOUT_SECONDS,
            health_check_interval=30,
        ),
        lease_seconds=MODEL_LIMITER_LEASE_SECONDS,
        max_hold_seconds=MODEL_LIMITER_MAX_HOLD_SECONDS,
        acquire_timeout_seconds=MODEL_LIMITER_ACQUIRE_TIMEOUT_SECONDS,
        operation_timeout_seconds=MODEL_LIMITER_REDIS_OPERATION_TIMEOUT_SECONDS,
        teacher_burst_capacity=MODEL_TEACHER_BURST_CONCURRENCY,
        global_reserve_capacity=MODEL_GLOBAL_RESERVE_CONCURRENCY,
    )
    return _limiter


async def ensure_redis_model_limiter_ready() -> None:
    """Fail FastAPI startup closed when Redis coordination is required but down."""
    from config import MODEL_LIMITER_BACKEND

    if MODEL_LIMITER_BACKEND != "redis":
        return
    limiter = get_model_limiter()
    if not isinstance(limiter, RedisModelLimiter):
        raise RuntimeError("Redis model limiter was not initialized")
    try:
        await limiter.ping()
    except Exception as exc:
        raise RuntimeError("Redis model coordinator is unavailable") from exc
