"""The cross-worker pool coordinator: token, capacity, circuit and retry state kept in Redis.

``RedisPoolCoordinator`` keeps the state ``LocalPoolCoordinator`` keeps in
memory, shared by every worker.  Each multi-key step is one Lua script, so it is
atomic: acquiring and releasing capacity (``_ACQUIRE_LUA``, ``_RELEASE_LUA``),
leaving the first-attempt queue, claiming and releasing a cross-pool retry,
recording a failure for the circuit, and claiming or abandoning the accelerator
probe.  Redis only ever receives opaque hashes, lease IDs and counters.

Moved unchanged from services/ocr_pool_scheduler.py, which still exposes these names.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
import uuid

from services.ocr_pool_scheduler_base import (
    AcquireDecision,
    PoolPermit,
    PoolSpec,
    _ACTIVE_PRIMARY_TTL_S,
)


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
