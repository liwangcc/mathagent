"""Cross-worker teacher fairness for OCR batch waves.

Only HMAC teacher hashes, opaque wave IDs, timestamps, and leases are coordinated.
No account identifiers, images, prompts, or student data are stored in Redis.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import math
import time
import uuid
from dataclasses import dataclass, field


logger = logging.getLogger(__name__)


@dataclass
class TeacherWavePermit:
    coordinator: object
    teacher_hash: str
    wave_id: str
    lease_token: str
    _released: bool = False
    _renew_task: asyncio.Task[None] | None = field(init=False, default=None, repr=False)
    _release_lock: asyncio.Lock = field(init=False, repr=False)
    _deadline_monotonic: float = field(init=False, repr=False)
    _deadline_epoch: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._release_lock = asyncio.Lock()
        self._deadline_monotonic = time.monotonic() + self.coordinator.max_hold_s
        self._deadline_epoch = time.time() + self.coordinator.max_hold_s
        self._renew_task = asyncio.create_task(self._renew_loop())

    async def _renew_loop(self) -> None:
        interval_s = max(0.001, float(self.coordinator.lease_s) / 3.0)
        while True:
            remaining = self._deadline_monotonic - time.monotonic()
            if remaining <= 0:
                logger.warning("OCR teacher wave reached its maximum hold time")
                return
            await asyncio.sleep(min(interval_s, remaining))
            if self._deadline_monotonic - time.monotonic() <= 0:
                logger.warning("OCR teacher wave reached its maximum hold time")
                return
            try:
                renewed = await self.coordinator.renew_wave(
                    self.teacher_hash,
                    self.wave_id,
                    self.lease_token,
                    expiry_epoch=min(
                        time.time() + float(self.coordinator.lease_s),
                        self._deadline_epoch,
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # A transient coordinator failure still leaves time for a later
                # heartbeat. An expired lease cannot be revived by renew_wave.
                logger.warning(
                    "failed to renew OCR teacher wave lease; will retry",
                    exc_info=True,
                )
                continue
            if not renewed:
                logger.warning("OCR teacher wave lease expired before release")
                return

    async def release(self) -> None:
        async with self._release_lock:
            if self._released:
                return
            renew_task = self._renew_task
            self._renew_task = None
            if renew_task is not None:
                renew_task.cancel()
                await asyncio.gather(renew_task, return_exceptions=True)
            await self.coordinator.release_wave(
                self.teacher_hash,
                self.wave_id,
                self.lease_token,
            )
            self._released = True


class LocalTeacherWaveCoordinator:
    def __init__(
        self,
        *,
        active_limit: int,
        lease_s: float = 600.0,
        max_hold_s: float | None = None,
        queue_lease_s: float = 15.0,
        first_wave_priority_grace_s: float = 15.0,
    ) -> None:
        if active_limit < 1:
            raise ValueError("active teacher limit must be positive")
        if not math.isfinite(lease_s) or lease_s <= 0:
            raise ValueError("teacher wave lease must be finite and positive")
        if max_hold_s is None:
            max_hold_s = lease_s * 2.0
        if not math.isfinite(max_hold_s) or max_hold_s < lease_s:
            raise ValueError("teacher wave maximum hold must cover at least one lease")
        if (
            not math.isfinite(first_wave_priority_grace_s)
            or first_wave_priority_grace_s < 0
        ):
            raise ValueError("first-wave priority grace must be finite and nonnegative")
        self.active_limit = active_limit
        self.lease_s = lease_s
        self.max_hold_s = max_hold_s
        self.queue_lease_s = queue_lease_s
        self.first_wave_priority_grace_s = first_wave_priority_grace_s
        self._lock = asyncio.Lock()
        self._active: dict[str, tuple[str, str, float]] = {}
        self._queue: dict[str, tuple[float, float, str, float]] = {}

    def _prune(self, now: float) -> None:
        self._active = {
            teacher: value for teacher, value in self._active.items() if value[2] > now
        }
        self._queue = {
            wave: value for wave, value in self._queue.items() if value[3] > now
        }

    async def try_acquire_wave(
        self, teacher_hash: str, wave_id: str, *, first_wave: bool
    ) -> TeacherWavePermit | None:
        async with self._lock:
            now = time.time()
            self._prune(now)
            existing = self._queue.get(wave_id)
            self._queue[wave_id] = (
                existing[0]
                if existing
                else now + (0.0 if first_wave else self.first_wave_priority_grace_s),
                existing[1] if existing else now,
                teacher_hash,
                now + self.queue_lease_s,
            )
            ordered = sorted(self._queue, key=lambda item: self._queue[item][:2])
            free = max(0, self.active_limit - len(self._active))
            if wave_id not in ordered[:free] or teacher_hash in self._active:
                return None
            self._queue.pop(wave_id, None)
            lease_token = uuid.uuid4().hex
            self._active[teacher_hash] = (
                wave_id,
                lease_token,
                now + self.lease_s,
            )
            return TeacherWavePermit(self, teacher_hash, wave_id, lease_token)

    async def cancel_wave(self, wave_id: str) -> None:
        async with self._lock:
            self._queue.pop(wave_id, None)

    async def release_wave(
        self,
        teacher_hash: str,
        wave_id: str,
        lease_token: str,
    ) -> None:
        async with self._lock:
            current = self._active.get(teacher_hash)
            if (
                current is not None
                and current[0] == wave_id
                and current[1] == lease_token
            ):
                self._active.pop(teacher_hash, None)

    async def renew_wave(
        self,
        teacher_hash: str,
        wave_id: str,
        lease_token: str,
        *,
        expiry_epoch: float | None = None,
    ) -> bool:
        async with self._lock:
            now = time.time()
            current = self._active.get(teacher_hash)
            if (
                current is None
                or current[0] != wave_id
                or current[1] != lease_token
                or current[2] <= now
            ):
                self._prune(now)
                return False
            requested_expiry = (
                now + self.lease_s
                if expiry_epoch is None
                else min(float(expiry_epoch), now + self.lease_s)
            )
            if not math.isfinite(requested_expiry) or requested_expiry <= now:
                self._prune(now)
                return False
            self._active[teacher_hash] = (
                wave_id,
                lease_token,
                max(current[2], requested_expiry),
            )
            return True

    async def availability(self, teacher_hash: str) -> str:
        async with self._lock:
            self._prune(time.time())
            if teacher_hash in self._active:
                return "available"
            free = max(0, self.active_limit - len(self._active))
            if len(self._queue) < free:
                return "available"
            return "queued"

    async def close(self) -> None:
        return None


_ACQUIRE_WAVE_LUA = r"""
local now = tonumber(ARGV[1])
local teacher_hash = ARGV[2]
local wave_id = ARGV[3]
local lease_token = ARGV[4]
local priority_delay = tonumber(ARGV[5])
local active_limit = tonumber(ARGV[6])
local lease_expiry = tonumber(ARGV[7])
local ttl = tonumber(ARGV[8])
local queue_expiry = tonumber(ARGV[9])

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
local expired = redis.call('ZRANGEBYSCORE', KEYS[4], '-inf', now)
for _, expired_wave in ipairs(expired) do redis.call('ZREM', KEYS[2], expired_wave) end
redis.call('ZREMRANGEBYSCORE', KEYS[4], '-inf', now)
local owner = redis.call('GET', KEYS[3])
if owner ~= false and redis.call('ZSCORE', KEYS[1], teacher_hash) == false then
  redis.call('DEL', KEYS[3])
  owner = false
end
local score = now + priority_delay
local existing_score = redis.call('ZSCORE', KEYS[2], wave_id)
-- Migrate continuation waves queued by the former priority*1e13 scheme.
if existing_score == false or tonumber(existing_score) >= 10000000000000 then
  redis.call('ZADD', KEYS[2], score, wave_id)
end
redis.call('ZADD', KEYS[4], queue_expiry, wave_id)
redis.call('EXPIRE', KEYS[2], ttl)
redis.call('EXPIRE', KEYS[4], ttl)
local rank = redis.call('ZRANK', KEYS[2], wave_id)
local free = active_limit - redis.call('ZCARD', KEYS[1])
if rank == false or free <= 0 or rank >= free or owner ~= false then return 0 end

redis.call('ZREM', KEYS[2], wave_id)
redis.call('ZREM', KEYS[4], wave_id)
redis.call('SET', KEYS[3], lease_token, 'EX', ttl)
redis.call('ZADD', KEYS[1], lease_expiry, teacher_hash)
redis.call('EXPIRE', KEYS[1], ttl)
return 1
"""

_RELEASE_WAVE_LUA = r"""
if redis.call('GET', KEYS[2]) ~= ARGV[3] then return 0 end
redis.call('DEL', KEYS[2])
redis.call('ZREM', KEYS[1], ARGV[1])
return 1
"""

_RENEW_WAVE_LUA = r"""
local teacher_hash = ARGV[1]
local wave_id = ARGV[2]
local lease_token = ARGV[3]
local now = tonumber(ARGV[4])
local requested_expiry = tonumber(ARGV[5])
local ttl = tonumber(ARGV[6])
local current_expiry = redis.call('ZSCORE', KEYS[1], teacher_hash)
if redis.call('GET', KEYS[2]) ~= lease_token then return 0 end
if current_expiry == false or tonumber(current_expiry) <= now then return 0 end
redis.call('ZADD', KEYS[1], math.max(tonumber(current_expiry), requested_expiry), teacher_hash)
redis.call('EXPIRE', KEYS[1], ttl)
redis.call('EXPIRE', KEYS[2], ttl)
return 1
"""

_CANCEL_WAVE_LUA = r"""
local removed = redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[1])
return removed
"""

_AVAILABILITY_LUA = r"""
local now = tonumber(ARGV[1])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
local expired = redis.call('ZRANGEBYSCORE', KEYS[3], '-inf', now)
for _, expired_wave in ipairs(expired) do redis.call('ZREM', KEYS[2], expired_wave) end
redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', now)
if redis.call('ZSCORE', KEYS[1], ARGV[2]) ~= false then return 1 end
local free = tonumber(ARGV[3]) - redis.call('ZCARD', KEYS[1])
if free > redis.call('ZCARD', KEYS[2]) then return 1 end
return 0
"""

class RedisTeacherWaveCoordinator:
    def __init__(
        self,
        redis_client,
        *,
        namespace: str,
        active_limit: int,
        operation_timeout_s: float,
        lease_s: float = 600.0,
        max_hold_s: float | None = None,
        queue_lease_s: float = 15.0,
        first_wave_priority_grace_s: float = 15.0,
    ) -> None:
        if active_limit < 1:
            raise ValueError("active teacher limit must be positive")
        if not math.isfinite(lease_s) or lease_s <= 0:
            raise ValueError("teacher wave lease must be finite and positive")
        if max_hold_s is None:
            max_hold_s = lease_s * 2.0
        if not math.isfinite(max_hold_s) or max_hold_s < lease_s:
            raise ValueError("teacher wave maximum hold must cover at least one lease")
        if (
            not math.isfinite(first_wave_priority_grace_s)
            or first_wave_priority_grace_s < 0
        ):
            raise ValueError("first-wave priority grace must be finite and nonnegative")
        self._redis = redis_client
        self._namespace = namespace.rstrip(":")
        self.active_limit = active_limit
        self.operation_timeout_s = operation_timeout_s
        self.lease_s = lease_s
        self.max_hold_s = max_hold_s
        self.queue_lease_s = queue_lease_s
        self.first_wave_priority_grace_s = first_wave_priority_grace_s

    async def _eval(self, script: str, numkeys: int, *args):
        return await asyncio.wait_for(
            self._redis.eval(script, numkeys, *args), timeout=self.operation_timeout_s
        )

    def _teacher_key(self, teacher_hash: str) -> str:
        return f"{self._namespace}:teacher:{teacher_hash}:wave"

    async def try_acquire_wave(
        self, teacher_hash: str, wave_id: str, *, first_wave: bool
    ) -> TeacherWavePermit | None:
        now = time.time()
        lease_token = uuid.uuid4().hex
        ttl = max(60, math.ceil(self.lease_s))
        granted = await self._eval(
            _ACQUIRE_WAVE_LUA,
            4,
            f"{self._namespace}:active",
            f"{self._namespace}:queue",
            self._teacher_key(teacher_hash),
            f"{self._namespace}:queue-expiry",
            now,
            teacher_hash,
            wave_id,
            lease_token,
            0.0 if first_wave else self.first_wave_priority_grace_s,
            self.active_limit,
            now + self.lease_s,
            ttl,
            now + self.queue_lease_s,
        )
        if not int(granted):
            return None
        return TeacherWavePermit(self, teacher_hash, wave_id, lease_token)

    async def cancel_wave(self, wave_id: str) -> None:
        await self._eval(
            _CANCEL_WAVE_LUA,
            2,
            f"{self._namespace}:queue",
            f"{self._namespace}:queue-expiry",
            wave_id,
        )

    async def release_wave(
        self,
        teacher_hash: str,
        wave_id: str,
        lease_token: str,
    ) -> None:
        await self._eval(
            _RELEASE_WAVE_LUA,
            2,
            f"{self._namespace}:active",
            self._teacher_key(teacher_hash),
            teacher_hash,
            wave_id,
            lease_token,
        )

    async def renew_wave(
        self,
        teacher_hash: str,
        wave_id: str,
        lease_token: str,
        *,
        expiry_epoch: float | None = None,
    ) -> bool:
        now = time.time()
        requested_expiry = (
            now + self.lease_s
            if expiry_epoch is None
            else min(float(expiry_epoch), now + self.lease_s)
        )
        if not math.isfinite(requested_expiry) or requested_expiry <= now:
            return False
        renewed = await self._eval(
            _RENEW_WAVE_LUA,
            2,
            f"{self._namespace}:active",
            self._teacher_key(teacher_hash),
            teacher_hash,
            wave_id,
            lease_token,
            now,
            requested_expiry,
            max(60, math.ceil(self.lease_s)),
        )
        return bool(int(renewed))

    async def availability(self, teacher_hash: str) -> str:
        available = await self._eval(
            _AVAILABILITY_LUA,
            3,
            f"{self._namespace}:active",
            f"{self._namespace}:queue",
            f"{self._namespace}:queue-expiry",
            time.time(),
            teacher_hash,
            self.active_limit,
        )
        return "available" if int(available) else "queued"

    async def close(self) -> None:
        await self._redis.aclose()


_coordinator = None
_coordinator_loop = None


def teacher_hash(teacher_id: str) -> str:
    from config import JWT_SECRET

    raw = str(teacher_id or "").strip()
    if not raw:
        raise ValueError("teacher identity is required")
    return hmac.new(JWT_SECRET.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()[:24]


def wave_id(batch_id: str, wave_index: int) -> str:
    return hashlib.sha256(f"{batch_id}:{wave_index}".encode("utf-8")).hexdigest()[:24]


def get_teacher_wave_coordinator():
    global _coordinator, _coordinator_loop
    current_loop = asyncio.get_running_loop()
    if _coordinator is not None and _coordinator_loop is current_loop:
        return _coordinator

    import config

    if not config.OCR_MULTI_POOL_ENABLED or config.OCR_MULTI_POOL_COORDINATOR == "local":
        _coordinator = LocalTeacherWaveCoordinator(
            active_limit=config.OCR_ACTIVE_TEACHER_LIMIT,
            first_wave_priority_grace_s=config.OCR_FIRST_WAVE_PRIORITY_GRACE_S,
        )
    else:
        import redis.asyncio as redis_async

        client = redis_async.from_url(
            config.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=config.MODEL_LIMITER_REDIS_OPERATION_TIMEOUT_SECONDS,
            socket_timeout=config.MODEL_LIMITER_REDIS_OPERATION_TIMEOUT_SECONDS,
            health_check_interval=30,
        )
        _coordinator = RedisTeacherWaveCoordinator(
            client,
            namespace="mathagent:ocr-teacher-waves:v1",
            active_limit=config.OCR_ACTIVE_TEACHER_LIMIT,
            operation_timeout_s=config.MODEL_LIMITER_REDIS_OPERATION_TIMEOUT_SECONDS,
            first_wave_priority_grace_s=config.OCR_FIRST_WAVE_PRIORITY_GRACE_S,
        )
    _coordinator_loop = current_loop
    return _coordinator


async def acquire_teacher_wave(
    teacher_id: str,
    batch_id: str,
    wave_index: int,
    *,
    poll_s: float = 0.1,
) -> TeacherWavePermit:
    coordinator = get_teacher_wave_coordinator()
    teacher = teacher_hash(teacher_id)
    opaque_wave = wave_id(batch_id, wave_index)
    try:
        while True:
            permit = await coordinator.try_acquire_wave(
                teacher, opaque_wave, first_wave=wave_index == 0
            )
            if permit is not None:
                return permit
            await asyncio.sleep(poll_s)
    except BaseException:
        await coordinator.cancel_wave(opaque_wave)
        raise


async def public_teacher_availability(_teacher_id: str) -> dict[str, str]:
    """Report readiness without reintroducing the retired whole-wave gate.

    Student Flash admission is per request in ``model_concurrency``. Legacy
    coordinator state may still be queried by compatibility callers, but it
    must not turn an old ``OCR_MULTI_POOL_ENABLED`` environment value into a
    four-teacher submit cap or a hidden pre-submit queue.
    """
    return {"status": "available", "label": "可用"}


async def reset_teacher_wave_coordinator_for_tests() -> None:
    global _coordinator, _coordinator_loop
    current = _coordinator
    _coordinator = None
    _coordinator_loop = None
    if current is not None:
        await current.close()


async def ensure_teacher_wave_coordinator_ready() -> None:
    import config

    if not config.OCR_MULTI_POOL_ENABLED:
        return
    coordinator = get_teacher_wave_coordinator()
    if isinstance(coordinator, RedisTeacherWaveCoordinator):
        response = await asyncio.wait_for(
            coordinator._redis.ping(), timeout=coordinator.operation_timeout_s
        )
        if not response:
            raise RuntimeError("OCR teacher wave coordinator did not acknowledge PING")
