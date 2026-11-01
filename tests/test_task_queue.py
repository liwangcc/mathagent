import asyncio
import sys
import types

import httpx
import pytest

from services import task_queue


def test_retry_backoff_releases_process_and_teacher_slots(monkeypatch):
    async def run():
        monkeypatch.setattr(
            task_queue,
            "exponential_retry_delay",
            lambda attempt, *, base_delay_s, retry_after_s=None, ceiling_s: max(
                base_delay_s * (2 ** (attempt - 1)), retry_after_s or 0,
            ),
        )
        policy = task_queue.RetryPolicy(
            max_retries=1,
            server_error_base_delay=1.0,
            server_error_max_delay=1.0,
        )
        queue = task_queue.TaskQueue(
            max_concurrent=1,
            per_teacher_limit=1,
            retry_policy=policy,
        )
        backoff_started = asyncio.Event()
        allow_retry = asyncio.Event()
        attempts = 0

        async def controlled_sleep(delay):
            assert delay == 1.0
            backoff_started.set()
            await allow_retry.wait()

        async def flaky_call():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.ConnectError("temporary provider failure")
            return "retried"

        async def other_call():
            return "other"

        monkeypatch.setattr(task_queue.asyncio, "sleep", controlled_sleep)
        first = asyncio.create_task(
            queue.submit(flaky_call, teacher_id="teacher-1")
        )
        second = None
        try:
            await asyncio.wait_for(backoff_started.wait(), timeout=1.0)

            teacher_sem = queue._teacher_semaphores["teacher-1"]
            assert queue._semaphore._value == 1
            assert teacher_sem._value == 1

            second = asyncio.create_task(
                queue.submit(other_call, teacher_id="teacher-1")
            )
            assert await asyncio.wait_for(second, timeout=0.5) == "other"
        finally:
            allow_retry.set()
            if second is not None:
                await asyncio.gather(second, return_exceptions=True)
            await asyncio.gather(first, return_exceptions=True)

        assert await first == "retried"
        assert queue.stats()["total_retries"] == 1

    asyncio.run(run())


def test_cancellation_releases_both_slots_and_is_not_retried():
    async def run():
        queue = task_queue.TaskQueue(max_concurrent=1, per_teacher_limit=1)
        started = asyncio.Event()
        stop = asyncio.Event()

        async def blocked_call():
            started.set()
            await stop.wait()

        pending = asyncio.create_task(
            queue.submit(blocked_call, teacher_id="teacher-cancel")
        )
        await asyncio.wait_for(started.wait(), timeout=1.0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

        assert queue.stats()["total_cancelled"] == 1
        assert queue.stats()["total_failed"] == 0
        assert queue.stats()["total_retries"] == 0
        assert queue.stats()["active_tasks"] == 0
        assert queue._semaphore._value == 1
        assert queue._teacher_semaphores["teacher-cancel"]._value == 1

    asyncio.run(run())


def test_short_retry_after_is_respected_and_long_retry_after_fails_fast(monkeypatch):
    async def run():
        delays = []
        attempts = 0
        policy = task_queue.RetryPolicy(
            max_retries=2,
            rate_limit_base_delay=1.0,
            rate_limit_max_delay=10.0,
        )
        queue = task_queue.TaskQueue(
            max_concurrent=1,
            per_teacher_limit=1,
            retry_policy=policy,
        )

        monkeypatch.setattr(
            task_queue,
            "exponential_retry_delay",
            lambda attempt, *, base_delay_s, retry_after_s=None, ceiling_s: max(
                base_delay_s * (2 ** (attempt - 1)), retry_after_s or 0,
            ),
        )

        async def record_sleep(delay):
            delays.append(delay)

        monkeypatch.setattr(task_queue.asyncio, "sleep", record_sleep)

        async def short_limited():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                response = httpx.Response(429, headers={"Retry-After": "3"})
                raise httpx.HTTPStatusError(
                    "rate limited",
                    request=httpx.Request("POST", "https://provider.invalid"),
                    response=response,
                )
            return "ok"

        assert await queue.submit(short_limited) == "ok"
        assert delays == [3.0]

        attempts = 0
        delays.clear()

        async def long_limited():
            nonlocal attempts
            attempts += 1
            response = httpx.Response(429, headers={"Retry-After": "600"})
            raise httpx.HTTPStatusError(
                "rate limited",
                request=httpx.Request("POST", "https://provider.invalid"),
                response=response,
            )

        with pytest.raises(httpx.HTTPStatusError):
            await queue.submit(long_limited)
        assert attempts == 1
        assert delays == []

    asyncio.run(run())


def test_retry_policy_adds_positive_jitter(monkeypatch):
    monkeypatch.setattr("services.retry_backoff.random.uniform", lambda low, high: high)
    policy = task_queue.RetryPolicy(
        server_error_base_delay=2.0,
        server_error_max_delay=10.0,
    )

    assert policy.get_delay(task_queue.ErrorCategory.SERVER_ERROR, 0) == 2.5


def test_grading_singleton_does_not_restore_retired_teacher_burst(monkeypatch):
    from services import flash_grading

    fake_config = types.ModuleType("config")
    fake_config.MODEL_TEACHER_BURST_CONCURRENCY = 60

    previous = task_queue._grading_queue
    monkeypatch.setitem(sys.modules, "config", fake_config)
    monkeypatch.setattr(flash_grading, "FLASH_LOCAL_MAX_CONCURRENT", 256)
    task_queue._grading_queue = None
    try:
        queue = task_queue.get_grading_queue()
        assert queue._semaphore._value == 256
        assert queue._per_teacher_limit == 256
        assert queue._retry_policy.max_retries == 1
    finally:
        task_queue._grading_queue = previous


def test_task_queue_rejects_non_positive_local_limits():
    for kwargs in (
        {"max_concurrent": 0},
        {"per_teacher_limit": 0},
        {"idempotent_cache_ttl_seconds": 0},
        {"idempotent_cache_max_entries": 0},
    ):
        with pytest.raises(ValueError):
            task_queue.TaskQueue(**kwargs)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (httpx.ReadError("connection dropped"), task_queue.ErrorCategory.SERVER_ERROR),
        (TimeoutError("capacity wait expired"), task_queue.ErrorCategory.TIMEOUT),
        (ValueError("invalid result"), task_queue.ErrorCategory.UNKNOWN),
    ],
)
def test_retry_classifier_covers_transport_and_builtin_timeout(exc, expected):
    assert task_queue.classify_error(exc) == expected


def test_unknown_programming_error_is_not_retried():
    async def run():
        queue = task_queue.TaskQueue(max_concurrent=1, per_teacher_limit=1)
        calls = 0

        async def invalid_operation():
            nonlocal calls
            calls += 1
            raise ValueError("invalid grading contract")

        with pytest.raises(ValueError, match="invalid grading contract"):
            await queue.submit(invalid_operation, teacher_id="teacher-1")
        assert calls == 1
        assert queue.stats()["total_retries"] == 0

    asyncio.run(run())


def test_explicit_retryable_error_is_retried_once():
    class ProviderResultMissing(RuntimeError):
        retryable = True

    async def run():
        queue = task_queue.TaskQueue(
            max_concurrent=1,
            per_teacher_limit=1,
            retry_policy=task_queue.RetryPolicy(
                max_retries=1,
                server_error_base_delay=0.001,
                server_error_max_delay=0.001,
            ),
        )
        attempts = 0

        async def flaky_call():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ProviderResultMissing("empty grading result")
            return "completed"

        assert await queue.submit(flaky_call) == "completed"
        assert attempts == 2
        assert queue.stats()["total_retries"] == 1

    asyncio.run(run())


def test_uncacheable_result_is_recomputed_on_same_idempotency_key():
    class Uncacheable(dict):
        __task_queue_cacheable__ = False

    async def run():
        queue = task_queue.TaskQueue(max_concurrent=1, per_teacher_limit=1)
        calls = 0

        async def operation():
            nonlocal calls
            calls += 1
            if calls == 1:
                return Uncacheable({"status": "provider_unavailable"})
            return {"status": "ok"}

        first = await queue.submit(operation, idempotency_key="same-paper")
        second = await queue.submit(operation, idempotency_key="same-paper")
        third = await queue.submit(operation, idempotency_key="same-paper")

        assert first["status"] == "provider_unavailable"
        assert second == {"status": "ok"}
        assert third == {"status": "ok"}
        assert calls == 2

    asyncio.run(run())


def test_idempotent_cache_expires_entries_during_lookup(monkeypatch):
    now = 1000.0
    monkeypatch.setattr(task_queue.time, "time", lambda: now)

    async def run():
        queue = task_queue.TaskQueue(
            max_concurrent=1,
            per_teacher_limit=1,
            idempotent_cache_ttl_seconds=10,
        )
        calls = 0

        async def operation():
            nonlocal calls
            calls += 1
            return {"call": calls}

        assert await queue.submit(operation, idempotency_key="paper") == {"call": 1}
        assert await queue.submit(operation, idempotency_key="paper") == {"call": 1}
        monkeypatch.setattr(task_queue.time, "time", lambda: now + 11)
        assert await queue.submit(operation, idempotency_key="paper") == {"call": 2}
        assert calls == 2

    asyncio.run(run())


def test_idempotent_cache_has_a_hard_entry_limit():
    async def run():
        queue = task_queue.TaskQueue(
            max_concurrent=1,
            per_teacher_limit=1,
            idempotent_cache_max_entries=2,
        )

        async def operation():
            return {"status": "ok"}

        for key in ("paper-1", "paper-2", "paper-3"):
            await queue.submit(operation, idempotency_key=key)

        assert queue.stats()["idempotent_cache_size"] == 2
        assert "paper-1" not in queue._idempotent_map

    asyncio.run(run())
