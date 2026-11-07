"""Opt-in RPM/TPM accounting; no provider, credentials or business data."""
import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import fakeredis.aioredis
import httpx
import pytest

from services import dashscope_budget
from services import model_concurrency as mc
from services import ocr_service as ocr
from services import template_column_layout_provider


PROVIDER = "dashscope:test-model"


def budget(**overrides):
    return dict(provider=PROVIDER, requests_per_minute=10, tokens_per_minute=10,
                reserved_tokens=10, timeout_seconds=0.01) | overrides


@pytest.fixture(params=["local", "redis"])
def backend(request):
    return request.param


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(epoch=1000.0, monotonic=0.0, sleeps=[], on_sleep=None)
    original_sleep = asyncio.sleep

    async def sleep(seconds):
        clock.sleeps.append(seconds)
        if clock.on_sleep is not None:
            callback, clock.on_sleep = clock.on_sleep, None
            await callback()
        clock.epoch += seconds
        clock.monotonic += seconds
        await original_sleep(0)

    # Leave fakeredis' TTL clock untouched.
    monkeypatch.setattr(mc, "time", SimpleNamespace(
        time=lambda: clock.epoch, monotonic=lambda: clock.monotonic))
    monkeypatch.setattr(mc.asyncio, "sleep", sleep)
    return clock


@asynccontextmanager
async def workers(backend):
    if backend == "local":
        limiter = mc.NoopModelLimiter()
        yield limiter, limiter, None
    else:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        try:
            yield (mc.RedisModelLimiter(redis, operation_timeout_seconds=2),
                   mc.RedisModelLimiter(redis, operation_timeout_seconds=2), redis)
        finally:
            await redis.aclose()


async def assert_totals(limiter, redis, requests, tokens):
    if redis is not None:
        totals = await redis.hgetall(limiter._rate_budget_totals_key(PROVIDER))
        assert int(totals.get("requests", 0)) == requests
        assert int(totals.get("tokens", 0)) == tokens
        assert await redis.zcard(limiter._rate_budget_keys(PROVIDER)[0]) == requests
    else:
        entries = limiter._rate_budget_windows.get(PROVIDER, [])
        assert len(entries) == requests
        assert sum(item[1] for item in entries) == tokens


def test_local_reservation_cancel_refunds_both_budgets():
    async def run():
        limiter = mc.NoopModelLimiter()
        assert callable(getattr(limiter, "reserve_rate_budget", None)), (
            "opt-in cancellable rate reservations must be implemented"
        )
        kwargs = dict(provider="dashscope:test-model", requests_per_minute=1,
                      tokens_per_minute=10, reserved_tokens=10, timeout_seconds=0.01)
        reservation = await limiter.reserve_rate_budget(**kwargs)
        assert await limiter.cancel_rate_budget(reservation) is True
        assert await limiter.cancel_rate_budget(reservation) is False
        successor = await limiter.reserve_rate_budget(**kwargs)
        assert successor.reservation_id != reservation.reservation_id

    asyncio.run(run())


def test_cancel_refunds_both_budgets_once(backend, clock):
    async def run():
        async with workers(backend) as (a, b, redis):
            reservation = await a.reserve_rate_budget(**budget(requests_per_minute=1))
            assert isinstance(reservation, mc.RateBudgetReservation)
            assert reservation.provider == PROVIDER
            with pytest.raises(FrozenInstanceError):
                reservation.reservation_id = "changed"
            with pytest.raises(mc.RateBudgetTimeout):
                await b.reserve_rate_budget(**budget(requests_per_minute=1))
            assert await b.cancel_rate_budget(reservation) is True
            assert await a.cancel_rate_budget(reservation) is False
            assert await a.settle_rate_budget(reservation, actual_tokens=99) is False
            await assert_totals(a, redis, 0, 0)
            successor = await b.reserve_rate_budget(**budget(requests_per_minute=1))
            assert successor.reservation_id != reservation.reservation_id
            await assert_totals(a, redis, 1, 10)
    asyncio.run(run())


@pytest.mark.parametrize("actual", [0, 3, 15])
def test_usage_replaces_estimate_once_even_above_limit(backend, clock, actual):
    async def run():
        async with workers(backend) as (a, b, redis):
            reservation = await a.reserve_rate_budget(**budget())
            assert await b.settle_rate_budget(reservation, actual_tokens=actual) is True
            assert await a.settle_rate_budget(reservation, actual_tokens=1) is False
            assert await b.cancel_rate_budget(reservation) is False
            await assert_totals(a, redis, 1, actual)
            if actual <= 3:
                await b.reserve_rate_budget(**budget(reserved_tokens=7))
                await assert_totals(a, redis, 2, actual + 7)
            else:
                with pytest.raises(mc.RateBudgetTimeout):
                    await b.reserve_rate_budget(**budget(reserved_tokens=1))
                await assert_totals(a, redis, 1, actual)
            # Settlement refunds only TPM, never the request count.
            with pytest.raises(mc.RateBudgetTimeout):
                await b.reserve_rate_budget(**budget(requests_per_minute=1, reserved_tokens=1))
    asyncio.run(run())


def test_missing_usage_keeps_estimate_and_late_expiry_never_revives(backend, clock):
    async def run():
        async with workers(backend) as (a, b, redis):
            reservation = await a.reserve_rate_budget(**budget())
            # Unknown usage deliberately never calls settle.
            with pytest.raises(mc.RateBudgetTimeout):
                await b.reserve_rate_budget(**budget(reserved_tokens=1))
            await assert_totals(a, redis, 1, 10)
            clock.epoch = 1060.0
            assert await b.settle_rate_budget(reservation, actual_tokens=5) is False
            assert await b.cancel_rate_budget(reservation) is False
            await assert_totals(a, redis, 0, 0)
            successor = await b.reserve_rate_budget(**budget())
            assert await a.cancel_rate_budget(reservation) is False
            assert await a.settle_rate_budget(reservation, actual_tokens=999) is False
            await assert_totals(a, redis, 1, 10)
            assert successor != reservation
    asyncio.run(run())


@pytest.mark.parametrize("action", ["cancel", "settle"])
def test_refund_releases_waiter_within_poll_before_expiry(backend, clock, action):
    async def run():
        async with workers(backend) as (a, b, redis):
            reservation = await a.reserve_rate_budget(**budget())

            async def refund():
                if action == "cancel":
                    assert await a.cancel_rate_budget(reservation)
                else:
                    assert await a.settle_rate_budget(reservation, actual_tokens=0)

            clock.on_sleep = refund
            successor = await b.reserve_rate_budget(**budget(timeout_seconds=1))
            assert successor != reservation
            assert clock.sleeps and all(seconds <= 0.1 for seconds in clock.sleeps)
            assert clock.epoch <= 1000.1
            await assert_totals(a, redis, 1 if action == "cancel" else 2, 10)
    asyncio.run(run())


def test_legacy_acquire_none_and_wait_strategy_share_opt_in_window(backend, clock):
    async def run():
        async with workers(backend) as (a, b, redis):
            assert await a.acquire_rate_budget(**budget(reserved_tokens=4)) is None
            reservation = await b.reserve_rate_budget(**budget(reserved_tokens=6))
            await assert_totals(a, redis, 2, 10)
            assert await b.cancel_rate_budget(reservation)
            await assert_totals(a, redis, 1, 4)
            assert await a.acquire_rate_budget(**budget(reserved_tokens=6)) is None
            clock.sleeps.clear()
            with pytest.raises(mc.RateBudgetTimeout):
                await a.acquire_rate_budget(**budget(timeout_seconds=1, reserved_tokens=1))
            assert clock.sleeps == [1]
            before = await redis.keys("*") if redis is not None else list(a._rate_budget_windows)
            disabled = budget(provider="disabled", requests_per_minute=0, tokens_per_minute=0)
            assert await a.reserve_rate_budget(**disabled) is None
            assert await a.acquire_rate_budget(**disabled) is None
            after = await redis.keys("*") if redis is not None else list(a._rate_budget_windows)
            assert after == before
    asyncio.run(run())


@pytest.mark.parametrize("field,value", [
    ("requests_per_minute", True), ("requests_per_minute", -1),
    ("requests_per_minute", 0.5), ("requests_per_minute", float("nan")),
    ("tokens_per_minute", True), ("tokens_per_minute", -1),
    ("tokens_per_minute", 1.5), ("reserved_tokens", True),
    ("reserved_tokens", -1), ("reserved_tokens", 0),
    ("reserved_tokens", 1.5), ("reserved_tokens", 11),
    ("timeout_seconds", True), ("timeout_seconds", -1),
    ("timeout_seconds", float("inf")),
])
def test_invalid_admission_has_no_side_effects(backend, clock, field, value):
    async def run():
        async with workers(backend) as (a, _, redis):
            for method in (a.reserve_rate_budget, a.acquire_rate_budget):
                with pytest.raises(ValueError):
                    await method(**budget(**{field: value}))
            if redis is not None:
                assert await redis.keys("*") == []
            else:
                assert a._rate_budget_windows == {}
    asyncio.run(run())


@pytest.mark.parametrize("actual", [True, -1, 1.5, None])
def test_invalid_usage_does_not_mutate_reservation(backend, clock, actual):
    async def run():
        async with workers(backend) as (a, _, redis):
            reservation = await a.reserve_rate_budget(**budget())
            with pytest.raises(ValueError):
                await a.settle_rate_budget(reservation, actual_tokens=actual)
            await assert_totals(a, redis, 1, 10)
            assert await a.settle_rate_budget(reservation, actual_tokens=3)
    asyncio.run(run())


def test_unknown_ids_do_not_create_redis_windows(backend, clock):
    async def run():
        async with workers(backend) as (a, _, redis):
            unknown = mc.RateBudgetReservation(PROVIDER, "missing")
            assert await a.settle_rate_budget(unknown, actual_tokens=123) is False
            assert await a.cancel_rate_budget(unknown) is False
            if redis is not None:
                assert await redis.keys("*") == []
            await assert_totals(a, redis, 0, 0)
            with pytest.raises(ValueError):
                await a.cancel_rate_budget(None)
    asyncio.run(run())


def test_provider_model_namespace_and_alias_preserved(backend, clock):
    async def run():
        async with workers(backend) as (a, b, _):
            reservation = await a.reserve_rate_budget(**budget())
            await b.reserve_rate_budget(**budget(provider="dashscope:other-model"))
            wrong = mc.RateBudgetReservation("dashscope:other-model", reservation.reservation_id)
            assert not await a.cancel_rate_budget(wrong)
            with pytest.raises(mc.RateBudgetTimeout):
                await b.reserve_rate_budget(**budget())
            alias = await a.reserve_rate_budget(**budget(provider="xiaomi_mimo"))
            assert alias.provider == mc.MIMO_SUBSCRIPTION_PROVIDER
            with pytest.raises(mc.RateBudgetTimeout):
                await b.reserve_rate_budget(**budget(provider=mc.MIMO_SUBSCRIPTION_PROVIDER))
            assert await b.cancel_rate_budget(alias)
    asyncio.run(run())


def test_redis_atomic_race_has_one_winner(clock):
    async def run():
        async with workers("redis") as (a, b, redis):
            reservation = await a.reserve_rate_budget(**budget())
            outcomes = await asyncio.gather(
                a.settle_rate_budget(reservation, actual_tokens=3),
                b.settle_rate_budget(reservation, actual_tokens=15),
                b.cancel_rate_budget(reservation))
            assert sum(outcomes) == 1
            await assert_totals(a, redis, 0 if outcomes[2] else 1,
                                0 if outcomes[2] else 3 if outcomes[0] else 15)
    asyncio.run(run())


@pytest.mark.parametrize("missing", ["none", "totals", "requests", "tokens", "states", "all"])
def test_redis_ttl_not_extended_and_missing_members_not_revived(clock, missing):
    async def run():
        async with workers("redis") as (a, b, redis):
            reservation = await a.reserve_rate_budget(**budget())
            keys = dict(zip(("requests", "tokens"), a._rate_budget_keys(PROVIDER)))
            keys["totals"] = a._rate_budget_totals_key(PROVIDER)
            keys["states"] = a._rate_budget_states_key(PROVIDER)
            for key in keys.values():
                await redis.pexpire(key, 5000)
            if missing == "all":
                await redis.delete(*keys.values())
            elif missing != "none":
                await redis.delete(keys[missing])
            before = {name: await redis.pttl(key) for name, key in keys.items()}
            changed = await b.settle_rate_budget(reservation, actual_tokens=3)
            assert changed is (missing in {"none", "totals"})
            assert await a.cancel_rate_budget(reservation) is False
            assert await a.settle_rate_budget(reservation, actual_tokens=999) is False
            for name, key in keys.items():
                after = await redis.pttl(key)
                if before[name] > 0:
                    assert after <= before[name]
                if missing == "all" or name == missing and name != "totals":
                    assert after == -2
            if missing == "totals":
                assert 0 < await redis.pttl(keys["totals"]) <= before["requests"]
                await assert_totals(a, redis, 1, 3)
            if missing == "none":
                assert await redis.zscore(keys["requests"], reservation.reservation_id) == 1000.0
    asyncio.run(run())


@pytest.mark.parametrize("initialized", [False, True])
def test_redis_30000_cleanup_and_legacy_window_migration(clock, initialized):
    async def run():
        async with workers("redis") as (a, _, redis):
            requests, tokens = a._rate_budget_keys(PROVIDER)
            totals = a._rate_budget_totals_key(PROVIDER)
            states = a._rate_budget_states_key(PROVIDER)
            expired = {f"expired-{index}": 939 for index in range(30_000)}
            await redis.zadd(requests, expired | {"legacy-live": 999})
            await redis.hset(tokens, mapping={member: 1 for member in expired} | {"legacy-live": 4})
            await redis.hset(states, mapping={member: "pending" for member in expired})
            if initialized:
                await redis.hset(totals, mapping={"initialized": 1, "requests": 30_001, "tokens": 30_004})
            reservation = await a.reserve_rate_budget(**budget(reserved_tokens=6))
            await assert_totals(a, redis, 2, 10)
            assert await redis.hlen(tokens) == 2
            assert await redis.hlen(states) == 1
            assert await a.settle_rate_budget(reservation, actual_tokens=1)
            await assert_totals(a, redis, 2, 5)
            assert await a.cancel_rate_budget(mc.RateBudgetReservation(PROVIDER, "legacy-live")) is False
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["exception", "cancel", "timeout"])
def test_lost_redis_response_cleans_stable_candidate(clock, monkeypatch, failure):
    async def run():
        async with workers("redis") as (a, _, redis):
            a._operation_timeout_seconds = 0.01
            original_eval = redis.eval
            candidate_ids = []

            async def lose_response(script, numkeys, *args):
                result = await original_eval(script, numkeys, *args)
                if script == mc._ACQUIRE_RATE_BUDGET_LUA:
                    candidate_ids.append(args[numkeys + 5])
                    if failure == "exception":
                        raise RuntimeError("admission response lost")
                    if failure == "cancel":
                        raise asyncio.CancelledError()
                    await asyncio.Event().wait()
                else:
                    candidate_ids.append(args[numkeys + 2])
                return result

            monkeypatch.setattr(redis, "eval", lose_response)
            error = {"exception": RuntimeError, "cancel": asyncio.CancelledError,
                     "timeout": TimeoutError}[failure]
            with pytest.raises(error):
                await a.reserve_rate_budget(**budget())
            assert len(candidate_ids) == 2 and candidate_ids[0] == candidate_ids[1]
            await assert_totals(a, redis, 0, 0)
            assert await redis.hlen(a._rate_budget_states_key(PROVIDER)) == 0
            monkeypatch.setattr(redis, "eval", original_eval)
            await a.reserve_rate_budget(**budget())
    asyncio.run(run())


def test_waiter_cancellation_does_not_refund_other_request(backend, clock):
    async def run():
        async with workers(backend) as (a, b, redis):
            blocker = await a.reserve_rate_budget(**budget())

            async def cancel_waiter():
                raise asyncio.CancelledError()

            clock.on_sleep = cancel_waiter
            with pytest.raises(asyncio.CancelledError):
                await b.reserve_rate_budget(**budget(timeout_seconds=1))
            await assert_totals(a, redis, 1, 10)
            assert b.__dict__.get("_rate_budget_local_waiters", {}) == {}
            assert await a.cancel_rate_budget(blocker)
            await assert_totals(a, redis, 0, 0)
    asyncio.run(run())


def test_settlements_refill_many_waiters_long_before_expiry(backend, clock):
    async def run():
        async with workers(backend) as (a, b, redis):
            limits = dict(requests_per_minute=100, tokens_per_minute=30, reserved_tokens=10)
            held = [await a.reserve_rate_budget(**budget(**limits)) for _ in range(3)]
            waiters = [asyncio.create_task(b.reserve_rate_budget(
                **budget(**limits, timeout_seconds=30))) for _ in range(3)]
            for _ in range(5):
                await asyncio.sleep(0)
            assert not any(task.done() for task in waiters)
            for admitted, reservation in enumerate(held, start=1):
                # Each settlement frees one estimate; one waiter takes it.
                assert await a.settle_rate_budget(reservation, actual_tokens=0)
                for _ in range(200):
                    if sum(task.done() for task in waiters) >= admitted:
                        break
                    await asyncio.sleep(0)
                assert sum(task.done() for task in waiters) == admitted
            assert all(isinstance(task.result(), mc.RateBudgetReservation) for task in waiters)
            # Refill came from settlement, not from the 60-second expiry.
            assert clock.epoch - 1000.0 < 10
            await assert_totals(a, redis, 6, 30)
            assert all(seconds <= 0.1 for seconds in clock.sleeps)
    asyncio.run(run())


def test_poll_interval_scales_with_local_waiters_and_is_capped(monkeypatch):
    limiter = mc.NoopModelLimiter()
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(mc.asyncio, "sleep", fake_sleep)

    async def run():
        await limiter._rate_budget_sleep("p", 60.0, 100.0, poll=True)
        limiter._rate_budget_local_waiters["p"] = 63
        await limiter._rate_budget_sleep("p", 60.0, 100.0, poll=True)
        limiter._rate_budget_local_waiters["p"] = 10_000
        await limiter._rate_budget_sleep("p", 60.0, 100.0, poll=True)
        await limiter._rate_budget_sleep("p", 0.02, 100.0, poll=True)
        await limiter._rate_budget_sleep("p", 60.0, 0.5, poll=True)
        await limiter._rate_budget_sleep("p", 60.0, 100.0, poll=False)

    asyncio.run(run())
    assert 0.05 <= slept[0] <= 0.1          # sole waiter: timely refill
    assert 0.2 <= slept[1] <= 0.4           # 64 waiters share the probe rate
    assert 0.5 <= slept[2] <= 1.0           # capped, never a whole window
    assert slept[3] == 0.02                 # an earlier expiry wins
    assert slept[4] <= 0.5                  # the caller's deadline wins
    assert slept[5] == 60.0                 # legacy waits for expiry
    assert limiter._rate_budget_local_waiters == {"p": 10_000}


# ---------------------------------------------------------------------------
# Consumer lifecycle: OCR, Flash grading and template-split transports. Every
# HTTP client below is an in-process stub; no provider can be reached.
# ---------------------------------------------------------------------------

FLASH = "dashscope:qwen3.8-flash"
URL = "https://unit.invalid/v1/chat/completions"
OCR_PAYLOAD = {
    "model": "qwen3.8-flash", "enable_thinking": False, "reasoning_effort": "none",
    "messages": [{"role": "user", "content": "synthetic transcription prompt"}],
    "max_completion_tokens": 9000, "temperature": 0, "stream": True,
}
OCR_ESTIMATE = dashscope_budget.estimate_request_tokens(OCR_PAYLOAD)
USAGE = {"prompt_tokens": 2100, "completion_tokens": 400, "total_tokens": 2500,
         "prompt_tokens_details": {"cached_tokens": 0}}


def sse(*events, done=True):
    text = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    return text + ("data: [DONE]\n\n" if done else "")


def stream_with_usage(usage, content='{"ok": true}', model="qwen3.8-flash"):
    # Usage arrives in its own event after finish_reason, as DashScope streams it.
    return sse(
        {"model": model, "choices": [{"index": 0, "delta": {"content": content[:5]}}]},
        {"model": model, "choices": [{"index": 0, "delta": {"content": content[5:]},
                                      "finish_reason": "stop"}]},
        {"model": model, "choices": [], "usage": usage},
    )


def http_response(text, status=200):
    return httpx.Response(status, text=text, request=httpx.Request("POST", URL))


async def window(limiter, redis, provider=FLASH):
    if redis is not None:
        totals = await redis.hgetall(limiter._rate_budget_totals_key(provider))
        return int(totals.get("requests", 0)), int(totals.get("tokens", 0))
    live = [item for item in limiter._rate_budget_windows.get(provider, [])
            if item[0] > time.time() - 60]
    return len(live), sum(item[1] for item in live)


def stub_http(monkeypatch, *outcomes):
    """Patch httpx.AsyncClient; each post consumes one response/exception."""
    sent, queue = [], list(outcomes)

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, _url, *, headers, json):
            sent.append(json)
            outcome = queue.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    monkeypatch.setattr(ocr.httpx, "AsyncClient", Client)
    return sent


def transport_error(name):
    request = httpx.Request("POST", URL)
    if name == "cancelled_in_flight":
        return asyncio.CancelledError()
    return {
        "connect_error": httpx.ConnectError, "connect_timeout": httpx.ConnectTimeout,
        "pool_timeout": httpx.PoolTimeout, "proxy_error": httpx.ProxyError,
        "read_timeout": httpx.ReadTimeout, "read_error": httpx.ReadError,
        "write_error": httpx.WriteError, "remote_protocol": httpx.RemoteProtocolError,
    }[name]("synthetic", request=request)


async def post_ocr(payload=None):
    return await ocr._post_vision_request(URL, {}, dict(payload or OCR_PAYLOAD), "test-only-key")


@pytest.mark.parametrize("failure", [
    "lease_timeout", "lease_cancelled", "connect_error", "connect_timeout",
    "pool_timeout", "proxy_error",
])
def test_ocr_transport_refunds_requests_that_never_left(backend, monkeypatch, failure):
    async def run():
        async with workers(backend) as (limiter, _, redis):
            monkeypatch.setattr(ocr, "get_model_limiter", lambda: limiter)
            if failure.startswith("lease"):
                async def acquire(**_kwargs):
                    if failure == "lease_timeout":
                        raise TimeoutError("timed out waiting for shared model capacity")
                    raise asyncio.CancelledError()
                monkeypatch.setattr(limiter, "acquire", acquire)
                sent = stub_http(monkeypatch)
                expected = TimeoutError if failure == "lease_timeout" else asyncio.CancelledError
            else:
                sent = stub_http(monkeypatch, transport_error(failure))
                expected = httpx.TransportError
            with pytest.raises(expected):
                await post_ocr()
            assert len(sent) == (0 if failure.startswith("lease") else 1)
            assert await window(limiter, redis) == (0, 0)
    asyncio.run(run())


@pytest.mark.parametrize("failure", [
    "read_timeout", "read_error", "write_error", "remote_protocol", "cancelled_in_flight",
])
def test_ocr_transport_keeps_estimate_once_bytes_may_have_left(backend, monkeypatch, failure):
    async def run():
        async with workers(backend) as (limiter, _, redis):
            monkeypatch.setattr(ocr, "get_model_limiter", lambda: limiter)
            stub_http(monkeypatch, transport_error(failure))
            with pytest.raises((httpx.TransportError, asyncio.CancelledError)):
                await post_ocr()
            assert await window(limiter, redis) == (1, OCR_ESTIMATE)
    asyncio.run(run())


def test_ocr_transport_settles_stream_usage_after_finish_exactly_once(backend, monkeypatch):
    async def run():
        async with workers(backend) as (limiter, _, redis):
            monkeypatch.setattr(ocr, "get_model_limiter", lambda: limiter)
            settled = []
            original = limiter.settle_rate_budget

            async def spy(reservation, *, actual_tokens):
                settled.append(actual_tokens)
                return await original(reservation, actual_tokens=actual_tokens)

            monkeypatch.setattr(limiter, "settle_rate_budget", spy)
            stub_http(monkeypatch, http_response(stream_with_usage(USAGE)))
            response = await post_ocr()
            assert settled == [2500]
            assert await window(limiter, redis) == (1, 2500)
            # The caller's own parse still sees whole content and usage once.
            parsed = ocr._parse_streamed_vision_response(response)
            assert parsed["choices"][0]["message"]["content"] == '{"ok": true}'
            assert parsed["choices"][0]["finish_reason"] == "stop"
            assert parsed["usage"] == USAGE
    asyncio.run(run())


@pytest.mark.parametrize("body,status", [
    (sse({"choices": [{"delta": {"content": "{}"}, "finish_reason": "stop"}]}), 200),
    (sse({"choices": [{"delta": {"content": "{}"}, "finish_reason": "stop"}], "usage": None},
         {"choices": [], "usage": None}), 200),
    (stream_with_usage({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 99}), 200),
    (stream_with_usage({"input_tokens": 10, "prompt_tokens": 11, "output_tokens": 5}), 200),
    (stream_with_usage(USAGE) + 'data: {"usage": {"total_tokens": \n\n', 200),
    (json.dumps({"error": {"code": "Throttling", "message": "synthetic"}}), 429),
    (json.dumps({"error": {"code": "InternalError"}}), 500),
])
def test_ocr_transport_keeps_estimate_for_unknown_usage(backend, monkeypatch, body, status):
    async def run():
        async with workers(backend) as (limiter, _, redis):
            monkeypatch.setattr(ocr, "get_model_limiter", lambda: limiter)
            stub_http(monkeypatch, http_response(body, status))
            response = await post_ocr()
            assert response.status_code == status
            assert await window(limiter, redis) == (1, OCR_ESTIMATE)
    asyncio.run(run())


def test_coordinator_failure_never_replaces_the_provider_outcome(backend, monkeypatch):
    async def run():
        async with workers(backend) as (limiter, _, redis):
            monkeypatch.setattr(ocr, "get_model_limiter", lambda: limiter)

            async def broken(*_args, **_kwargs):
                raise RuntimeError("synthetic coordinator outage")

            monkeypatch.setattr(limiter, "settle_rate_budget", broken)
            stub_http(monkeypatch, http_response(stream_with_usage(USAGE)))
            assert (await post_ocr()).status_code == 200
            assert await window(limiter, redis) == (1, OCR_ESTIMATE)

            async def lease_timeout(**_kwargs):
                raise TimeoutError("timed out waiting for shared model capacity")

            monkeypatch.setattr(limiter, "cancel_rate_budget", broken)
            monkeypatch.setattr(limiter, "acquire", lease_timeout)
            with pytest.raises(TimeoutError, match="shared model capacity"):
                await post_ocr()
            assert await window(limiter, redis) == (2, 2 * OCR_ESTIMATE)
    asyncio.run(run())


def test_student_ocr_retry_settles_each_attempt_and_keeps_contract(
    backend, monkeypatch, tmp_path,
):
    async def run():
        async with workers(backend) as (limiter, _, redis):
            monkeypatch.setattr(ocr, "get_model_limiter", lambda: limiter)
            monkeypatch.setattr(ocr, "_vision_pool", None)
            monkeypatch.setattr(ocr, "RETRY_DELAY", 0)
            monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://unit.invalid/v1")
            first = sse(
                {"model": "qwen3.8-flash", "choices": [{"delta": {
                    "reasoning_content": "synthetic", "content": '{"unsafe": true}'},
                    "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 3000, "completion_tokens": 700,
                                          "total_tokens": 3700}},
            )
            second = stream_with_usage(USAGE, content='{"safe": true}',
                                       model="qwen3.8-flash-2026-09")
            sent = stub_http(monkeypatch, http_response(first), http_response(second))
            image = tmp_path / "synthetic.jpg"
            image.write_bytes(b"jpeg-probe")
            receipt = {}
            result = await ocr._call_vision_request(
                str(image), "return json", max_tokens=100_000, request_receipt=receipt,
            )
            assert result == '{"safe": true}'
            assert len(sent) == 2
            for payload in sent:
                assert payload["model"] == "qwen3.8-flash"
                assert payload["enable_thinking"] is False
                assert payload["reasoning_effort"] == "none"
                assert payload["temperature"] == 0
                assert payload["max_completion_tokens"] == 9000
                assert payload["stream"] is True
                assert "stream_options" not in payload
            assert await window(limiter, redis) == (2, 3700 + 2500)
            assert receipt["attempts"] == 2
            assert receipt["requested_model"] == "qwen3.8-flash"
            assert receipt["response_model"] == "qwen3.8-flash-2026-09"
            assert receipt["finish_reason"] == "stop"
    asyncio.run(run())


def _flash_runtime(monkeypatch, limiter):
    from services import flash_grading as flash
    from services.key_pool import KeyPool

    monkeypatch.setattr(ocr, "_vision_pool", KeyPool(["test-only-flash-key"], max_concurrent_per_key=None))
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://unit.invalid/v1")
    monkeypatch.setattr(ocr, "get_model_limiter", lambda: limiter)
    monkeypatch.setattr(flash, "_credential_failures", {})
    monkeypatch.setattr(flash, "_semaphore", None)
    monkeypatch.setattr(flash, "_semaphore_loop", None)
    return flash


def test_flash_grading_settles_once_without_a_second_ocr_parse(backend, monkeypatch):
    async def run():
        async with workers(backend) as (limiter, _, redis):
            flash = _flash_runtime(monkeypatch, limiter)
            content = json.dumps({"results": {"1": {
                "result": "correct", "explanation": "", "hint": "",
                "model_answer": "", "standard_answer": ""}}})
            usage = {"prompt_tokens": 900, "completion_tokens": 3100, "total_tokens": 4000,
                     "completion_tokens_details": {"reasoning_tokens": 2800}}
            sent = stub_http(monkeypatch, http_response(
                stream_with_usage(usage, content=content, model=flash.FLASH_MODEL)))

            def duplicate_parse(*_args, **_kwargs):
                raise AssertionError("Flash must not re-parse the stream with the OCR parser")

            monkeypatch.setattr(ocr, "_parse_streamed_vision_response", duplicate_parse)
            result = await flash.call_flash(
                [{"role": "user", "content": "{}"}], phase="text", teacher_id="teacher_1")
            assert json.loads(result["content"]) == json.loads(content)
            assert result["usage"] == usage
            assert sent[0]["max_tokens"] == 8000
            assert await window(limiter, redis) == (1, 4000)
    asyncio.run(run())


def test_flash_grading_rejection_keeps_conservative_estimate(backend, monkeypatch):
    async def run():
        async with workers(backend) as (limiter, _, redis):
            flash = _flash_runtime(monkeypatch, limiter)
            sent = stub_http(monkeypatch, http_response('{"error": {"code": "Throttling"}}', 429))
            with pytest.raises(flash.FlashGradingError, match="HTTP 429"):
                await flash.call_flash([{"role": "user", "content": "{}"}], phase="text")
            estimate = dashscope_budget.estimate_request_tokens(sent[0])
            assert estimate >= 8000
            assert await window(limiter, redis) == (1, estimate)
    asyncio.run(run())


TEMPLATE_JSON = {
    "model": "qwen3.8-flash",
    "messages": [{"role": "user", "content": [
        {"type": "text", "text": "synthetic column prompt"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA", "detail": "high"}},
    ]}],
    "temperature": 0, "max_tokens": 8192, "stream": False,
    "enable_thinking": False, "reasoning_effort": "none",
}
TEMPLATE_USAGE = {"prompt_tokens": 1500, "completion_tokens": 60, "total_tokens": 1560,
                  "completion_tokens_details": {"reasoning_tokens": 0}}


@pytest.mark.parametrize("outcome", [
    "lease_timeout", "lease_cancelled", "connect_error", "read_error", "usage", "no_usage",
])
def test_template_transport_lifecycle(backend, monkeypatch, outcome):
    from services import template_column_layout as mimo

    async def run():
        async with workers(backend) as (limiter, _, redis):
            route = SimpleNamespace(model="qwen3.8-flash", max_tokens=8192)
            reservation = await mimo._acquire_template_rate_budget(
                limiter=limiter, route=route, request_json=TEMPLATE_JSON)
            estimate = dashscope_budget.estimate_request_tokens(TEMPLATE_JSON, 8192)
            assert await window(limiter, redis) == (1, estimate)
            sent = []
            body = {"choices": [{"message": {"content": "{}"}}]}
            if outcome == "usage":
                body["usage"] = TEMPLATE_USAGE

            class Client:
                async def post(self, _url, *, headers, json):
                    sent.append(json)
                    if outcome in {"connect_error", "read_error"}:
                        raise transport_error(outcome)
                    return httpx.Response(200, json=body, request=httpx.Request("POST", URL))

            if outcome.startswith("lease"):
                async def acquire(**_kwargs):
                    if outcome == "lease_timeout":
                        raise TimeoutError("timed out waiting for shared model capacity")
                    raise asyncio.CancelledError()
                monkeypatch.setattr(limiter, "acquire", acquire)

            call = mimo._post_with_shared_lease(
                client=Client(), limiter=limiter, api_key="test-only-key",
                base_url="https://unit.invalid/v1", pool=None, teacher_id="teacher_1",
                request_json=TEMPLATE_JSON, route=route, rate_budget_reservation=reservation,
            )
            expected_window = {
                "lease_timeout": (0, 0), "lease_cancelled": (0, 0), "connect_error": (0, 0),
                "read_error": (1, estimate), "usage": (1, 1560), "no_usage": (1, estimate),
            }[outcome]
            if outcome in {"usage", "no_usage"}:
                assert (await call).status_code == 200
            else:
                with pytest.raises((TimeoutError, asyncio.CancelledError, httpx.TransportError)):
                    await call
            assert len(sent) == (0 if outcome.startswith("lease") else 1)
            assert await window(limiter, redis) == expected_window
    asyncio.run(run())


def test_template_split_full_path_settles_actual_usage(backend, monkeypatch, tmp_path):
    from PIL import Image

    from services import template_column_layout as mimo
    from services.key_pool import KeyPool

    image_path = tmp_path / "synthetic-template.png"
    Image.new("RGB", (800, 1200), "white").save(image_path)
    pool = KeyPool(["test-only-key"], max_concurrent_per_key=4,
                   base_urls=["https://unit.invalid/v1"])
    monkeypatch.setattr(template_column_layout_provider, "_resolve_template_key_pool", lambda: pool)
    monkeypatch.setattr(mimo, "_resolve_template_key_pool", lambda: pool)
    monkeypatch.setattr(
        template_column_layout_provider,
        "_template_capability_unavailable_until",
        0.0,
    )
    calls = []

    async def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "synthetic", "usage": TEMPLATE_USAGE,
            "choices": [{"message": {"content": json.dumps({
                "column_count": 1,
                "columns": [{"column_index": 1, "x1_1000": 0, "x2_1000": 1000}],
            })}}],
        })

    async def run():
        async with workers(backend) as (limiter, _, redis):
            monkeypatch.setattr(
                template_column_layout_provider,
                "get_model_limiter",
                lambda: limiter,
            )
            monkeypatch.setattr(mimo, "get_model_limiter", lambda: limiter)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await mimo.recognize_column_layout(image_path, client=client)
            assert result["summary"]["total"] == 1
            assert len(calls) == 1
            assert calls[0]["enable_thinking"] is False
            assert calls[0]["reasoning_effort"] == "none"
            assert await window(limiter, redis) == (1, 1560)
    asyncio.run(run())


def test_settlement_near_expiry_does_not_restart_rolling_window(backend, clock):
    async def run():
        async with workers(backend) as (a, b, redis):
            reservation = await a.reserve_rate_budget(**budget())
            clock.epoch = 1059.0
            assert await b.settle_rate_budget(reservation, actual_tokens=15)
            clock.epoch = 1060.0
            assert await a.settle_rate_budget(reservation, actual_tokens=999) is False
            assert await a.cancel_rate_budget(reservation) is False
            await assert_totals(a, redis, 0, 0)
            await b.reserve_rate_budget(**budget())
            await assert_totals(a, redis, 1, 10)
    asyncio.run(run())


def test_local_30000_expired_legacy_entries_cleanup(clock):
    async def run():
        limiter = mc.NoopModelLimiter()
        limiter._rate_budget_windows[PROVIDER] = [(939.0, 1)] * 30_000 + [(999.0, 4)]
        reservation = await limiter.reserve_rate_budget(**budget(reserved_tokens=6))
        await assert_totals(limiter, None, 2, 10)
        assert await limiter.settle_rate_budget(reservation, actual_tokens=1)
        await assert_totals(limiter, None, 2, 5)
    asyncio.run(run())
