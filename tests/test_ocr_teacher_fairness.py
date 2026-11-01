import asyncio
from unittest.mock import patch

import fakeredis.aioredis

from services.ocr_teacher_fairness import (
    LocalTeacherWaveCoordinator,
    RedisTeacherWaveCoordinator,
)
from services.ocr_pool_scheduler import LocalPoolCoordinator


def test_local_pool_coordinator_enforces_per_teacher_pool_limit():
    from tests.test_ocr_pool_scheduler import _pool

    async def run():
        coordinator = LocalPoolCoordinator(global_rpm=60000, global_burst=100)
        pool = _pool("ys", hard=60)
        teacher_a = []
        for _ in range(20):
            decision = await coordinator.try_acquire(
                pool,
                capacity_limit=60,
                borrowed=False,
                use_global_gate=True,
                teacher_hash="teacher-a-hash",
                teacher_limit=20,
            )
            assert decision.permit is not None
            teacher_a.append(decision.permit)
        blocked = await coordinator.try_acquire(
            pool,
            capacity_limit=60,
            borrowed=False,
            use_global_gate=True,
            teacher_hash="teacher-a-hash",
            teacher_limit=20,
        )
        assert blocked.permit is None
        other = await coordinator.try_acquire(
            pool,
            capacity_limit=60,
            borrowed=False,
            use_global_gate=True,
            teacher_hash="teacher-b-hash",
            teacher_limit=20,
        )
        assert other.permit is not None
        await teacher_a[0].release()
        resumed = await coordinator.try_acquire(
            pool,
            capacity_limit=60,
            borrowed=False,
            use_global_gate=True,
            teacher_hash="teacher-a-hash",
            teacher_limit=20,
        )
        assert resumed.permit is not None
        for permit in teacher_a[1:]:
            await permit.release()
        await resumed.permit.release()
        await other.permit.release()

    asyncio.run(run())


def test_local_wave_coordinator_allows_three_and_queues_fourth():
    async def run():
        coordinator = LocalTeacherWaveCoordinator(active_limit=3)
        permits = []
        for index in range(3):
            permit = await coordinator.try_acquire_wave(
                f"teacher-{index}", f"wave-{index}", first_wave=True
            )
            assert permit is not None
            permits.append(permit)
        assert await coordinator.availability("teacher-4") == "queued"
        blocked = await coordinator.try_acquire_wave(
            "teacher-4", "wave-4", first_wave=True
        )
        assert blocked is None
        await permits[0].release()
        granted = await coordinator.try_acquire_wave(
            "teacher-4", "wave-4", first_wave=True
        )
        assert granted is not None
        await granted.release()
        for permit in permits[1:]:
            await permit.release()

    asyncio.run(run())


def test_first_wave_is_prioritized_over_existing_teacher_second_wave():
    async def run():
        coordinator = LocalTeacherWaveCoordinator(active_limit=1)
        active = await coordinator.try_acquire_wave("teacher-a", "a-first", first_wave=True)
        assert active is not None
        assert await coordinator.try_acquire_wave("teacher-a", "a-second", first_wave=False) is None
        assert await coordinator.try_acquire_wave("teacher-b", "b-first", first_wave=True) is None
        await active.release()
        b_first = await coordinator.try_acquire_wave("teacher-b", "b-first", first_wave=True)
        assert b_first is not None
        assert await coordinator.try_acquire_wave("teacher-a", "a-second", first_wave=False) is None
        await b_first.release()
        a_second = await coordinator.try_acquire_wave("teacher-a", "a-second", first_wave=False)
        assert a_second is not None
        await a_second.release()

    asyncio.run(run())


def test_continuation_wave_ages_ahead_of_sustained_new_first_waves():
    now = [100.0]

    async def run():
        coordinator = LocalTeacherWaveCoordinator(
            active_limit=1,
            queue_lease_s=100.0,
            first_wave_priority_grace_s=15.0,
        )
        active = await coordinator.try_acquire_wave(
            "teacher-a", "a-first", first_wave=True
        )
        assert active is not None
        assert await coordinator.try_acquire_wave(
            "teacher-a", "a-continuation", first_wave=False
        ) is None

        for second in range(100, 115):
            now[0] = float(second)
            wave_id = f"new-first-{second}"
            assert await coordinator.try_acquire_wave(
                wave_id, wave_id, first_wave=True
            ) is None
            await active.release()
            active = await coordinator.try_acquire_wave(
                wave_id, wave_id, first_wave=True
            )
            assert active is not None

        now[0] = 116.0
        await active.release()
        assert await coordinator.try_acquire_wave(
            "late-teacher", "late-first", first_wave=True
        ) is None
        continuation = await coordinator.try_acquire_wave(
            "teacher-a", "a-continuation", first_wave=False
        )
        assert continuation is not None
        await continuation.release()

        late = await coordinator.try_acquire_wave(
            "late-teacher", "late-first", first_wave=True
        )
        assert late is not None
        await late.release()

    with patch("services.ocr_teacher_fairness.time.time", side_effect=lambda: now[0]):
        asyncio.run(run())


def test_redis_wave_coordinator_uses_same_bounded_priority_delay():
    class FakeRedis:
        def __init__(self):
            self.calls = []

        async def eval(self, script, numkeys, *args):
            self.calls.append((script, numkeys, args))
            return 0

    redis = FakeRedis()
    coordinator = RedisTeacherWaveCoordinator(
        redis,
        namespace="test:teacher-waves",
        active_limit=1,
        operation_timeout_s=0.5,
        first_wave_priority_grace_s=15.0,
    )

    async def run():
        await coordinator.try_acquire_wave(
            "teacher-first", "first-wave", first_wave=True
        )
        await coordinator.try_acquire_wave(
            "teacher-continuation", "continuation-wave", first_wave=False
        )
        await coordinator.cancel_wave("continuation-wave")

    with patch("services.ocr_teacher_fairness.time.time", return_value=100.0):
        asyncio.run(run())

    acquire_calls = [call for call in redis.calls if call[1] == 4]
    assert [call[2][8] for call in acquire_calls] == [0.0, 15.0]
    assert all("local score = now + priority_delay" in call[0] for call in acquire_calls)
    assert all(
        "existing_score) >= 10000000000000" in call[0]
        for call in acquire_calls
    )
    cancel_call = redis.calls[-1]
    assert cancel_call[1] == 2
    assert cancel_call[2][-1] == "continuation-wave"
    assert "redis.call('ZREM', KEYS[2], ARGV[1])" in cancel_call[0]


def test_local_wave_permit_renews_across_its_original_short_lease(monkeypatch):
    now = [100.0]
    sleeping = asyncio.Queue()
    wake_renewal = asyncio.Queue()

    async def controlled_sleep(delay_s):
        await sleeping.put(delay_s)
        await wake_renewal.get()

    monkeypatch.setattr(
        "services.ocr_teacher_fairness.time.time", lambda: now[0]
    )
    monkeypatch.setattr(
        "services.ocr_teacher_fairness.asyncio.sleep", controlled_sleep
    )

    async def run():
        coordinator = LocalTeacherWaveCoordinator(active_limit=1, lease_s=0.09)
        permit = await coordinator.try_acquire_wave(
            "teacher-a", "wave-a", first_wave=True
        )
        assert permit is not None
        interval_s = await asyncio.wait_for(sleeping.get(), timeout=0.5)
        assert abs(interval_s - 0.03) < 0.001

        now[0] = 100.04
        wake_renewal.put_nowait(None)
        await asyncio.wait_for(sleeping.get(), timeout=0.5)

        now[0] = 100.10
        assert await coordinator.try_acquire_wave(
            "teacher-b", "wave-b", first_wave=True
        ) is None

        await permit.release()
        follower = await coordinator.try_acquire_wave(
            "teacher-b", "wave-b", first_wave=True
        )
        assert follower is not None
        await follower.release()

    asyncio.run(run())


def test_local_wave_permit_stops_renewing_at_hard_deadline(monkeypatch):
    clock = {"monotonic": 100.0, "epoch": 1_000.0}
    original_sleep = asyncio.sleep

    async def advance_clock(delay_s):
        clock["monotonic"] += delay_s
        clock["epoch"] += delay_s
        await original_sleep(0)

    monkeypatch.setattr(
        "services.ocr_teacher_fairness.time.monotonic",
        lambda: clock["monotonic"],
    )
    monkeypatch.setattr(
        "services.ocr_teacher_fairness.time.time",
        lambda: clock["epoch"],
    )
    monkeypatch.setattr(
        "services.ocr_teacher_fairness.asyncio.sleep",
        advance_clock,
    )

    async def run():
        coordinator = LocalTeacherWaveCoordinator(
            active_limit=1,
            lease_s=3.0,
            max_hold_s=5.0,
        )
        permit = await coordinator.try_acquire_wave(
            "teacher-a", "wave-a", first_wave=True
        )
        assert permit is not None

        await permit._renew_task

        assert clock["monotonic"] == 105.0
        assert coordinator._active["teacher-a"][2] <= 1_005.0
        follower = await coordinator.try_acquire_wave(
            "teacher-b", "wave-b", first_wave=True
        )
        assert follower is not None
        await follower.release()
        await permit.release()

    asyncio.run(run())


def test_redis_wave_renewal_requires_live_matching_owner():
    async def run():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        coordinator = RedisTeacherWaveCoordinator(
            redis,
            namespace="test:teacher-renewal",
            active_limit=1,
            operation_timeout_s=0.5,
            lease_s=10.0,
        )
        teacher = "teacher-a"
        wave = "wave-a"
        lease_token = "lease-a"
        active_key = "test:teacher-renewal:active"
        owner_key = "test:teacher-renewal:teacher:teacher-a:wave"
        now = [101.0]
        with patch(
            "services.ocr_teacher_fairness.time.time", side_effect=lambda: now[0]
        ):
            await redis.zadd(active_key, {teacher: 105.0})
            await redis.set(owner_key, lease_token, ex=60)
            assert await coordinator.renew_wave(teacher, wave, lease_token) is True
            assert float(await redis.zscore(active_key, teacher)) == 111.0

            now[0] = 102.0
            await redis.set(owner_key, "lease-b", ex=60)
            assert await coordinator.renew_wave(teacher, wave, lease_token) is False
            assert float(await redis.zscore(active_key, teacher)) == 111.0

            await redis.set(owner_key, lease_token, ex=60)
            await redis.zadd(active_key, {teacher: 102.0})
            assert await coordinator.renew_wave(teacher, wave, lease_token) is False
            assert float(await redis.zscore(active_key, teacher)) == 102.0
            await redis.aclose()

    asyncio.run(run())


def test_stale_local_permit_cannot_touch_reacquired_wave(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(
        "services.ocr_teacher_fairness.time.time", lambda: now[0]
    )

    async def run():
        coordinator = LocalTeacherWaveCoordinator(
            active_limit=1,
            lease_s=10.0,
            max_hold_s=20.0,
        )
        stale = await coordinator.try_acquire_wave(
            "teacher-a", "wave-a", first_wave=True
        )
        assert stale is not None

        now[0] = 111.0
        current = await coordinator.try_acquire_wave(
            "teacher-a", "wave-a", first_wave=True
        )
        assert current is not None
        assert current.lease_token != stale.lease_token

        assert await coordinator.renew_wave(
            "teacher-a", "wave-a", stale.lease_token
        ) is False
        await stale.release()
        assert coordinator._active["teacher-a"][1] == current.lease_token

        await current.release()
        assert "teacher-a" not in coordinator._active

    asyncio.run(run())


def test_stale_redis_permit_cannot_touch_reacquired_wave():
    async def run():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        coordinator = RedisTeacherWaveCoordinator(
            redis,
            namespace="test:teacher-fencing",
            active_limit=1,
            operation_timeout_s=0.5,
            lease_s=10.0,
            max_hold_s=20.0,
        )
        owner_key = "test:teacher-fencing:teacher:teacher-a:wave"
        active_key = "test:teacher-fencing:active"
        now = [100.0]
        with patch(
            "services.ocr_teacher_fairness.time.time", side_effect=lambda: now[0]
        ):
            stale = await coordinator.try_acquire_wave(
                "teacher-a", "wave-a", first_wave=True
            )
            assert stale is not None

            now[0] = 111.0
            current = await coordinator.try_acquire_wave(
                "teacher-a", "wave-a", first_wave=True
            )
            assert current is not None
            assert current.lease_token != stale.lease_token

            assert await coordinator.renew_wave(
                "teacher-a", "wave-a", stale.lease_token
            ) is False
            await stale.release()
            assert await redis.get(owner_key) == current.lease_token
            assert await redis.zscore(active_key, "teacher-a") is not None

            await current.release()
            assert await redis.get(owner_key) is None
            assert await redis.zscore(active_key, "teacher-a") is None
            await redis.aclose()

    asyncio.run(run())


def test_public_availability_contract_contains_no_capacity_details(monkeypatch):
    from services import ocr_teacher_fairness as fairness

    monkeypatch.setattr("config.OCR_MULTI_POOL_ENABLED", True)
    coordinator = LocalTeacherWaveCoordinator(active_limit=1)

    async def run():
        monkeypatch.setattr(fairness, "_coordinator", coordinator)
        monkeypatch.setattr(fairness, "_coordinator_loop", asyncio.get_running_loop())
        teacher_a = fairness.teacher_hash("teacher-a")
        permit = await coordinator.try_acquire_wave(teacher_a, "wave-a", first_wave=True)
        assert permit is not None
        payload = await fairness.public_teacher_availability("teacher-b")
        await permit.release()
        return payload

    payload = asyncio.run(run())
    # The compatibility coordinator can be full, but the active Flash route
    # queues at the shared per-request gate, not this retired teacher-count gate.
    assert payload == {"status": "available", "label": "可用"}
    forbidden = {"active_teachers", "queue_length", "free_slots", "position", "pools"}
    assert forbidden.isdisjoint(payload)


def test_abandoned_waiting_wave_expires_without_blocking_followers():
    async def run():
        coordinator = LocalTeacherWaveCoordinator(
            active_limit=1, lease_s=10, queue_lease_s=0.01
        )
        active = await coordinator.try_acquire_wave("teacher-a", "wave-a", first_wave=True)
        assert active is not None
        assert await coordinator.try_acquire_wave(
            "teacher-b", "wave-b", first_wave=True
        ) is None
        await active.release()
        await asyncio.sleep(0.02)
        follower = await coordinator.try_acquire_wave(
            "teacher-c", "wave-c", first_wave=True
        )
        assert follower is not None
        await follower.release()

    asyncio.run(run())


def test_public_availability_does_not_activate_retired_ocr_pool_graph(monkeypatch):
    from services import ocr_teacher_fairness as fairness

    class _WaveCoordinator:
        async def availability(self, _teacher_hash):
            return "available"

    async def run():
        monkeypatch.setattr("config.OCR_MULTI_POOL_ENABLED", True)
        monkeypatch.setattr(
            fairness, "get_teacher_wave_coordinator", lambda: _WaveCoordinator()
        )
        import services.ocr_pool_scheduler as scheduler_module

        assert not hasattr(scheduler_module, "get_ocr_pool_scheduler")
        return await fairness.public_teacher_availability("teacher-a")

    assert asyncio.run(run()) == {"status": "available", "label": "可用"}
