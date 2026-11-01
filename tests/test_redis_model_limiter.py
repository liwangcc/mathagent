import asyncio
import time

import pytest
import fakeredis.aioredis

import config
from services import dashscope_budget
from services.model_concurrency import (
    ModelLease,
    RateBudgetTimeout,
    RedisModelLimiter,
)


def test_credential_invalidity_and_runtime_circuit_are_shared_without_raw_key():
    async def run():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        limiter_a = RedisModelLimiter(redis, operation_timeout_seconds=1)
        limiter_b = RedisModelLimiter(redis, operation_timeout_seconds=1)
        credential = {
            "provider": "mimo",
            "api_key": "raw-secret-never-store-this",
            "base_url": "https://provider.example/v1",
            "model": "reasoning-model",
        }

        assert not await limiter_b.is_credential_invalid(**credential)
        await limiter_a.mark_credential_invalid(**credential, status_code=401)
        assert await limiter_b.is_credential_invalid(**credential)

        await limiter_a.mark_credential_cooldown(
            **credential,
            cooldown_seconds=30.0,
        )
        shared_cooldown = await limiter_b.credential_cooldown_seconds(**credential)
        assert 29.0 <= shared_cooldown <= 30.0

        # A concurrent worker with a shorter response must not erase the
        # provider's earlier Retry-After lower bound.
        await limiter_b.mark_credential_cooldown(
            **credential,
            cooldown_seconds=1.0,
        )
        preserved_cooldown = await limiter_a.credential_cooldown_seconds(**credential)
        assert 29.0 <= preserved_cooldown <= 30.0

        # Rotating any credential routing component creates a fresh fingerprint.
        assert not await limiter_b.is_credential_invalid(
            **{**credential, "api_key": "rotated-secret"}
        )
        assert await limiter_b.credential_cooldown_seconds(
            **{**credential, "api_key": "rotated-secret"}
        ) == 0.0
        redis_keys = await redis.keys("*")
        assert all(credential["api_key"] not in key for key in redis_keys)

        assert not await limiter_b.is_provider_failure("mimo")
        await limiter_a.mark_provider_failure("mimo", 60)
        assert await limiter_b.is_provider_failure("mimo")
        await limiter_b.clear_provider_failure("mimo")
        assert not await limiter_a.is_provider_failure("mimo")
        await redis.aclose()

    asyncio.run(run())


def test_provider_namespaces_and_teacher_buckets_are_isolated_and_opaque():
    async def run():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        limiter = RedisModelLimiter(
            redis,
            acquire_timeout_seconds=0.05,
            operation_timeout_seconds=1,
        )
        payg = limiter._keys("xiaomi_mimo_payg", "same-key", "teacher-1")
        subscription = limiter._keys(
            "xiaomi_mimo_subscription", "same-key", "teacher-1"
        )
        legacy_subscription = limiter._keys(
            "xiaomi_mimo", "same-key", "teacher-1"
        )
        assert payg[0] != subscription[0]
        assert payg[1] != subscription[1]
        assert payg[2] != subscription[2]
        assert payg[3] != subscription[3]
        assert legacy_subscription == subscription
        assert all("teacher-1" not in key for key in payg + subscription)
        assert all("same-key" not in key for key in payg + subscription)
        await redis.aclose()

    asyncio.run(run())


def test_paid_provider_rate_budget_is_shared_and_recovers_after_one_minute(monkeypatch):
    async def run():
        clock = {"epoch": 1_000.0}
        monkeypatch.setattr("services.model_concurrency.time.time", lambda: clock["epoch"])
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        limiter_a = RedisModelLimiter(redis, operation_timeout_seconds=1)
        limiter_b = RedisModelLimiter(redis, operation_timeout_seconds=1)
        kwargs = {
            "provider": "xiaomi_mimo_payg",
            "requests_per_minute": 2,
            "tokens_per_minute": 10,
            "reserved_tokens": 4,
        }

        await limiter_a.acquire_rate_budget(timeout_seconds=1, **kwargs)
        await limiter_b.acquire_rate_budget(timeout_seconds=1, **kwargs)
        with pytest.raises(RateBudgetTimeout):
            await limiter_a.acquire_rate_budget(timeout_seconds=0.01, **kwargs)

        clock["epoch"] += 61.0
        await limiter_b.acquire_rate_budget(timeout_seconds=1, **kwargs)
        keys = await redis.keys("*")
        assert all("test-only" not in key for key in keys)
        await redis.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("boundary", ["rpm", "tpm"])
def test_flash_rate_budget_admits_exact_configured_boundary(monkeypatch, boundary):
    """Seed a near-full window; real Lua admits the last unit, not one less.

    This is an accounting boundary test, not 30K real requests or a throughput
    benchmark. Low-token ideal input isolates RPM from the independent TPM gate.
    """
    monkeypatch.setattr(dashscope_budget, "MODEL_RATE_BUDGET_WAIT_SECONDS", 0.02)
    payload = {"model": "qwen3.8-flash", "max_tokens": 1, "messages": []}
    reserved = dashscope_budget.estimate_request_tokens(payload)
    rpm, tpm = config.DASHSCOPE_MODEL_RATE_LIMITS["qwen3.8-flash"]
    provider = dashscope_budget.budget_provider("qwen3.8-flash")

    async def run():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        worker_a = RedisModelLimiter(redis, operation_timeout_seconds=2)
        worker_b = RedisModelLimiter(redis, operation_timeout_seconds=2)
        requests, tokens = worker_a._rate_budget_keys(provider)
        totals = worker_a._rate_budget_totals_key(provider)
        if boundary == "rpm":
            existing_tokens = {f"seed-{index}": reserved for index in range(rpm - 1)}
        else:
            existing_tokens = {"seed-tpm": tpm - reserved}
        try:
            # Atomic seeding mirrors existing valid accounting, including the
            # aggregate used by the production O(1) steady-state Lua path.
            async with redis.pipeline(transaction=True) as pipe:
                pipe.zadd(requests, {member: time.time() for member in existing_tokens})
                pipe.hset(tokens, mapping=existing_tokens)
                pipe.hset(totals, mapping={
                    "initialized": 1, "requests": len(existing_tokens),
                    "tokens": sum(existing_tokens.values()),
                })
                await pipe.execute()
            await dashscope_budget.acquire(worker_a, model="qwen3.8-flash", payload=payload)
            assert await redis.zcard(requests) == len(existing_tokens) + 1
            reached = await redis.hgetall(totals)
            if boundary == "rpm":
                assert int(reached["requests"]) == rpm == 30_000
                assert int(reached["tokens"]) < tpm
            else:
                assert int(reached["tokens"]) == tpm == 5_000_000
                assert int(reached["requests"]) < rpm
            # A second process uses the same window, not another copy of quota.
            with pytest.raises(RateBudgetTimeout):
                await dashscope_budget.acquire(worker_b, model="qwen3.8-flash", payload=payload)
            assert await redis.hgetall(totals) == reached
        finally:
            await redis.aclose()

    asyncio.run(run())


def test_rate_budget_cleans_30000_expired_members_in_bounded_batches(monkeypatch):
    """A high-RPM window must not pass all expired members to one unpack call."""

    async def run():
        clock = {"epoch": 1_000.0}
        monkeypatch.setattr("services.model_concurrency.time.time", lambda: clock["epoch"])
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        limiter = RedisModelLimiter(redis, operation_timeout_seconds=2)
        requests_key, tokens_key = limiter._rate_budget_keys("dashscope")

        # 30K RPM is the current Qwen OCR quota. Populate one full old window
        # to exercise cleanup at the provider's actual upper bound.
        pipe = redis.pipeline()
        for index in range(30_000):
            member = f"expired-{index}"
            pipe.zadd(requests_key, {member: clock["epoch"] - 61})
            pipe.hset(tokens_key, member, 100)
        await pipe.execute()

        await limiter.acquire_rate_budget(
            provider="dashscope",
            requests_per_minute=30_000,
            tokens_per_minute=5_000_000,
            reserved_tokens=1,
            timeout_seconds=2,
        )

        assert await redis.zcard(requests_key) == 1
        assert await redis.hlen(tokens_key) == 1
        await redis.aclose()

    asyncio.run(run())


def test_redis_limiter_shares_capacity_prioritizes_waiting_teacher_and_hides_key():
    async def run():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        limiter_a = RedisModelLimiter(redis, lease_seconds=30, waiter_seconds=2)
        limiter_b = RedisModelLimiter(redis, lease_seconds=30, waiter_seconds=2)
        kwargs = {
            "provider": "mimo",
            "api_key": "raw-secret-key-must-never-appear-in-redis",
            "global_capacity": 2,
            "teacher_base_capacity": 1,
        }

        a_first = await limiter_a.acquire(teacher_id="teacher-a", **kwargs)
        await a_first.__aenter__()
        a_borrowed = await limiter_a.acquire(teacher_id="teacher-a", **kwargs)
        await a_borrowed.__aenter__()
        try:
            waiting_b = asyncio.create_task(limiter_b.acquire(teacher_id="teacher-b", **kwargs))
            await asyncio.sleep(0.15)
            assert not waiting_b.done(), "teacher-b should wait while capacity is fully borrowed"

            await a_borrowed.__aexit__(None, None, None)
            b_lease = await asyncio.wait_for(waiting_b, timeout=1)
            await b_lease.__aenter__()
            try:
                redis_keys = await redis.keys("*")
                assert all("raw-secret-key" not in key for key in redis_keys)
            finally:
                await b_lease.__aexit__(None, None, None)
        finally:
            await a_first.__aexit__(None, None, None)
            await redis.aclose()

    asyncio.run(run())


def test_provider_capacity_is_shared_across_api_keys_and_respects_burst_reserve():
    async def run():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        limiter = RedisModelLimiter(redis, lease_seconds=30, waiter_seconds=2)
        common = {
            "provider": "mimo",
            "global_capacity": 3,
            "teacher_base_capacity": 1,
            "teacher_burst_capacity": 2,
            "global_reserve_capacity": 1,
        }

        first = await limiter.acquire(
            teacher_id="teacher-a",
            api_key="key-a",
            **common,
        )
        await first.__aenter__()
        second = await limiter.acquire(
            teacher_id="teacher-a",
            api_key="key-b",
            **common,
        )
        await second.__aenter__()
        try:
            # The two credentials must land in one provider-wide lease set.
            first_keys = limiter._keys("mimo", "key-a", "teacher-a")
            second_keys = limiter._keys("mimo", "key-b", "teacher-a")
            assert first_keys[0] == second_keys[0]
            assert first_keys[2] != second_keys[2]
            assert first_keys[3] == second_keys[3]

            # A teacher can use its second burst slot, but the final reserved
            # global slot is unavailable to burst traffic (3 - reserve 1).
            waiting = asyncio.create_task(
                limiter.acquire(
                    teacher_id="teacher-a",
                    api_key="key-c",
                    **common,
                )
            )
            await asyncio.sleep(0.15)
            assert not waiting.done()

            await second.__aexit__(None, None, None)
            third = await asyncio.wait_for(waiting, timeout=1)
            await third.__aenter__()
            await third.__aexit__(None, None, None)
        finally:
            await first.__aexit__(None, None, None)
            await redis.aclose()

    asyncio.run(run())


def test_explicit_zero_reserve_allows_sole_teacher_to_fill_provider_capacity():
    async def run():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        limiter = RedisModelLimiter(
            redis,
            lease_seconds=30,
            waiter_seconds=2,
            operation_timeout_seconds=1,
        )
        common = {
            "provider": "deepseek",
            "global_capacity": 4,
            "teacher_base_capacity": 1,
            "teacher_burst_capacity": 4,
            "global_reserve_capacity": 0,
        }
        leases = []
        try:
            for index in range(4):
                lease = await limiter.acquire(
                    api_key="deepseek-key",
                    teacher_id="teacher-alone",
                    **common,
                )
                await lease.__aenter__()
                leases.append(lease)
            all_leases, _teacher, _credential, _waiters = limiter._keys(
                "deepseek", "deepseek-key", "teacher-alone"
            )
            assert await redis.zcard(all_leases) == 4
        finally:
            for lease in leases:
                await lease.__aexit__(None, None, None)
            await redis.aclose()

    asyncio.run(run())


def test_mimo_route_defaults_to_zero_reserve_when_capacity_policy_is_omitted():
    async def run():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        limiter = RedisModelLimiter(
            redis,
            lease_seconds=30,
            waiter_seconds=2,
            operation_timeout_seconds=1,
        )
        leases = []
        try:
            for _ in range(4):
                lease = await limiter.acquire(
                    provider="xiaomi_mimo_subscription",
                    api_key="subscription-key",
                    teacher_id="teacher-alone",
                    global_capacity=4,
                    teacher_base_capacity=1,
                )
                await lease.__aenter__()
                leases.append(lease)
            all_leases, _teacher, _credential, _waiters = limiter._keys(
                "xiaomi_mimo_subscription", "subscription-key", "teacher-alone"
            )
            assert await redis.zcard(all_leases) == 4
        finally:
            for lease in leases:
                await lease.__aexit__(None, None, None)
            await redis.aclose()

    asyncio.run(run())


def test_subscription_explicit_nonzero_reserve_remains_compatible():
    async def run():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        limiter = RedisModelLimiter(
            redis,
            lease_seconds=30,
            waiter_seconds=2,
            # The assertion below verifies the reserve policy, not the
            # production 30-second admission budget. Keep this probe short so
            # a full test run cannot spend half a minute on an expected wait.
            acquire_timeout_seconds=0.05,
            operation_timeout_seconds=1,
        )
        common = {
            "provider": "xiaomi_mimo_subscription",
            "api_key": "subscription-key",
            "teacher_id": "teacher-alone",
            "global_capacity": 4,
            "teacher_base_capacity": 1,
            "teacher_burst_capacity": 4,
            "global_reserve_capacity": 1,
        }
        leases = []
        try:
            for _ in range(3):
                lease = await limiter.acquire(**common)
                await lease.__aenter__()
                leases.append(lease)
            with pytest.raises(TimeoutError):
                await limiter.acquire(**common)
        finally:
            for lease in leases:
                await lease.__aexit__(None, None, None)
            await redis.aclose()

    asyncio.run(run())


def test_known_provider_hard_cap_clamps_oversized_callers():
    async def run():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        limiter = RedisModelLimiter(
            redis,
            acquire_timeout_seconds=0.05,
            operation_timeout_seconds=1,
        )
        leases = []
        try:
            for _ in range(8):
                lease = await limiter.acquire(
                    provider="xiaomi_mimo",
                    api_key="subscription-key",
                    teacher_id="teacher-alone",
                    global_capacity=500,
                    credential_capacity=500,
                    teacher_base_capacity=30,
                    teacher_burst_capacity=500,
                    global_reserve_capacity=30,
                )
                await lease.__aenter__()
                leases.append(lease)
            with pytest.raises(TimeoutError):
                await limiter.acquire(
                    provider="xiaomi_mimo",
                    api_key="subscription-key",
                    teacher_id="teacher-alone",
                    global_capacity=500,
                    credential_capacity=500,
                    teacher_base_capacity=30,
                    teacher_burst_capacity=500,
                    global_reserve_capacity=30,
                )
        finally:
            for lease in leases:
                await lease.__aexit__(None, None, None)
            await redis.aclose()

    asyncio.run(run())


def test_late_teacher_gets_a_turn_before_existing_teacher_can_burst_again():
    """A waiting teacher participates in the share before another burst grant."""

    async def run():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        limiter = RedisModelLimiter(
            redis,
            lease_seconds=30,
            waiter_seconds=2,
            acquire_timeout_seconds=1,
            operation_timeout_seconds=1,
        )
        common = {
            "provider": "mimo",
            "api_key": "secret",
            "global_capacity": 4,
            "teacher_base_capacity": 2,
            "teacher_burst_capacity": 4,
            "global_reserve_capacity": 0,
        }
        a_leases = []
        try:
            for _ in range(2):
                lease = await limiter.acquire(teacher_id="teacher-a", **common)
                await lease.__aenter__()
                a_leases.append(lease)

            # B receives the next slot and becomes an active participant. A is
            # then at its dynamic share, so its next request must wait.
            b_first = asyncio.create_task(
                limiter.acquire(teacher_id="teacher-b", **common)
            )
            b_lease = await asyncio.wait_for(b_first, timeout=1)
            await b_lease.__aenter__()
            try:
                a_third = asyncio.create_task(
                    limiter.acquire(teacher_id="teacher-a", **common)
                )
                await asyncio.sleep(0.05)
                assert not a_third.done()
            finally:
                await b_lease.__aexit__(None, None, None)
                a_third.cancel()
                await asyncio.gather(a_third, return_exceptions=True)
        finally:
            for lease in a_leases:
                await lease.__aexit__(None, None, None)
            await redis.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(("teacher_count", "capacity"), [(2, 6), (3, 9)])
def test_waiting_teachers_share_all_available_provider_slots(teacher_count, capacity):
    """A registered queue is admitted in equal turns without stranded slots."""

    async def run():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        limiter = RedisModelLimiter(
            redis,
            lease_seconds=30,
            waiter_seconds=2,
            acquire_timeout_seconds=2,
            operation_timeout_seconds=1,
        )
        common = {
            "provider": "mimo",
            "api_key": "secret",
            "global_capacity": capacity,
            "teacher_base_capacity": 1,
            "teacher_burst_capacity": capacity,
            "global_reserve_capacity": 0,
        }
        # Fill every provider slot before registering waiters.  A single
        # blocker leaves capacity-1 immediately grantable, so most tasks can
        # skip the waiter ZSET and the test no longer observes a fair queue.
        blocker_leases = []
        for index in range(capacity):
            # Distinct blocker identities let the fair-share coordinator fill
            # the pool itself; one teacher is intentionally capped below the
            # full capacity while it is the sole participant.
            blocker = await limiter.acquire(
                teacher_id=f"blocker-{index}",
                **common,
            )
            await blocker.__aenter__()
            blocker_leases.append(blocker)
        tasks = [
            asyncio.create_task(
                limiter.acquire(teacher_id=f"teacher-{index}", **common)
            )
            for index in range(teacher_count)
            for _ in range(capacity // teacher_count)
        ]
        leases = []
        try:
            _all, _teacher, _credential, waiters = limiter._keys(
                "mimo", "secret", "teacher-0"
            )
            for _ in range(100):
                if await redis.zcard(waiters) == len(tasks):
                    break
                await asyncio.sleep(0.01)
            assert await redis.zcard(waiters) == len(tasks)

            # Releasing the blockers leaves all teachers registered at once;
            # the Lua admission rule must then rotate at the minimum active
            # count instead of allowing one teacher to consume the pool.
            for blocker in blocker_leases:
                await blocker.__aexit__(None, None, None)
            results = await asyncio.gather(*tasks)
            for lease in results:
                await lease.__aenter__()
                leases.append(lease)

            assert len(leases) == capacity
            members = await redis.zrange(_all, 0, -1)
            counts = {
                teacher: sum(
                    member.startswith(
                        f"{limiter.teacher_fingerprint('mimo', teacher)}{chr(31)}"
                    )
                    for member in members
                )
                for teacher in (f"teacher-{i}" for i in range(teacher_count))
            }
            assert counts == {
                f"teacher-{i}": capacity // teacher_count
                for i in range(teacher_count)
            }
        finally:
            for lease in leases:
                await lease.__aexit__(None, None, None)
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # The blockers are normally released before the assertion.  Keep
            # cleanup idempotent if an assertion fires earlier.
            for blocker in blocker_leases:
                await blocker.__aexit__(None, None, None)
            await redis.aclose()

    asyncio.run(run())


def test_credential_capacity_is_shared_across_workers_without_storing_raw_keys():
    async def run():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        limiter_a = RedisModelLimiter(
            redis,
            lease_seconds=30,
            acquire_timeout_seconds=0.05,
            operation_timeout_seconds=1,
        )
        limiter_b = RedisModelLimiter(
            redis,
            lease_seconds=30,
            acquire_timeout_seconds=0.05,
            operation_timeout_seconds=1,
        )
        common = {
            "provider": "xiaomi_mimo",
            "global_capacity": 4,
            "credential_capacity": 1,
            "teacher_base_capacity": 4,
            "teacher_burst_capacity": 4,
            "global_reserve_capacity": 0,
        }
        raw_key_a = "raw-key-a-must-never-reach-redis"
        raw_key_b = "raw-key-b-must-never-reach-redis"

        first = await limiter_a.acquire(
            api_key=raw_key_a,
            teacher_id="teacher-a",
            **common,
        )
        await first.__aenter__()
        try:
            with pytest.raises(TimeoutError):
                await limiter_b.acquire(
                    api_key=raw_key_a,
                    teacher_id="teacher-b",
                    **common,
                )

            # Another healthy credential keeps its independent shared slot.
            second = await limiter_b.acquire(
                api_key=raw_key_b,
                teacher_id="teacher-b",
                **common,
            )
            await second.__aenter__()
            await second.__aexit__(None, None, None)

            first_keys = limiter_a._keys(
                "xiaomi_mimo", raw_key_a, "teacher-a"
            )
            second_keys = limiter_b._keys(
                "xiaomi_mimo", raw_key_b, "teacher-b"
            )
            assert first_keys[0] == second_keys[0]
            assert first_keys[2] != second_keys[2]
            assert await redis.zcard(first_keys[2]) == 1
            redis_keys = await redis.keys("*")
            assert all(raw_key_a not in key and raw_key_b not in key for key in redis_keys)
        finally:
            await first.__aexit__(None, None, None)
            await redis.aclose()

    asyncio.run(run())


def test_credential_lease_is_renewed_and_released_with_other_capacity_buckets():
    async def run():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        limiter = RedisModelLimiter(
            redis,
            lease_seconds=30,
            operation_timeout_seconds=1,
        )
        lease = await limiter.acquire(
            provider="mimo",
            api_key="secret",
            teacher_id="teacher-a",
            global_capacity=2,
            credential_capacity=1,
            teacher_base_capacity=1,
        )
        all_leases, teacher_leases, credential_leases, _waiters = limiter._keys(
            "mimo", "secret", "teacher-a"
        )
        expiry = __import__("time").time() + 45
        assert await lease._renew(expiry) is True
        assert {
            await redis.zscore(all_leases, next(iter(await redis.zrange(all_leases, 0, -1)))),
            await redis.zscore(teacher_leases, next(iter(await redis.zrange(teacher_leases, 0, -1)))),
            await redis.zscore(credential_leases, next(iter(await redis.zrange(credential_leases, 0, -1)))),
        } == {expiry}
        ttls = [
            await redis.pttl(key)
            for key in (all_leases, teacher_leases, credential_leases)
        ]
        assert all(ttl > 0 for ttl in ttls)

        await lease.__aexit__(None, None, None)
        assert await redis.zcard(all_leases) == 0
        assert await redis.zcard(teacher_leases) == 0
        assert await redis.zcard(credential_leases) == 0
        await redis.aclose()

    asyncio.run(run())


def test_lease_does_not_start_a_watchdog_when_one_ttl_is_the_hard_limit():
    async def run():
        release_calls = 0

        async def release():
            nonlocal release_calls
            release_calls += 1

        async def renew(_expiry_epoch: float) -> bool:
            raise AssertionError("a one-TTL lease must not renew")

        lease = ModelLease(release, renew, lease_seconds=30, max_hold_seconds=30)
        async with lease:
            assert lease._renew_task is None

        assert release_calls == 1

    asyncio.run(run())


def test_lease_watchdog_stops_at_finite_hard_deadline(monkeypatch):
    async def run():
        renewals = []

        async def release():
            return None

        async def renew(expiry_epoch: float) -> bool:
            renewals.append(expiry_epoch)
            return True

        clock = {"monotonic": 0.0, "epoch": 1_000.0}

        def monotonic():
            return clock["monotonic"]

        def epoch():
            return clock["epoch"]

        original_sleep = asyncio.sleep

        async def advance_clock(seconds: float):
            clock["monotonic"] += seconds
            clock["epoch"] += seconds
            await original_sleep(0)

        monkeypatch.setattr("services.model_concurrency.time.monotonic", monotonic)
        monkeypatch.setattr("services.model_concurrency.time.time", epoch)
        monkeypatch.setattr("services.model_concurrency.asyncio.sleep", advance_clock)
        lease = ModelLease(release, renew, lease_seconds=1, max_hold_seconds=2)
        await lease.__aenter__()
        await original_sleep(0)
        await original_sleep(0)
        await lease._renew_task
        await lease.__aexit__(None, None, None)

        # Renewal reaches, but never crosses, the fixed hard deadline at 1002.
        assert renewals == [1_002.0]

    asyncio.run(run())


def test_redis_acquire_times_out_and_removes_its_waiter():
    async def run():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        limiter = RedisModelLimiter(
            redis,
            lease_seconds=30,
            acquire_timeout_seconds=0.05,
            operation_timeout_seconds=1,
        )
        kwargs = {
            "provider": "mimo",
            "api_key": "secret",
            "global_capacity": 1,
            "teacher_base_capacity": 1,
        }
        first = await limiter.acquire(teacher_id="teacher-a", **kwargs)
        await first.__aenter__()
        try:
            with pytest.raises(TimeoutError):
                await limiter.acquire(teacher_id="teacher-b", **kwargs)
            _all_leases, _teacher_leases, _credential_leases, waiters = limiter._keys(
                "mimo", "secret", "teacher-b"
            )
            assert await redis.zcard(waiters) == 0
        finally:
            await first.__aexit__(None, None, None)
            await redis.aclose()

    asyncio.run(run())


def test_same_teacher_concurrent_waiters_keep_distinct_fairness_entries():
    async def run():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        limiter = RedisModelLimiter(
            redis,
            lease_seconds=30,
            acquire_timeout_seconds=1,
            operation_timeout_seconds=1,
        )
        kwargs = {
            "provider": "mimo",
            "api_key": "secret",
            "global_capacity": 1,
            "teacher_base_capacity": 1,
        }
        first = await limiter.acquire(teacher_id="teacher-a", **kwargs)
        await first.__aenter__()
        waiting = [
            asyncio.create_task(limiter.acquire(teacher_id="teacher-b", **kwargs))
            for _ in range(2)
        ]
        try:
            _all_leases, _teacher_leases, _credential_leases, waiters = limiter._keys(
                "mimo", "secret", "teacher-b"
            )
            for _ in range(50):
                if await redis.zcard(waiters) == 2:
                    break
                await asyncio.sleep(0.01)
            assert await redis.zcard(waiters) == 2
        finally:
            for task in waiting:
                task.cancel()
            await asyncio.gather(*waiting, return_exceptions=True)
            assert await redis.zcard(waiters) == 0
            await first.__aexit__(None, None, None)
            await redis.aclose()

    asyncio.run(run())


def test_unreleased_lease_is_reclaimed_after_its_bounded_ttl(monkeypatch):
    """A crashed worker cannot keep Redis model capacity forever."""

    async def run():
        clock = {"epoch": 1_000.0}
        monkeypatch.setattr("services.model_concurrency.time.time", lambda: clock["epoch"])
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        limiter = RedisModelLimiter(
            redis,
            lease_seconds=30,
            max_hold_seconds=30,
            operation_timeout_seconds=1,
        )
        kwargs = {
            "provider": "mimo",
            "api_key": "secret",
            "global_capacity": 1,
            "teacher_base_capacity": 1,
        }

        # Acquire but intentionally omit __aexit__, equivalent to a worker
        # crash after taking the permit. A later acquire cleans expired scores.
        await limiter.acquire(teacher_id="teacher-a", **kwargs)
        clock["epoch"] += 31
        successor = await limiter.acquire(teacher_id="teacher-b", **kwargs)
        await successor.__aenter__()
        await successor.__aexit__(None, None, None)
        await redis.aclose()

    asyncio.run(run())
