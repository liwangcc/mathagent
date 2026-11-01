"""Quota-free regressions for the shared Flash scheduler's useful capacity.

These exercise production dispatchers, payload paths and Redis Lua through
fakeredis. HTTP is mocked; they do not measure current provider capacity.
"""
import asyncio
import os
from pathlib import Path
import runpy

import fakeredis.aioredis
import pytest

import config
from services import flash_grading, task_queue
from services.bounded_dispatch import teacher_waved_completion_map
from services.model_concurrency import RedisModelLimiter, configured_fair_share_policy


@pytest.mark.parametrize("profile", ["standard", "low_memory"])
def test_memory_profiles_do_not_reduce_network_slots(monkeypatch, profile):
    env = os.environ.copy()
    for name in (
        "OCR_LOCAL_MAX_CONCURRENT", "OCR_BATCH_DISPATCH_CONCURRENT",
        "OCR_PER_TEACHER_LIMIT", "DASHSCOPE_GLOBAL_CONCURRENCY",
        "MODEL_TEACHER_BASE_CONCURRENCY", "MODEL_TEACHER_BURST_CONCURRENCY",
        "MODEL_GLOBAL_RESERVE_CONCURRENCY", "TEMPLATE_CUT_KEY_CONCURRENCY",
    ):
        env.pop(name, None)
    env.update(PYTHON_DOTENV_DISABLED="1", MATHAGENT_RESOURCE_PROFILE=profile)
    with monkeypatch.context() as isolated:
        isolated.setattr(os, "environ", env)
        loaded = runpy.run_path(str(Path(config.__file__)))
    assert loaded["DASHSCOPE_GLOBAL_CONCURRENCY"] == 256
    assert loaded["OCR_LOCAL_MAX_CONCURRENT"] == 256
    assert loaded["OCR_BATCH_DISPATCH_CONCURRENT"] == 256
    assert loaded["OCR_PER_TEACHER_LIMIT"] == 256
    assert loaded["TEMPLATE_CUT_KEY_CONCURRENCY"] == 256
    assert loaded["MODEL_GLOBAL_RESERVE_CONCURRENCY"] == 0
    # Native preprocessing and byte admission are still separate safeguards.
    assert loaded["OCR_PREPROCESS_LOCAL_MAX_CONCURRENT"] > 0
    assert loaded["OCR_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES"] > 0


def test_dashscope_ignores_legacy_nonborrowable_reserve(monkeypatch):
    monkeypatch.setattr(config, "MODEL_TEACHER_BASE_CONCURRENCY", 30)
    monkeypatch.setattr(config, "MODEL_TEACHER_BURST_CONCURRENCY", 60)
    monkeypatch.setattr(config, "MODEL_GLOBAL_RESERVE_CONCURRENCY", 30)
    policy = configured_fair_share_policy(256, provider="dashscope")
    assert policy.global_reserve_capacity == 0
    assert policy.teacher_burst_capacity == 256


def test_grading_queue_has_no_secondary_teacher_sixty_slot_cap(monkeypatch):
    monkeypatch.setattr(config, "MODEL_TEACHER_BURST_CONCURRENCY", 60)
    monkeypatch.setattr(flash_grading, "FLASH_LOCAL_MAX_CONCURRENT", 256)
    monkeypatch.setattr(task_queue, "_grading_queue", None)
    queue = task_queue.get_grading_queue()
    assert queue._semaphore._value == 256
    assert queue._per_teacher_limit == 256


def test_flash_default_window_matches_shared_capacity(monkeypatch):
    monkeypatch.delenv("FLASH_LOCAL_MAX_CONCURRENT", raising=False)
    assert flash_grading._configured_flash_local_max_concurrent() == config.DASHSCOPE_GLOBAL_CONCURRENCY


def test_flash_dispatch_refills_before_slow_item_finishes():
    async def run():
        release_slow = asyncio.Event()
        replacement_started = asyncio.Event()
        started = []

        async def work(index):
            started.append(index)
            if index == 0:
                await release_slow.wait()
            elif index == 2:
                replacement_started.set()
            return index

        async def consume():
            return [result async for result in teacher_waved_completion_map(
                range(3), work, teacher_id="teacher-a", batch_id="batch-a",
                wave_size=2, max_concurrent=2,
            )]

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(replacement_started.wait(), timeout=0.3)
            assert not release_slow.is_set()
            assert started == [0, 1, 2]
        finally:
            release_slow.set()
            results = await task
        assert sorted(results) == [(0, 0), (1, 1), (2, 2)]

    asyncio.run(run())


@pytest.mark.parametrize("legacy_multi_pool", [False, True])
def test_full_flash_dispatch_window_ignores_retired_wave_caps(monkeypatch, legacy_multi_pool):
    import services.ocr_teacher_fairness as fairness

    monkeypatch.setattr(config, "OCR_MULTI_POOL_ENABLED", legacy_multi_pool)
    monkeypatch.setattr(config, "OCR_ACTIVE_TEACHER_LIMIT", 1)

    async def retired_gate(*_args, **_kwargs):
        raise AssertionError("Flash dispatch must not use whole-wave admission")

    monkeypatch.setattr(fairness, "acquire_teacher_wave", retired_gate)

    async def run():
        capacity = config.DASHSCOPE_GLOBAL_CONCURRENCY
        full = asyncio.Event()
        release = asyncio.Event()
        active = 0
        peak = 0

        async def work(index):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == capacity:
                full.set()
            try:
                await release.wait()
                return index
            finally:
                active -= 1

        async def consume():
            return [result async for result in teacher_waved_completion_map(
                range(capacity + 1), work,
                teacher_id="teacher-a", batch_id="full-window",
                wave_size=1, max_concurrent=capacity,
            )]

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(full.wait(), timeout=1)
            assert active == capacity == 256
        finally:
            release.set()
            results = await asyncio.wait_for(task, timeout=1)
        assert len(results) == capacity + 1
        assert peak == capacity

    asyncio.run(run())


def test_legacy_multi_pool_readiness_never_gates_flash(monkeypatch):
    import services.ocr_teacher_fairness as fairness

    monkeypatch.setattr(config, "OCR_MULTI_POOL_ENABLED", True)
    assert asyncio.run(
        fairness.public_teacher_availability("teacher-with-old-env")
    ) == {"status": "available", "label": "可用"}


@pytest.mark.parametrize("capacity", [8, 256])
@pytest.mark.parametrize("other_active", [False, True])
def test_waiting_teacher_fills_all_free_slots_even_if_other_teacher_is_active(other_active, capacity):
    async def run():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        workers = [RedisModelLimiter(redis, acquire_timeout_seconds=0.08) for _ in range(2)]
        common = dict(
            provider="dashscope", api_key="synthetic-key", global_capacity=capacity,
            teacher_base_capacity=1, teacher_burst_capacity=capacity,
            global_reserve_capacity=0,
        )
        leases = []
        try:
            if other_active:
                leases.append(await workers[1].acquire(teacher_id="teacher-b", **common))
            for index in range(capacity - len(leases)):
                leases.append(await workers[index % 2].acquire(teacher_id="teacher-a", **common))
            global_key = workers[0]._keys("dashscope", "synthetic-key", "teacher-a")[0]
            assert await redis.zcard(global_key) == capacity
            with pytest.raises(TimeoutError):
                await workers[1].acquire(teacher_id="teacher-a", **common)
            assert await redis.zcard(global_key) == capacity
        finally:
            for lease in leases:
                await lease.__aexit__(None, None, None)
            await redis.aclose()

    asyncio.run(run())


def test_late_waiting_teacher_gets_next_released_slot():
    async def run():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        worker_a = RedisModelLimiter(redis, acquire_timeout_seconds=0.5)
        worker_b = RedisModelLimiter(redis, acquire_timeout_seconds=0.5)
        common = dict(
            provider="dashscope", api_key="synthetic-key", global_capacity=4,
            teacher_base_capacity=1, teacher_burst_capacity=4,
            global_reserve_capacity=0,
        )
        leases = []
        pending = []
        try:
            for _ in range(4):
                leases.append(await worker_a.acquire(teacher_id="teacher-a", **common))
            waiting_b = asyncio.create_task(worker_b.acquire(teacher_id="teacher-b", **common))
            pending.append(waiting_b)
            waiter_key = worker_b._keys("dashscope", "synthetic-key", "teacher-b")[3]
            async def wait_for_registration():
                while not await redis.zcard(waiter_key):
                    await asyncio.sleep(0)
            await asyncio.wait_for(wait_for_registration(), timeout=0.3)
            waiting_a = asyncio.create_task(worker_a.acquire(teacher_id="teacher-a", **common))
            pending.append(waiting_a)
            await leases.pop().__aexit__(None, None, None)
            leases.append(await asyncio.wait_for(asyncio.shield(waiting_b), timeout=0.3))
            assert not waiting_a.done()
            assert await redis.zcard(worker_a._keys("dashscope", "synthetic-key", "teacher-a")[0]) == 4
        finally:
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for lease in leases:
                await lease.__aexit__(None, None, None)
            await redis.aclose()

    asyncio.run(run())
