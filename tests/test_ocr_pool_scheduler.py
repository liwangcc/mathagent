import asyncio
import json
from dataclasses import replace
from unittest.mock import patch

import httpx
import pytest
import fakeredis.aioredis

from services.ocr_pool_scheduler import (
    LocalPoolCoordinator,
    OcrPoolError,
    OcrPoolScheduler,
    PoolSpec,
    RedisPoolCoordinator,
    _classify_http_failure,
)
from services import template_recognition


def _pool(
    name: str,
    *,
    hard: int = 4,
    borrowed: int | None = None,
    ttl: float = 1.0,
) -> PoolSpec:
    return PoolSpec(
        name=name,
        base_url=f"https://{name}.example.invalid/v1",
        api_key=f"test-{name}-credential",
        model="gpt-test",
        max_rpm=60000,
        hard_in_flight=hard,
        first_event_timeout_s=0.5,
        attempt_ttl_s=ttl,
        burst=100,
        borrowed_in_flight=borrowed,
    )


def _scheduler(
    transport,
    *,
    share: float = 0.2,
    max_batch_retries: int = 3,
    circuit_threshold: int = 100,
    primary_soft: int = 2,
) -> OcrPoolScheduler:
    coordinator = LocalPoolCoordinator(global_rpm=60000, global_burst=100)
    return OcrPoolScheduler(
        primary=_pool("ys", hard=4),
        failover_pool=_pool("proxy", hard=4),
        accelerator=_pool("hk", hard=3, borrowed=2),
        retry_pool=_pool("vip", hard=2),
        coordinator=coordinator,
        global_first_attempt_rpm=60000,
        primary_soft_in_flight=primary_soft,
        accelerator_share=share,
        batch_deadline_s=10.0,
        final_guard_s=0.1,
        max_batch_retries=max_batch_retries,
        circuit_failure_threshold=circuit_threshold,
        circuit_cooldown_s=1.0,
        connect_timeout_s=0.5,
        write_timeout_s=0.5,
        sse_idle_timeout_s=0.5,
        transport=transport,
    )


def test_batch_deadline_uses_actual_paper_count_and_six_minute_floor():
    scheduler = _scheduler(None)
    scheduler.batch_deadline_s = 1440.0
    scheduler.batch_target_papers = 60
    scheduler.batch_target_deadline_s = 360.0

    assert scheduler._batch_deadline_for_papers(1) == 360.0
    assert scheduler._batch_deadline_for_papers(60) == 360.0
    assert scheduler._batch_deadline_for_papers(61) == 720.0
    assert scheduler._batch_deadline_for_papers(120) == 720.0


class _AsyncSseStream(httpx.AsyncByteStream):
    def __init__(self, events):
        self._events = list(events)

    async def __aiter__(self):
        for event in self._events:
            if event == "[DONE]":
                data = event
            else:
                data = json.dumps(event, separators=(",", ":"))
            yield f"data: {data}\n\n".encode("utf-8")

    async def aclose(self):
        return None


class _DelayedAsyncSseStream(_AsyncSseStream):
    """Emit each event after its paired delay in seconds."""

    async def __aiter__(self):
        for delay_s, event in self._events:
            await asyncio.sleep(delay_s)
            if event == "[DONE]":
                data = event
            else:
                data = json.dumps(event, separators=(",", ":"))
            yield f"data: {data}\n\n".encode("utf-8")


def _install_responses_transport(
    monkeypatch,
    events,
    captured,
    *,
    stream_type=_AsyncSseStream,
    header_delay_s=0.0,
):
    import services.ocr_pool_scheduler as scheduler_module

    async_client = httpx.AsyncClient

    async def handler(request):
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        if header_delay_s:
            await asyncio.sleep(header_delay_s)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=stream_type(events),
        )

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs):
        captured["timeout"] = kwargs["timeout"]
        return async_client(transport=transport, **kwargs)

    monkeypatch.setattr(scheduler_module.httpx, "AsyncClient", client_factory)


def _install_chat_transport(monkeypatch, events, captured, *, stream_type=_AsyncSseStream):
    import services.ocr_pool_scheduler as scheduler_module

    async_client = httpx.AsyncClient

    async def handler(request):
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=stream_type(events),
        )

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs):
        return async_client(transport=transport, **kwargs)

    monkeypatch.setattr(scheduler_module.httpx, "AsyncClient", client_factory)


def test_chat_completions_stream_uses_verified_hk_contract(monkeypatch):
    captured = {}
    _install_chat_transport(
        monkeypatch,
        [
            {"choices": [{"delta": {"reasoning_content": "hidden"}}]},
            {"choices": [{"delta": {"content": '{"ok":true}'}}]},
            "[DONE]",
        ],
        captured,
    )
    scheduler = _scheduler(None)
    pool = replace(
        scheduler.accelerator,
        base_url="https://hikari.example.invalid/v1",
        api_protocol="chat_completions",
    )
    payload = {
        "input": [{
            "role": "user",
            "content": [
                {"type": "input_text", "text": "[task=student_transcription_only] OCR"},
                {"type": "input_image", "image_url": "data:image/jpeg;base64,abc"},
            ],
        }],
        "max_output_tokens": 6000,
    }

    result = asyncio.run(
        scheduler._send_stream(pool, payload, "request-chat-contract")
    )

    assert result == '{"ok":true}'
    assert captured["url"] == "https://hikari.example.invalid/v1/chat/completions"
    body = captured["body"]
    assert body["model"] == "gpt-test"
    assert body["temperature"] == 0
    assert body["stream"] is True
    assert body["model_reasoning_effort"] == "none"
    assert body["max_tokens"] == 6000
    assert body["messages"][0]["content"][0] == {
        "type": "text",
        "text": "[task=student_transcription_only] OCR",
    }
    assert body["messages"][0]["content"][1]["type"] == "image_url"
    assert "reasoning" not in body


def test_chat_stream_stops_at_finish_reason_instead_of_waiting_for_done(monkeypatch):
    """Provider finish_reason ends the answer; the request must not idle to timeout."""
    captured = {}
    _install_chat_transport(
        monkeypatch,
        [
            (0.0, {"choices": [{"delta": {"content": '{"ok":true}'}, "finish_reason": "stop"}]}),
            (5.0, "[DONE]"),
        ],
        captured,
        stream_type=_DelayedAsyncSseStream,
    )
    scheduler = _scheduler(None)
    pool = replace(
        scheduler.accelerator,
        base_url="https://hikari.example.invalid/v1",
        api_protocol="chat_completions",
    )
    payload = {"input": [{"role": "user", "content": [{"type": "input_text", "text": "Q"}]}]}

    # Without the terminator check this raises sse_idle_timeout while the model is done.
    assert asyncio.run(
        scheduler._send_stream(pool, payload, "request-finish-reason")
    ) == '{"ok":true}'


def test_chat_stream_rejects_truncated_answer_instead_of_accepting_it(monkeypatch):
    """finish_reason=length means cut-off JSON; it must not pass as an answer."""
    captured = {}
    _install_chat_transport(monkeypatch, [
        {"choices": [{"delta": {"content": '{"references":'}, "finish_reason": "length"}]},
    ], captured)
    scheduler = _scheduler(None)
    pool = replace(
        scheduler.accelerator,
        base_url="https://hikari.example.invalid/v1",
        api_protocol="chat_completions",
    )
    payload = {"input": [{"role": "user", "content": [{"type": "input_text", "text": "Q"}]}]}

    with pytest.raises(OcrPoolError, match="truncated_stream"):
        asyncio.run(
            scheduler._send_stream(pool, payload, "request-truncated")
        )


def test_chat_refusal_rejects_even_when_content_is_valid_json(monkeypatch):
    _install_chat_transport(monkeypatch, [
        {"choices": [{"delta": {"content": '{"answers":[]}'}}]},
        {"choices": [{"delta": {"refusal": "unable to continue"}, "finish_reason": "stop"}]},
    ], {})
    scheduler = _scheduler(None)
    pool = replace(scheduler.accelerator, api_protocol="chat_completions")
    payload = {"input": [{"role": "user", "content": [{"type": "input_text", "text": "Q"}]}]}
    with pytest.raises(OcrPoolError, match="model_refused") as error:
        asyncio.run(scheduler._send_stream(pool, payload, "request-refused"))
    assert error.value.retryable is False


def test_answer_sheet_locator_chat_maps_high_without_blank_anchor_token_override(monkeypatch):
    from services.answer_sheet_template_locator import _prompt

    captured = {}
    _install_chat_transport(monkeypatch, [
        {"choices": [{"delta": {"content": '{"ok":true}'}}]}, "[DONE]",
    ], captured)
    scheduler = _scheduler(None)
    pool = replace(scheduler.accelerator, model="qwen3.8-max", api_protocol="chat_completions")
    prompt = _prompt([{"question_no": "1", "question_type": "choice"}], 1, 1,
                     image_size=(1600, 2000))
    payload = {
        "input": [{"role": "user", "content": [
            {"type": "input_text", "text": prompt},
            {"type": "input_image", "image_url": "data:image/png;base64,abc"},
        ]}],
        "model_reasoning_effort": "high", "max_output_tokens": 4096,
    }

    assert asyncio.run(scheduler._send_stream(pool, payload, "sheet-locator-wire")) == '{"ok":true}'
    body = captured["body"]
    assert body["model"] == "qwen3.8-max"
    assert body["reasoning_effort"] == "high"
    assert body["enable_thinking"] is True
    assert body["max_tokens"] == 4096
    assert body["temperature"] == 0 and body["stream"] is True
    assert "model_reasoning_effort" not in body
    assert body["messages"][0]["content"][0]["text"] == prompt
    assert "你负责为空白数学试卷上的每道题选择一个题号标记位置" not in prompt
    assert payload["model_reasoning_effort"] == "high"


@pytest.mark.parametrize("prompt", [
    "你是模板注册阶段的数学试卷答案位置定位模型。",
    "普通任务：文中引用“你是空白答题卡模板的版面粗定位模型。”不代表该定位任务。",
    "[task=student_transcription_only] OCR",
    "普通空白试卷识别任务",
])
def test_qwen_effort_mapping_is_independent_of_task_prompt(monkeypatch, prompt):
    captured = {}
    _install_chat_transport(monkeypatch, [
        {"choices": [{"delta": {"content": '{"ok":true}'}}]}, "[DONE]",
    ], captured)
    scheduler = _scheduler(None)
    pool = replace(scheduler.accelerator, model="qwen3.8-max", api_protocol="chat_completions")
    payload = {"input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
               "model_reasoning_effort": "high", "max_output_tokens": 6000}
    assert asyncio.run(scheduler._send_stream(pool, payload, "unchanged-chat-wire")) == '{"ok":true}'
    body = captured["body"]
    assert body["reasoning_effort"] == "high"
    assert body["enable_thinking"] is True
    assert body["max_tokens"] == 6000
    assert "model_reasoning_effort" not in body


def test_answer_sheet_locator_responses_contract_unchanged(monkeypatch):
    from services.answer_sheet_template_locator import _prompt

    captured = {}
    _install_responses_transport(monkeypatch, [
        {"type": "response.output_text.delta", "delta": '{"ok":true}'},
        {"type": "response.completed", "response": {"status": "completed"}},
    ], captured)
    scheduler = _scheduler(None)
    prompt = _prompt([{"question_no": "1", "question_type": "fill"}], 1, 1)
    payload = {"input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
               "model_reasoning_effort": "high", "max_output_tokens": 4096}
    assert asyncio.run(scheduler._send_stream(scheduler.primary, payload, "unchanged-responses-wire")) == '{"ok":true}'
    body = captured["body"]
    assert body["reasoning"] == {"effort": "high"}
    assert body["max_output_tokens"] == 4096
    assert "reasoning_effort" not in body and "enable_thinking" not in body
    assert "model_reasoning_effort" not in body


def test_responses_stream_allows_first_sse_event_past_idle_timeout(monkeypatch):
    captured = {}
    _install_responses_transport(
        monkeypatch,
        [
            (0.12, {"type": "response.created", "response": {"status": "in_progress"}}),
            (0.0, {"type": "response.output_text.delta", "delta": '{"ok":true}'}),
            (0.0, {"type": "response.completed", "response": {"status": "completed"}}),
        ],
        captured,
        stream_type=_DelayedAsyncSseStream,
    )
    scheduler = _scheduler(None)
    scheduler.sse_idle_timeout_s = 0.05
    pool = replace(
        scheduler.primary,
        first_event_timeout_s=0.4,
        attempt_ttl_s=1.0,
    )

    result = asyncio.run(
        scheduler._send_stream(
            pool,
            {"input": [{"role": "user", "content": "test"}]},
            "request-first-event",
        )
    )

    assert result == '{"ok":true}'
    assert captured["timeout"].read == 0.4


def test_responses_stream_enforces_idle_timeout_after_first_sse_event(monkeypatch):
    captured = {}
    _install_responses_transport(
        monkeypatch,
        [
            (0.0, {"type": "response.created", "response": {"status": "in_progress"}}),
            (0.12, {"type": "response.output_text.delta", "delta": '{"ok":true}'}),
        ],
        captured,
        stream_type=_DelayedAsyncSseStream,
    )
    scheduler = _scheduler(None)
    scheduler.sse_idle_timeout_s = 0.05
    pool = replace(
        scheduler.primary,
        first_event_timeout_s=0.4,
        attempt_ttl_s=1.0,
    )

    with pytest.raises(OcrPoolError, match="sse_idle_timeout"):
        asyncio.run(
            scheduler._send_stream(
                pool,
                {"input": [{"role": "user", "content": "test"}]},
                "request-idle-timeout",
            )
        )


def test_responses_stream_keeps_header_timeout_at_first_event_budget(monkeypatch):
    captured = {}
    _install_responses_transport(
        monkeypatch,
        [],
        captured,
        header_delay_s=0.12,
    )
    scheduler = _scheduler(None)
    scheduler.sse_idle_timeout_s = 0.05
    pool = replace(
        scheduler.primary,
        first_event_timeout_s=0.05,
        attempt_ttl_s=1.0,
    )

    with pytest.raises(OcrPoolError, match="response_header_timeout"):
        asyncio.run(
            scheduler._send_stream(
                pool,
                {"input": [{"role": "user", "content": "test"}]},
                "request-header-timeout",
            )
        )


@pytest.mark.parametrize(
    "base_url",
    [
        "https://ys.example.invalid/v1",
        "https://ys.example.invalid/v1/chat/completions",
        "https://ys.example.invalid/v1/responses",
    ],
)
def test_responses_stream_uses_high_and_completes_without_done(
    monkeypatch, base_url
):
    captured = {}
    _install_responses_transport(
        monkeypatch,
        [
            {"type": "response.created", "response": {"status": "in_progress"}},
            {"type": "response.output_text.delta", "delta": '{"ok":'},
            {"type": "response.output_text.delta", "delta": "true}"},
            {"type": "response.completed", "response": {"status": "completed"}},
        ],
        captured,
    )
    scheduler = _scheduler(None)
    pool = replace(scheduler.primary, base_url=base_url)
    payload = {
        "input": [{"role": "user", "content": "test"}],
        "max_output_tokens": 8192,
        "reasoning": {"effort": "low"},
        "stream": False,
        "temperature": 0,
        "reasoning_effort": "low",
        "messages": [{"role": "user", "content": "legacy"}],
        "max_completion_tokens": 100,
    }

    result = asyncio.run(
        scheduler._send_stream(pool, payload, "request-1")
    )

    assert result == '{"ok":true}'
    assert captured["url"] == "https://ys.example.invalid/v1/responses"
    assert captured["headers"]["accept"] == "text/event-stream"
    body = captured["body"]
    assert body["model"] == "gpt-test"
    assert body["reasoning"] == {"effort": "high"}
    assert body["stream"] is True
    assert body["max_output_tokens"] == 8192
    assert "temperature" not in body
    assert "reasoning_effort" not in body
    assert "messages" not in body
    assert "max_completion_tokens" not in body


def test_student_transcription_marker_uses_its_configured_reasoning_effort(
    monkeypatch,
):
    import config

    captured = {}
    _install_responses_transport(
        monkeypatch,
        [
            {"type": "response.output_text.delta", "delta": '{"ok":true}'},
            {"type": "response.completed", "response": {"status": "completed"}},
        ],
        captured,
    )
    monkeypatch.setattr(
        config,
        "OCR_TRANSCRIPTION_REASONING_EFFORT",
        "low",
        raising=False,
    )
    scheduler = _scheduler(None)
    payload = {
        "input": [{"role": "user", "content": [{
            "type": "input_text",
            "text": "[task=student_transcription_only] only transcribe",
        }]}],
        "max_output_tokens": 25000,
    }

    result = asyncio.run(scheduler._send_stream(
        scheduler.primary,
        payload,
        "request-transcription",
    ))

    assert result == '{"ok":true}'
    assert captured["body"]["reasoning"] == {"effort": "low"}


def test_responses_stream_accepts_completed_response_output_fallback(monkeypatch):
    captured = {}
    _install_responses_transport(
        monkeypatch,
        [{
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [{
                    "type": "message",
                    "content": [{"type": "output_text", "text": "buffered"}],
                }],
            },
        }],
        captured,
    )
    scheduler = _scheduler(None)

    result = asyncio.run(
        scheduler._send_stream(
            scheduler.primary,
            {"input": [{"role": "user", "content": "test"}]},
            "request-2",
        )
    )

    assert result == "buffered"


@pytest.mark.parametrize(
    ("event", "error_code"),
    [
        ({"type": "response.failed", "response": {"status": "failed"}}, "response_failed"),
        (
            {"type": "response.incomplete", "response": {"status": "incomplete"}},
            "response_incomplete",
        ),
        (
            {"type": "response.completed", "response": {"status": "incomplete"}},
            "response_terminal_incomplete",
        ),
        ({"type": "error", "message": "upstream failed"}, "stream_error"),
    ],
)
def test_responses_stream_rejects_failure_events(monkeypatch, event, error_code):
    captured = {}
    _install_responses_transport(monkeypatch, [event], captured)
    scheduler = _scheduler(None)

    with pytest.raises(OcrPoolError) as exc_info:
        asyncio.run(
            scheduler._send_stream(
                scheduler.primary,
                {"input": [{"role": "user", "content": "test"}]},
                "request-failure",
            )
        )

    assert exc_info.value.code == error_code
    assert exc_info.value.retryable is True


def test_responses_stream_does_not_accept_done_without_completed(monkeypatch):
    captured = {}
    _install_responses_transport(
        monkeypatch,
        [
            {"type": "response.output_text.delta", "delta": "partial"},
            "[DONE]",
        ],
        captured,
    )
    scheduler = _scheduler(None)

    with pytest.raises(OcrPoolError) as exc_info:
        asyncio.run(
            scheduler._send_stream(
                scheduler.primary,
                {"input": [{"role": "user", "content": "test"}]},
                "request-incomplete",
            )
        )

    assert exc_info.value.code == "missing_completed"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_rpm", float("nan")),
        ("max_rpm", float("inf")),
        ("first_event_timeout_s", float("nan")),
        ("attempt_ttl_s", float("inf")),
    ],
)
def test_pool_spec_rejects_nonfinite_rate_and_timeout_values(field, value):
    with pytest.raises(ValueError):
        replace(_pool("invalid"), **{field: value})


def test_local_batch_and_retry_state_expires_and_can_be_reused():
    coordinator = LocalPoolCoordinator(global_rpm=60, global_burst=1)

    async def run():
        with (
            patch("services.ocr_pool_scheduler.time.time", return_value=1000.0),
            patch("services.ocr_pool_scheduler.time.monotonic", return_value=50.0),
        ):
            first = await coordinator.get_batch_start("batch", 10)
            assert await coordinator.get_batch_start("batch", 10) == first
            assert await coordinator.claim_retry("batch", "paper", 1, 10)
            assert await coordinator.claim_accelerator_probe("batch")
            await coordinator.add_priority_waiter("pool", "waiter", 10)

        with (
            patch("services.ocr_pool_scheduler.time.time", return_value=1011.0),
            patch("services.ocr_pool_scheduler.time.monotonic", return_value=61.0),
        ):
            second = await coordinator.get_batch_start("batch", 10)
            assert second == 1011.0
            assert second != first
            assert await coordinator.claim_retry("batch", "paper", 1, 10)
            assert await coordinator.accelerator_gate("batch") == "unknown"
            assert coordinator._priority_waiters == {}

    asyncio.run(run())


def test_retry_stagger_uses_jitter_and_stays_inside_deadline(monkeypatch):
    import services.ocr_pool_scheduler as scheduler_module

    async def transport(_pool, _payload, _request_id):
        return "ok"

    scheduler = _scheduler(transport)
    sleeps = []

    monkeypatch.setattr(scheduler_module.time, "time", lambda: 100.0)
    monkeypatch.setattr(
        scheduler_module,
        "jittered_backoff",
        lambda *_args, **_kwargs: 0.2,
    )

    async def record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(scheduler_module.asyncio, "sleep", record_sleep)

    async def run():
        await scheduler._sleep_before_retry(101.0)
        await scheduler._sleep_before_retry(100.12)

    asyncio.run(run())

    assert sleeps == pytest.approx([0.2, 0.12])


def test_first_accelerator_selected_request_probes_hk_then_batch_uses_both_pools():
    calls = []

    async def transport(pool, _payload, _request_id):
        calls.append(pool.name)
        return json.dumps({"pool": pool.name})

    scheduler = _scheduler(transport, share=0.5)

    selected_key = next(
        key
        for key in (f"paper-{index}" for index in range(100))
        if scheduler._prefer_accelerator(
            key,
            __import__("hashlib").sha256(key.encode("utf-8")).hexdigest()[:24],
        )
    )

    async def run():
        await scheduler.request(
            {},
            batch_key="batch-a",
            paper_key=selected_key,
            teacher_id="teacher-a",
        )
        for index in range(6):
            await scheduler.request(
                {},
                batch_key="batch-a",
                paper_key=f"paper-{index}",
                teacher_id="teacher-a",
            )

    asyncio.run(run())

    assert calls[0] == "hk"  # one same-workload health probe
    assert {"hk", "ys"}.issubset(calls)
    assert scheduler.stats()["first_accelerator"] == calls.count("hk")


def test_accelerator_share_is_a_stable_weighted_fraction():
    async def transport(_pool, _payload, _request_id):
        return '{"ok":true}'

    route_keys = [f"paper-{index}" for index in range(2000)]
    paper_hashes = {
        key: __import__("hashlib").sha256(key.encode("utf-8")).hexdigest()[:24]
        for key in route_keys
    }

    selected = {}
    for share in (0.1, 0.5, 1.0):
        scheduler = _scheduler(transport, share=share)
        selected[share] = {
            key
            for key in route_keys
            if scheduler._prefer_accelerator(key, paper_hashes[key])
        }

    assert 150 <= len(selected[0.1]) <= 250
    assert 900 <= len(selected[0.5]) <= 1100
    assert len(selected[1.0]) == len(route_keys)
    assert selected[0.1] < selected[0.5] < selected[1.0]

    scheduler = _scheduler(transport, share=0.1)
    assert {
        key
        for key in route_keys
        if scheduler._prefer_accelerator(key, paper_hashes[key])
    } == selected[0.1]


def test_accelerator_share_controls_actual_routing_across_small_batches():
    calls = []

    async def transport(pool, _payload, _request_id):
        calls.append(pool.name)
        return '{"ok":true}'

    scheduler = _scheduler(transport, share=0.2)
    # Stay inside the coordinator's initial global burst so this verifies the
    # configured routing share rather than capacity-driven spillover.
    route_keys = [f"paper-{index}" for index in range(80)]
    expected_accelerator = sum(
        scheduler._prefer_accelerator(
            key,
            __import__("hashlib").sha256(key.encode("utf-8")).hexdigest()[:24],
        )
        for key in route_keys
    )

    async def run():
        for index, route_key in enumerate(route_keys):
            await scheduler.request(
                {},
                batch_key=f"small-batch-{index}",
                paper_key=route_key,
                teacher_id="teacher-a",
            )

    asyncio.run(run())

    assert calls.count("hk") == expected_accelerator
    assert 8 <= calls.count("hk") <= 24
    assert calls.count("ys") == len(route_keys) - expected_accelerator


def test_zero_accelerator_share_routes_batch_and_interactive_requests_to_primary():
    calls = []

    async def transport(pool, _payload, _request_id):
        calls.append(pool.name)
        return json.dumps({"pool": pool.name})

    scheduler = _scheduler(transport, share=0.0)

    async def run():
        await scheduler.request(
            {},
            batch_key="batch-primary-only",
            paper_key="paper-batch",
            teacher_id="teacher-a",
        )
        await scheduler.request(
            {},
            batch_key="interactive-primary-only",
            paper_key="paper-interactive",
            teacher_id="teacher-a",
            priority="interactive",
        )

    asyncio.run(run())

    assert calls == ["ys", "ys"]
    assert scheduler.stats()["first_accelerator"] == 0


@pytest.mark.parametrize("status_code", [400, 409, 413, 415, 422])
def test_request_specific_http_failures_do_not_retry_or_poison_pool(status_code):
    assert _classify_http_failure(status_code) == (False, False)

    async def transport(_pool, _payload, _request_id):
        raise OcrPoolError(
            f"http_{status_code}",
            retryable=False,
            transport_failure=False,
            status_code=status_code,
        )

    scheduler = _scheduler(transport, share=0.0, circuit_threshold=1)

    async def run():
        with pytest.raises(OcrPoolError, match=f"http_{status_code}"):
            await scheduler.request(
                {},
                batch_key=f"request-error-{status_code}",
                paper_key="paper-1",
                teacher_id="teacher-a",
            )
        assert await scheduler.coordinator.is_circuit_open(
            scheduler.primary.coordination_key
        ) is False
        assert scheduler.coordinator._consecutive_failures.get(
            scheduler.primary.coordination_key, 0
        ) == 0

    asyncio.run(run())


@pytest.mark.parametrize("status_code", [401, 403, 404, 408, 429, 500, 503])
def test_shared_http_failures_are_retryable_on_another_pool(status_code):
    assert _classify_http_failure(status_code) == (True, True)


def test_circuited_primary_switches_future_requests_to_failover_but_retries_vip():
    calls = []

    async def transport(pool, _payload, _request_id):
        calls.append(pool.name)
        if pool.name == "ys":
            raise OcrPoolError("primary_down", retryable=True, transport_failure=True)
        return '{"ok":true}'

    scheduler = _scheduler(transport, share=0.0, circuit_threshold=1)

    async def run():
        first = await scheduler.request(
            {}, batch_key="batch-switch", paper_key="paper-1", teacher_id="teacher-a"
        )
        second = await scheduler.request(
            {}, batch_key="batch-switch-2", paper_key="paper-2", teacher_id="teacher-a"
        )
        return json.loads(first), json.loads(second)

    first, second = asyncio.run(run())

    assert first["ok"] is True
    assert second["ok"] is True
    assert calls == ["ys", "vip", "proxy"]


@pytest.mark.parametrize("status_code", [401, 403, 404])
def test_fatal_http_circuit_permanently_disables_same_pool_fingerprint(status_code):
    async def transport(_pool, _payload, _request_id):
        raise OcrPoolError(
            f"http_{status_code}",
            retryable=status_code in {401, 403},
            transport_failure=True,
            status_code=status_code,
        )

    scheduler = _scheduler(transport, share=0.0)
    same_pool = replace(scheduler.primary)

    async def run():
        with pytest.raises(OcrPoolError, match=f"http_{status_code}"):
            await scheduler._execute(scheduler.primary, {}, None)
        # A late success from an already in-flight request must not clear a
        # fatal credential/endpoint/model circuit.
        await scheduler.coordinator.record_success(same_pool.coordination_key)
        with patch("services.ocr_pool_scheduler.time.monotonic", return_value=1e100):
            assert (
                await scheduler.coordinator.is_circuit_open(
                    same_pool.coordination_key
                )
                is True
            )
            decision = await scheduler.coordinator.try_acquire(
                same_pool,
                capacity_limit=same_pool.hard_in_flight,
                borrowed=False,
                use_global_gate=False,
            )
        assert decision.permit is None
        assert decision.circuit_open is True

    asyncio.run(run())


def test_rotated_key_uses_a_fresh_pool_fingerprint():
    coordinator = LocalPoolCoordinator(global_rpm=60000, global_burst=100)
    expired_pool = _pool("ys")
    rotated_pool = replace(expired_pool, api_key="rotated-test-credential")

    async def run():
        await coordinator.record_failure(
            expired_pool.coordination_key,
            threshold=1,
            cooldown_s=60,
            fatal=True,
        )
        blocked = await coordinator.try_acquire(
            expired_pool,
            capacity_limit=expired_pool.hard_in_flight,
            borrowed=False,
            use_global_gate=False,
        )
        recovered = await coordinator.try_acquire(
            rotated_pool,
            capacity_limit=rotated_pool.hard_in_flight,
            borrowed=False,
            use_global_gate=False,
        )
        assert blocked.permit is None
        assert blocked.circuit_open is True
        assert recovered.permit is not None
        await recovered.permit.release()

    assert expired_pool.coordination_key != rotated_pool.coordination_key
    asyncio.run(run())


def test_pool_cooldown_never_shortens_and_fatal_circuit_stays_permanent():
    async def run():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        coordinator = RedisPoolCoordinator(
            redis,
            namespace="test:ocr-cooldown",
            global_rpm=60,
            global_burst=1,
            operation_timeout_s=0.5,
        )
        pool = _pool("ys")
        circuit_key = f"{coordinator._pool_base(pool.coordination_key)}:circuit"

        await coordinator.record_failure(
            pool.coordination_key,
            threshold=1,
            cooldown_s=3600,
            fatal=False,
        )
        await coordinator.record_failure(
            pool.coordination_key,
            threshold=1,
            cooldown_s=1,
            fatal=False,
        )
        assert 3_599_000 <= await redis.pttl(circuit_key) <= 3_600_000

        await coordinator.record_failure(
            pool.coordination_key,
            threshold=1,
            cooldown_s=1,
            fatal=True,
        )
        await coordinator.record_failure(
            pool.coordination_key,
            threshold=1,
            cooldown_s=1,
            fatal=False,
        )
        assert await redis.get(circuit_key) == "fatal"
        assert await redis.pttl(circuit_key) == -1
        await redis.aclose()

    asyncio.run(run())


def test_scheduler_does_not_pre_reserve_unclaimed_batch_retries():
    async def transport(_pool, _payload, _request_id):
        return '{"ok":true}'

    scheduler = _scheduler(transport, share=0.0, max_batch_retries=50)
    scheduler.batch_deadline_s = 360.0
    scheduler.primary = replace(scheduler.primary, attempt_ttl_s=240.0)
    scheduler.accelerator = replace(scheduler.accelerator, attempt_ttl_s=70.0)
    scheduler.retry_pool = replace(
        scheduler.retry_pool,
        max_rpm=10,
        burst=2,
        attempt_ttl_s=95.0,
    )
    captured = []

    class Permit:
        async def release(self):
            return None

    async def acquire_first(_batch_hash, **kwargs):
        captured.append(kwargs["deadline_epoch"])
        return scheduler.primary, Permit(), False

    scheduler._acquire_first = acquire_first

    async def run():
        with patch("services.ocr_pool_scheduler.time.time", return_value=1000.0):
            await scheduler.request(
                {},
                batch_key="retry-span-budget",
                paper_key="paper-1",
                teacher_id="teacher-a",
                batch_paper_count=60,
            )

    asyncio.run(run())
    # Fifty VIP retries at 10 RPM take 288 seconds to begin, but those retries
    # do not exist yet.  The first attempt keeps its own complete 240-second
    # window instead of being rejected before the batch begins.
    assert captured[0] == pytest.approx(1120.0)


def test_redis_fatal_circuit_has_no_expiry_and_never_receives_the_raw_key():
    class FakeRedis:
        def __init__(self):
            self.calls = []

        async def eval(self, script, numkeys, *args):
            self.calls.append((script, numkeys, args))
            if numkeys == 10:
                return [1, 0, 0]
            return 1

    raw_key = "raw-test-credential-must-not-reach-redis"
    pool = replace(_pool("ys"), api_key=raw_key)
    redis = FakeRedis()
    coordinator = RedisPoolCoordinator(
        redis,
        namespace="test:ocr",
        global_rpm=60,
        global_burst=1,
        operation_timeout_s=0.5,
    )

    async def run():
        decision = await coordinator.try_acquire(
            pool,
            capacity_limit=pool.hard_in_flight,
            borrowed=False,
            use_global_gate=False,
        )
        assert decision.permit is not None
        await decision.permit.release()
        await coordinator.record_failure(
            pool.coordination_key,
            threshold=1,
            cooldown_s=60,
            fatal=True,
        )

    asyncio.run(run())

    failure_calls = [call for call in redis.calls if call[1] == 2]
    assert len(failure_calls) == 1
    script, numkeys, args = failure_calls[0]
    fatal_branch = script.split("end", 1)[0]
    assert numkeys == 2
    assert args[-3:] == (1, 1, 60)
    assert "redis.call('SET', KEYS[1], 'fatal')" in fatal_branch
    assert "'EX'" not in fatal_branch
    assert "'PX'" not in fatal_branch
    assert raw_key not in pool.coordination_key
    assert raw_key not in repr(redis.calls)


def test_local_failover_selection_expires_back_to_default_primary():
    coordinator = LocalPoolCoordinator(global_rpm=60, global_burst=1)

    async def run():
        with patch("services.ocr_pool_scheduler.time.monotonic", return_value=1000.0):
            await coordinator.set_active_primary("proxy")
        with patch("services.ocr_pool_scheduler.time.monotonic", return_value=1299.9):
            assert await coordinator.get_active_primary("ys") == "proxy"
        with patch("services.ocr_pool_scheduler.time.monotonic", return_value=1300.0):
            assert await coordinator.get_active_primary("ys") == "ys"

    asyncio.run(run())


def test_redis_failover_selection_uses_a_bounded_ttl():
    class FakeRedis:
        def __init__(self):
            self.calls = []

        async def set(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return True

    redis = FakeRedis()
    coordinator = RedisPoolCoordinator(
        redis,
        namespace="test:ocr",
        global_rpm=60,
        global_burst=1,
        operation_timeout_s=0.5,
    )

    asyncio.run(coordinator.set_active_primary("proxy"))

    assert redis.calls == [
        (("test:ocr:active-primary", "proxy"), {"ex": 300})
    ]


def test_redis_legacy_failover_selection_is_migrated_to_a_bounded_ttl():
    class FakeRedis:
        def __init__(self):
            self.expirations = []

        async def get(self, key):
            assert key == "test:ocr:active-primary"
            return "proxy"

        async def ttl(self, key):
            assert key == "test:ocr:active-primary"
            return -1

        async def expire(self, key, seconds):
            self.expirations.append((key, seconds))
            return True

    redis = FakeRedis()
    coordinator = RedisPoolCoordinator(
        redis,
        namespace="test:ocr",
        global_rpm=60,
        global_burst=1,
        operation_timeout_s=0.5,
    )

    selected = asyncio.run(coordinator.get_active_primary("ys"))

    assert selected == "proxy"
    assert redis.expirations == [("test:ocr:active-primary", 300)]


def test_priority_waiter_stops_new_borrowing_without_killing_existing_request():
    coordinator = LocalPoolCoordinator(global_rpm=60000, global_burst=100)
    accelerator = _pool("hk", hard=3, borrowed=2)
    pool_key = accelerator.coordination_key

    async def run():
        first = await coordinator.try_acquire(
            accelerator,
            capacity_limit=2,
            borrowed=True,
            use_global_gate=True,
        )
        assert first.permit is not None

        await coordinator.add_priority_waiter(pool_key, "interactive-1", 30)
        blocked = await coordinator.try_acquire(
            accelerator,
            capacity_limit=2,
            borrowed=True,
            use_global_gate=True,
        )
        assert blocked.permit is None

        # Reserved/high-priority capacity remains usable while the borrowed
        # request continues naturally; it is not cancelled or force-released.
        priority = await coordinator.try_acquire(
            accelerator,
            capacity_limit=3,
            borrowed=False,
            use_global_gate=False,
        )
        assert priority.permit is not None
        assert coordinator._active[pool_key] == 2

        await priority.permit.release()
        await coordinator.remove_priority_waiter(pool_key, "interactive-1")
        resumed = await coordinator.try_acquire(
            accelerator,
            capacity_limit=2,
            borrowed=True,
            use_global_gate=True,
        )
        assert resumed.permit is not None
        await resumed.permit.release()
        await first.permit.release()

    asyncio.run(run())


def test_retry_pool_is_used_at_most_once_for_one_paper():
    calls = []

    async def transport(pool, _payload, _request_id):
        calls.append(pool.name)
        if pool.name == "vip":
            return '{"ok":true}'
        raise OcrPoolError(
            "upstream_failed",
            retryable=True,
            transport_failure=True,
        )

    scheduler = _scheduler(transport, share=0.0)

    async def run():
        first = await scheduler.request(
            {},
            batch_key="batch-retry",
            paper_key="same-paper",
            teacher_id="teacher-a",
        )
        assert first == '{"ok":true}'
        with pytest.raises(OcrPoolError, match="batch_retry_limit_reached"):
            await scheduler.request(
                {},
                batch_key="batch-retry",
                paper_key="same-paper",
                teacher_id="teacher-a",
            )

    asyncio.run(run())

    assert calls.count("vip") == 1
    assert scheduler.stats()["retry_started"] == 1
    assert scheduler.stats()["retry_rejected"] == 1


def test_retry_ledger_is_not_refreshed_by_a_new_deadline_generation():
    calls = []

    async def transport(pool, _payload, _request_id):
        calls.append(pool.name)
        if pool.name == "vip":
            return '{"ok":true}'
        raise OcrPoolError(
            "upstream_failed",
            retryable=True,
            transport_failure=True,
        )

    scheduler = _scheduler(transport, share=0.0, circuit_threshold=100)

    async def run():
        assert await scheduler.request(
            {},
            batch_key="batch:phase:recognition-resume:1",
            paper_key="batch:phase:recognition-resume:1:paper-1",
            retry_batch_key="batch:phase:recognition-resume",
            retry_paper_key="batch:phase:recognition-resume:paper-1",
            teacher_id="teacher-a",
        ) == '{"ok":true}'

        with pytest.raises(OcrPoolError, match="batch_retry_limit_reached"):
            await scheduler.request(
                {},
                batch_key="batch:phase:recognition-resume:2",
                paper_key="batch:phase:recognition-resume:2:paper-1",
                retry_batch_key="batch:phase:recognition-resume",
                retry_paper_key="batch:phase:recognition-resume:paper-1",
                teacher_id="teacher-a",
            )

    asyncio.run(run())

    assert calls.count("ys") == 2
    assert calls.count("vip") == 1
    assert scheduler.stats()["retry_started"] == 1
    assert scheduler.stats()["retry_rejected"] == 1
    assert scheduler.coordinator._active.get(
        scheduler.retry_pool.coordination_key, 0
    ) == 0


def test_retry_dispatch_failure_does_not_consume_retry_ledger():
    async def transport(_pool, _payload, _request_id):
        raise OcrPoolError(
            "upstream_failed",
            retryable=True,
            transport_failure=True,
        )

    scheduler = _scheduler(transport, share=0.0, circuit_threshold=100)

    async def fail_retry_acquire(*, deadline_epoch):
        assert deadline_epoch > __import__("time").time()
        raise OcrPoolError(
            "pool_dispatch_deadline:vip",
            retryable=False,
            transport_failure=False,
        )

    scheduler._acquire_retry = fail_retry_acquire

    async def run():
        with pytest.raises(OcrPoolError, match="pool_dispatch_deadline:vip"):
            await scheduler.request(
                {},
                batch_key="retry-capacity-busy",
                paper_key="paper-1",
                teacher_id="teacher-a",
            )

    asyncio.run(run())

    assert scheduler.coordinator._batch_retries == {}
    assert scheduler.coordinator._paper_retries == set()
    assert scheduler.stats()["retry_started"] == 0


def test_excess_concurrent_retries_are_rejected_before_retry_pool_acquire():
    calls = []

    async def transport(pool, _payload, _request_id):
        calls.append(pool.name)
        if pool.name == "vip":
            return '{"ok":true}'
        raise OcrPoolError(
            "upstream_failed",
            retryable=True,
            transport_failure=True,
        )

    scheduler = _scheduler(
        transport,
        share=0.0,
        max_batch_retries=3,
        circuit_threshold=100,
    )
    acquire_calls = 0
    original_acquire_retry = scheduler._acquire_retry

    async def counted_acquire_retry(*, deadline_epoch):
        nonlocal acquire_calls
        acquire_calls += 1
        return await original_acquire_retry(deadline_epoch=deadline_epoch)

    scheduler._acquire_retry = counted_acquire_retry

    async def run():
        return await asyncio.gather(*(
            scheduler.request(
                {},
                batch_key="concurrent-retry-cap",
                paper_key=f"paper-{index}",
                teacher_id="teacher-a",
            )
            for index in range(20)
        ), return_exceptions=True)

    results = asyncio.run(run())

    assert results.count('{"ok":true}') == 3
    assert sum(
        isinstance(result, OcrPoolError)
        and result.code == "batch_retry_limit_reached"
        for result in results
    ) == 17
    assert acquire_calls == 3
    assert calls.count("vip") == 3
    assert scheduler.stats()["retry_started"] == 3
    assert scheduler.stats()["retry_rejected"] == 17


def test_redis_unused_retry_claim_can_be_released_and_reclaimed():
    async def run():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        coordinator = RedisPoolCoordinator(
            redis,
            namespace="test:ocr-retry-release",
            global_rpm=60,
            global_burst=1,
            operation_timeout_s=0.5,
        )

        assert await coordinator.claim_retry("batch", "paper", 1, 60) is True
        assert await coordinator.release_retry_claim("batch", "paper") is True
        assert await coordinator.release_retry_claim("batch", "paper") is False
        assert await coordinator.claim_retry("batch", "paper", 1, 60) is True
        await redis.aclose()

    asyncio.run(run())


def test_template_fallback_started_marker_waits_for_admission(monkeypatch):
    """Qwen diagnostics mark a request only after TPM and permit admission."""
    calls = []

    async def transport(pool, _payload, _request_id):
        calls.append(pool.name)
        return '{"ok":true}'

    async def run():
        scheduler = _scheduler(transport, share=0.5)
        scheduler.template_fallback_pool = _pool("qwen-fallback")
        diagnostics = {}
        admission_entered = asyncio.Event()
        release_admission = asyncio.Event()

        async def blocked_rate_budget(_payload):
            admission_entered.set()
            await release_admission.wait()

        monkeypatch.setattr(
            scheduler,
            "_acquire_template_fallback_rate_budget",
            blocked_rate_budget,
        )
        request = asyncio.create_task(
            scheduler.request_template_fallback_pool(
                {},
                request_key="fallback-admission-marker",
                route_diagnostics=diagnostics,
            )
        )
        await admission_entered.wait()
        assert diagnostics.get("_fallback_request_started") is None
        release_admission.set()
        assert await request == '{"ok":true}'
        assert diagnostics["_fallback_request_started"] is True

    asyncio.run(run())
    assert calls == ["qwen-fallback"]


def test_template_fallback_admission_failure_clears_pending_without_start_marker(
    monkeypatch,
):
    """A rejected Qwen admission is retryable and was not a provider call."""

    async def transport(_pool, _payload, _request_id):
        raise AssertionError("transport must not run when admission fails")

    async def run():
        scheduler = _scheduler(transport, share=0.5)
        scheduler.template_fallback_pool = _pool("qwen-fallback")
        diagnostics = {}

        async def reject_admission(_payload):
            raise OcrPoolError(
                "qwen_tpm_admission",
                retryable=False,
                transport_failure=False,
                request_sent=False,
                failure_category="admission",
            )

        monkeypatch.setattr(
            scheduler,
            "_acquire_template_fallback_rate_budget",
            reject_admission,
        )
        with pytest.raises(OcrPoolError, match="qwen_tpm_admission"):
            await scheduler.request_template_fallback_pool(
                {},
                request_key="fallback-admission-rejected",
                route_diagnostics=diagnostics,
            )

        assert diagnostics["fallback_attempted"] is True
        assert diagnostics["_fallback_admission_pending"] is False
        assert diagnostics.get("_fallback_request_started") is None

    asyncio.run(run())


def test_batch_allows_only_three_automatic_cross_pool_retries():
    calls = []

    async def transport(pool, _payload, _request_id):
        calls.append(pool.name)
        if pool.name == "vip":
            return '{"ok":true}'
        raise OcrPoolError(
            "upstream_failed",
            retryable=True,
            transport_failure=True,
        )

    scheduler = _scheduler(
        transport,
        share=0.0,
        max_batch_retries=3,
        circuit_threshold=100,
    )

    async def run():
        for index in range(3):
            assert await scheduler.request(
                {},
                batch_key="batch-three",
                paper_key=f"paper-{index}",
                teacher_id="teacher-a",
            ) == '{"ok":true}'
        with pytest.raises(OcrPoolError, match="batch_retry_limit_reached"):
            await scheduler.request(
                {},
                batch_key="batch-three",
                paper_key="paper-4",
                teacher_id="teacher-a",
            )

    asyncio.run(run())

    assert calls.count("vip") == 3


@pytest.mark.parametrize("branch", ["locator", "solver"])
def test_max_refusal_is_not_retried_or_counted_as_pool_failure(branch):
    from services import template_max_owned as contract

    calls = []

    async def transport(pool, _payload, _request_id):
        calls.append(pool.name)
        return json.dumps({"refusal": "cannot continue"})

    receiver = (contract.receive_locator_candidates if branch == "locator"
                else contract.receive_solver_candidates)
    manifest = [{"q_id": "Q1", "slots": [{"slot_id": "1[1]", "cue": "blank"}]}]
    scheduler = _scheduler(transport, share=0.0)
    with pytest.raises(contract.MaxTemplateRejection, match="阻断"):
        asyncio.run(scheduler.request(
            {}, batch_key="max-rejection", paper_key="paper-rejection", teacher_id="teacher-a",
            validator=lambda content: receiver(json.loads(content), manifest)))

    assert calls == ["ys"]
    assert scheduler.coordinator._consecutive_failures.get(scheduler.primary.coordination_key, 0) == 0


@pytest.mark.parametrize("branch", ["locator", "solver"])
@pytest.mark.parametrize("status", ["extra", "refused"])
def test_explicit_max_report_is_a_valid_response_not_a_retry(branch, status):
    from services import template_max_owned as contract

    calls = []
    payload = ({"coordinate_space": "normalized_1000_v1", "needs_review": [],
                "questions": [{"question_number": "1", "status": status, "reason": "directory rejected"}]}
               if branch == "locator" else
               {"answers": [{"q_id": "Q1", "status": status, "reason": "directory rejected"}]})

    async def transport(pool, _payload, _request_id):
        calls.append(pool.name)
        return json.dumps(payload)

    receiver = (contract.receive_locator_candidates if branch == "locator"
                else contract.receive_solver_candidates)
    manifest = [{"q_id": "Q1", "slots": [{"slot_id": "1[1]", "cue": "blank"}]}]
    scheduler = _scheduler(transport, share=0.0)
    content = asyncio.run(scheduler.request(
        {}, batch_key="max-report", paper_key="paper-report", teacher_id="teacher-a",
        validator=lambda content: receiver(json.loads(content), manifest)))

    received = receiver(json.loads(content), manifest)
    assert received["max_flags"] == {"1": [{"scope": "question", "status": status,
                                            "reason": "directory rejected", "source": branch}]}
    assert calls == ["ys"]
    assert scheduler.coordinator._consecutive_failures.get(scheduler.primary.coordination_key, 0) == 0


def test_contract_failure_retries_once_and_counts_toward_pool_circuit():
    calls = []

    async def transport(pool, _payload, _request_id):
        calls.append(pool.name)
        if pool.name == "vip":
            return '{"valid":true}'
        return '{"valid":false}'

    def validator(content):
        if json.loads(content).get("valid") is not True:
            raise ValueError("contract")

    scheduler = _scheduler(transport, share=0.0)
    result = asyncio.run(
        scheduler.request(
            {},
            batch_key="batch-contract",
            paper_key="paper-contract",
            teacher_id="teacher-a",
            validator=validator,
        )
    )

    assert result == '{"valid":true}'
    assert calls == ["ys", "vip"]
    pool_key = scheduler.primary.coordination_key
    assert scheduler.coordinator._consecutive_failures.get(pool_key, 0) == 1
    assert asyncio.run(scheduler.coordinator.is_circuit_open(pool_key)) is False


def test_primary_soft_limit_can_borrow_hk_and_then_use_primary_hard_window():
    async def transport(pool, _payload, _request_id):
        return pool.name

    scheduler = _scheduler(transport, share=0.0, primary_soft=1)

    async def run():
        phase = scheduler._batch_hash("phase")
        await scheduler.coordinator.finish_accelerator_probe(phase, False)
        held = await scheduler.coordinator.try_acquire(
            scheduler.primary,
            capacity_limit=1,
            borrowed=False,
            use_global_gate=True,
        )
        assert held.permit is not None
        pool, permit, _ = await scheduler._acquire_first(
            phase,
            route_key="paper-1",
            paper_hash="1" * 24,
            teacher_hash=scheduler._teacher_hash("teacher-a"),
            deadline_epoch=__import__("time").time() + 1,
        )
        assert pool.name == "ys"
        assert scheduler.coordinator._active[scheduler.primary.coordination_key] == 2
        await permit.release()
        await held.permit.release()

    asyncio.run(run())


def test_429_opens_pool_circuit_immediately():
    async def transport(_pool, _payload, _request_id):
        raise OcrPoolError(
            "http_429",
            retryable=True,
            transport_failure=True,
            status_code=429,
        )

    scheduler = _scheduler(transport, share=0.0, circuit_threshold=3)

    async def run():
        with pytest.raises(OcrPoolError, match="http_429"):
            await scheduler._execute(scheduler.primary, {}, None)
        decision = await scheduler.coordinator.try_acquire(
            scheduler.primary,
            capacity_limit=scheduler.primary.hard_in_flight,
            borrowed=False,
            use_global_gate=False,
        )
        assert decision.permit is None
        assert decision.circuit_open is True

    asyncio.run(run())


def test_non_batch_route_is_interactive_and_uses_reserved_hk_capacity(tmp_path):
    from services.ocr_service import _multi_pool_route_keys

    path = tmp_path / "template_preprocessed.jpg"
    path.write_bytes(b"image")

    batch_key, paper_key, priority = _multi_pool_route_keys(str(path))

    assert batch_key.startswith("interactive:")
    assert paper_key.endswith(":template")
    assert priority == "interactive"


def _configure_template_scheduler_for_test(monkeypatch, *, fallback=True):
    """Install the qwen3.8-max Token Plan route; leave retired GPT/YS creds set."""
    import config

    monkeypatch.setattr(config, "OCR_MULTI_POOL_COORDINATOR", "local")
    # Production .env still carries retired GPT/YS credentials; they must be ignored.
    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_BASE_URL", "https://gpt.example.invalid/v1", raising=False)
    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_API_KEY", "canonical-key", raising=False)
    monkeypatch.setattr(config, "OCR_RETRY_BASE_URL", "https://ys.example.invalid/v1", raising=False)
    monkeypatch.setattr(config, "OCR_RETRY_API_KEY", "ys-key", raising=False)
    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_FALLBACK_BASE_URL", (
        "https://qwen.example.invalid/v1" if fallback else ""
    ))
    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_FALLBACK_API_KEY", "fallback-key" if fallback else "")
    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_FALLBACK_MODEL", "qwen3.8max" if fallback else "")
    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_FALLBACK_HARD_IN_FLIGHT", 4)
    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_FALLBACK_FIRST_EVENT_TIMEOUT_S", 1.0)
    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_FALLBACK_ATTEMPT_TTL_S", 2.0)
    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_FALLBACK_ENABLED", fallback)


def test_template_scheduler_builds_only_the_max_token_plan_route(monkeypatch):
    _configure_template_scheduler_for_test(monkeypatch)

    scheduler = OcrPoolScheduler.from_template_config(pool_route="fallback")

    pool = scheduler.template_fallback_pool
    assert scheduler.primary is pool
    assert (pool.name, pool.model, pool.api_protocol) == (
        "template-gpt-fallback", "qwen3.8max", "chat_completions",
    )
    # Token Plan: tier concurrency is the only provider limit; no RPM pacing.
    assert pool.hard_in_flight == scheduler.first_attempt_hard_in_flight == 4
    assert pool.max_rpm == scheduler.global_first_attempt_rpm == 0.0
    assert scheduler.failover_pool is None


@pytest.mark.parametrize("route", ["auto", "canonical", "ys", "retry"])
def test_template_scheduler_retired_routes_fail_closed(monkeypatch, route):
    _configure_template_scheduler_for_test(monkeypatch)

    with pytest.raises(ValueError, match="is retired"):
        OcrPoolScheduler.from_template_config(pool_route=route)


def test_template_scheduler_ignores_partially_configured_fallback(monkeypatch):
    _configure_template_scheduler_for_test(monkeypatch)
    import config

    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_FALLBACK_API_KEY", "")
    # Simulate a stale enablement flag from a previous config reload.
    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_FALLBACK_ENABLED", True)

    with pytest.raises(RuntimeError, match="qwen3.8max fallback is not configured"):
        OcrPoolScheduler.from_template_config(pool_route="fallback")


def test_template_scheduler_fallback_request_uses_qwen_pool_directly(monkeypatch):
    _configure_template_scheduler_for_test(monkeypatch)
    calls = []

    async def transport(pool, _payload, _request_id):
        calls.append((pool.name, pool.model))
        return '{"ok":true}'

    scheduler = OcrPoolScheduler.from_template_config(pool_route="fallback")
    scheduler._transport = transport

    async def run():
        return await scheduler.request_template_fallback_pool(
            {}, request_key="template-fallback-page-1"
        )

    assert asyncio.run(run()) == '{"ok":true}'
    assert calls == [("template-gpt-fallback", "qwen3.8max")]


def test_scheduler_readiness_does_not_instantiate_retired_student_pools(monkeypatch):
    import config
    import services.ocr_pool_scheduler as scheduler_module

    monkeypatch.setattr(config, "OCR_MULTI_POOL_ENABLED", True)
    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_BASE_URL", "", raising=False)
    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_API_KEY", "", raising=False)
    for name in (
        "OCR_TEMPLATE_GPT_FALLBACK_BASE_URL", "OCR_TEMPLATE_GPT_FALLBACK_API_KEY", "OCR_TEMPLATE_GPT_FALLBACK_MODEL",
        "OCR_TEMPLATE_YS_BASE_URL", "OCR_TEMPLATE_YS_API_KEY",
        "OCR_RETRY_BASE_URL", "OCR_RETRY_API_KEY",
        "OCR_TEMPLATE_EXEMPTION_BASE_URL", "OCR_TEMPLATE_EXEMPTION_API_KEY",
    ):
        monkeypatch.setattr(config, name, "", raising=False)
    # The retired graph has no entry point left; readiness cannot resurrect it.
    assert not hasattr(scheduler_module, "get_ocr_pool_scheduler")
    assert not hasattr(scheduler_module.OcrPoolScheduler, "from_config")
    monkeypatch.setattr(
        scheduler_module,
        "get_template_ocr_pool_scheduler",
        lambda: pytest.fail("unconfigured template route must not be loaded"),
    )

    asyncio.run(scheduler_module.ensure_ocr_pool_scheduler_ready())


def test_scheduler_readiness_ignores_retired_ys_and_gpt_credentials(monkeypatch):
    import config
    import services.ocr_pool_scheduler as scheduler_module

    requested_routes = []

    # Production .env still carries retired YS/GPT credentials; startup must
    # neither probe nor initialize those routes when Max is not configured.
    monkeypatch.setattr(config, "OCR_TEMPLATE_YS_BASE_URL", "https://ys.example.invalid/v1", raising=False)
    monkeypatch.setattr(config, "OCR_TEMPLATE_YS_API_KEY", "ys-key", raising=False)
    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_BASE_URL", "https://gpt.example.invalid/v1", raising=False)
    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_API_KEY", "canonical-key", raising=False)
    for name in (
        "OCR_TEMPLATE_GPT_FALLBACK_BASE_URL", "OCR_TEMPLATE_GPT_FALLBACK_API_KEY", "OCR_TEMPLATE_GPT_FALLBACK_MODEL",
    ):
        monkeypatch.setattr(config, name, "", raising=False)
    monkeypatch.setattr(
        scheduler_module,
        "get_template_ocr_pool_scheduler",
        lambda *, pool_route=None: requested_routes.append(pool_route),
    )

    asyncio.run(scheduler_module.ensure_ocr_pool_scheduler_ready())

    assert requested_routes == []


def test_scheduler_readiness_initializes_qwen_route_when_ys_is_absent(
    monkeypatch,
):
    import config
    import services.ocr_pool_scheduler as scheduler_module

    requested_routes = []

    class _Scheduler:
        coordinator = object()

    for name in (
        "OCR_TEMPLATE_YS_BASE_URL",
        "OCR_TEMPLATE_YS_API_KEY",
        "OCR_RETRY_BASE_URL",
        "OCR_RETRY_API_KEY",
        "OCR_TEMPLATE_EXEMPTION_BASE_URL",
        "OCR_TEMPLATE_EXEMPTION_API_KEY",
        "OCR_TEMPLATE_GPT_BASE_URL",
        "OCR_TEMPLATE_GPT_API_KEY",
    ):
        monkeypatch.setattr(config, name, "", raising=False)
    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_FALLBACK_BASE_URL", "https://qwen.example.invalid/v1")
    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_FALLBACK_API_KEY", "qwen-key")
    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_FALLBACK_MODEL", "qwen3.8max")
    monkeypatch.setattr(
        scheduler_module,
        "get_template_ocr_pool_scheduler",
        lambda *, pool_route=None: requested_routes.append(pool_route) or _Scheduler(),
    )

    asyncio.run(scheduler_module.ensure_ocr_pool_scheduler_ready())

    assert requested_routes == ["fallback"]


def test_scheduler_readiness_keeps_zero_argument_getter_compatibility(monkeypatch):
    import config
    import services.ocr_pool_scheduler as scheduler_module

    calls = []

    class _Scheduler:
        coordinator = object()

    monkeypatch.setattr(config, "OCR_TEMPLATE_YS_BASE_URL", "", raising=False)
    monkeypatch.setattr(config, "OCR_TEMPLATE_YS_API_KEY", "", raising=False)
    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_BASE_URL", "", raising=False)
    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_API_KEY", "", raising=False)
    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_FALLBACK_BASE_URL", "https://qwen.example.invalid/v1")
    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_FALLBACK_API_KEY", "qwen-key")
    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_FALLBACK_MODEL", "qwen3.8max")
    monkeypatch.setattr(
        scheduler_module,
        "get_template_ocr_pool_scheduler",
        lambda: calls.append("called") or _Scheduler(),
    )

    asyncio.run(scheduler_module.ensure_ocr_pool_scheduler_ready())

    assert calls == ["called"]


def test_answer_sheet_student_evidence_uses_batch_pool_routing(tmp_path):
    from services.ocr_service import _multi_pool_route_keys

    evidence_dir = (
        tmp_path
        / "answer_sheet_0123456789abcdef"
        / "student_upload_abcdef0123456789"
        / ("evidence-set-" + "a" * 32)
    )
    evidence_dir.mkdir(parents=True)
    path = evidence_dir / "evidence_01.png"
    path.write_bytes(b"image")

    batch_key, paper_key, priority = _multi_pool_route_keys(str(path))

    assert batch_key == "answer_sheet_0123456789abcdef:student-recognition"
    assert paper_key.endswith(
        ":student_upload_abcdef0123456789:"
        + "evidence-set-"
        + "a" * 32
        + ":evidence_01.png"
    )
    assert priority == "batch"


def test_answer_sheet_template_image_remains_interactive(tmp_path):
    from services.ocr_service import _multi_pool_route_keys

    session_dir = tmp_path / "answer_sheet_0123456789abcdef"
    session_dir.mkdir()
    path = session_dir / "layout-template_preprocessed.jpg"
    path.write_bytes(b"image")

    batch_key, _paper_key, priority = _multi_pool_route_keys(str(path))

    assert batch_key.startswith("interactive:")
    assert priority == "interactive"


def test_second_resume_generation_gets_a_fresh_phase_budget(tmp_path):
    from services.ocr_service import (
        _multi_pool_route_keys,
        reset_vision_phase_key,
        set_vision_phase_key,
    )

    batch_dir = tmp_path / "quick_batch_phase-budget"
    batch_dir.mkdir()
    path = batch_dir / "paper_preprocessed.jpg"
    path.write_bytes(b"image")

    first_token = set_vision_phase_key(
        "recognition-resume:1",
        retry_scope_key="recognition-resume",
    )
    try:
        first_batch, first_paper, priority = _multi_pool_route_keys(str(path))
    finally:
        reset_vision_phase_key(first_token)
    second_token = set_vision_phase_key(
        "recognition-resume:2",
        retry_scope_key="recognition-resume",
    )
    try:
        second_batch, second_paper, _ = _multi_pool_route_keys(str(path))
    finally:
        reset_vision_phase_key(second_token)

    assert priority == "batch"
    assert first_batch != second_batch
    assert first_paper != second_paper

    calls = []

    async def transport(pool, _payload, _request_id):
        calls.append(pool.name)
        return '{"ok":true}'

    scheduler = _scheduler(transport, share=0.0)
    scheduler.batch_deadline_s = 3.0

    async def run():
        with patch("services.ocr_pool_scheduler.time.time", return_value=1000.0):
            await scheduler.request(
                {},
                batch_key=first_batch,
                paper_key=first_paper,
                teacher_id="teacher-a",
            )
        # This generation still has time for a complete first attempt. A later
        # persisted generation independently gets a fresh first-attempt budget.
        with patch("services.ocr_pool_scheduler.time.time", return_value=1001.0):
            await scheduler.request(
                {},
                batch_key=first_batch,
                paper_key=f"{first_paper}:late",
                teacher_id="teacher-a",
            )
            await scheduler.request(
                {},
                batch_key=second_batch,
                paper_key=second_paper,
                teacher_id="teacher-a",
            )

    asyncio.run(run())
    assert calls == ["ys", "ys", "ys"]


def test_429_retry_after_extends_pool_cooldown_without_changing_attempt_limit():
    captured = {}

    async def transport(_pool, _payload, _request_id):
        raise OcrPoolError(
            "http_429",
            retryable=True,
            transport_failure=True,
            status_code=429,
            retry_after_s=3600.0,
        )

    scheduler = _scheduler(transport, share=0.0, circuit_threshold=3)

    async def record_failure(pool_name, **kwargs):
        captured["pool_name"] = pool_name
        captured.update(kwargs)

    scheduler.coordinator.record_failure = record_failure

    async def run():
        with pytest.raises(OcrPoolError, match="http_429"):
            await scheduler._execute(scheduler.primary, {}, None)

    asyncio.run(run())
    assert captured == {
        "pool_name": scheduler.primary.coordination_key,
        "threshold": 1,
        "cooldown_s": 3600.0,
        "fatal": False,
    }


def test_cancelled_hk_health_probe_closes_batch_gate_and_releases_capacity():
    started = asyncio.Event()

    async def transport(_pool, _payload, _request_id):
        started.set()
        await asyncio.Event().wait()

    scheduler = _scheduler(transport, share=0.2)
    selected_key = next(
        key
        for key in (f"cancel-probe-paper-{index}" for index in range(100))
        if scheduler._prefer_accelerator(
            key,
            __import__("hashlib").sha256(key.encode("utf-8")).hexdigest()[:24],
        )
    )

    async def run():
        task = asyncio.create_task(
            scheduler.request(
                {},
                batch_key="cancelled-probe-batch",
                paper_key=selected_key,
                teacher_id="teacher-a",
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert list(scheduler.coordinator._accelerator_gate.values()) == ["blocked"]
        pool_key = scheduler.accelerator.coordination_key
        assert scheduler.coordinator._active.get(pool_key, 0) == 0
        assert scheduler.coordinator._borrowed.get(pool_key, 0) == 0

    asyncio.run(run())


def test_permit_release_retries_transient_coordinator_failure():
    class FlakyCoordinator:
        def __init__(self):
            self.calls = 0

        async def release(self, _permit):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("redis unavailable")

    from services.ocr_pool_scheduler import PoolPermit

    coordinator = FlakyCoordinator()
    permit = PoolPermit(
        coordinator,
        pool_name="hk",
        lease_id="lease-1",
        borrowed=True,
    )

    async def run():
        await permit.release()
        assert permit._released is True

        await permit.release()
        assert coordinator.calls == 2

    asyncio.run(run())


def test_hk_allows_twenty_borrowed_plus_five_reserved_priority_permits():
    coordinator = LocalPoolCoordinator(global_rpm=60000, global_burst=100)
    accelerator = _pool("hk", hard=25, borrowed=20)
    pool_key = accelerator.coordination_key

    async def run():
        priority = []
        for _ in range(5):
            decision = await coordinator.try_acquire(
                accelerator,
                capacity_limit=25,
                borrowed=False,
                use_global_gate=False,
            )
            assert decision.permit is not None
            priority.append(decision.permit)

        borrowed = []
        for _ in range(20):
            decision = await coordinator.try_acquire(
                accelerator,
                capacity_limit=25,
                borrowed=True,
                use_global_gate=True,
            )
            assert decision.permit is not None
            borrowed.append(decision.permit)

        assert coordinator._active[pool_key] == 25
        assert coordinator._borrowed[pool_key] == 20

        blocked = await coordinator.try_acquire(
            accelerator,
            capacity_limit=25,
            borrowed=True,
            use_global_gate=True,
        )
        assert blocked.permit is None

        for active_permit in borrowed + priority:
            await active_permit.release()

    asyncio.run(run())


def test_hk_probe_does_not_wait_when_priority_work_blocks_borrowing():
    calls = []

    async def transport(pool, _payload, _request_id):
        calls.append(pool.name)
        return pool.name

    scheduler = _scheduler(transport, share=0.2)
    selected_key = next(
        key
        for key in (f"probe-paper-{index}" for index in range(100))
        if scheduler._prefer_accelerator(
            key,
            __import__("hashlib").sha256(key.encode("utf-8")).hexdigest()[:24],
        )
    )

    async def run():
        await scheduler.coordinator.add_priority_waiter(
            scheduler.accelerator.coordination_key, "interactive", 30
        )
        result = await asyncio.wait_for(
            scheduler.request(
                {},
                batch_key="probe-priority-batch",
                paper_key=selected_key,
                teacher_id="teacher-a",
            ),
            timeout=0.2,
        )
        assert result == "ys"
        assert calls == ["ys"]
        assert list(scheduler.coordinator._accelerator_gate.values()) == []

    asyncio.run(run())


def test_permit_granted_after_dispatch_deadline_is_released():
    class LateCoordinator:
        def __init__(self):
            self.release_calls = 0

        async def try_acquire(self, pool, **_kwargs):
            await asyncio.sleep(0.02)
            from services.ocr_pool_scheduler import AcquireDecision, PoolPermit

            return AcquireDecision(
                permit=PoolPermit(
                    self,
                    pool_name=pool.name,
                    lease_id="late-lease",
                    borrowed=False,
                )
            )

        async def release(self, _permit):
            self.release_calls += 1

    coordinator = LateCoordinator()
    scheduler = _scheduler(lambda *_args: None)
    scheduler.coordinator = coordinator

    async def run():
        with pytest.raises(OcrPoolError, match="pool_dispatch_deadline:vip"):
            await scheduler._wait_for_pool(
                scheduler.retry_pool,
                capacity_limit=scheduler.retry_pool.hard_in_flight,
                borrowed=False,
                use_global_gate=False,
                deadline_epoch=__import__("time").time() + 0.005,
            )
        assert coordinator.release_calls == 1

    asyncio.run(run())


def test_late_teacher_uses_proxy_when_hk_is_full_without_exceeding_global_limit():
    coordinator = LocalPoolCoordinator(global_rpm=60000, global_burst=100)
    hk = _pool("hk", hard=30)
    proxy = _pool("proxy", hard=19)

    async def acquire(pool, teacher, waiter):
        return await coordinator.try_acquire(
            pool,
            capacity_limit=pool.hard_in_flight,
            borrowed=False,
            use_global_gate=True,
            teacher_hash=teacher,
            global_in_flight_limit=49,
            global_teacher_limit=30,
            waiter_id=waiter,
        )

    async def run():
        hk_permits = []
        for index in range(30):
            decision = await acquire(hk, "teacher-a", f"a-{index}")
            assert decision.permit is not None
            hk_permits.append(decision.permit)

        proxy_permits = []
        for index in range(19):
            decision = await acquire(proxy, "teacher-b", f"b-{index}")
            assert decision.permit is not None
            proxy_permits.append(decision.permit)

        assert coordinator._global_first_active == 49
        assert coordinator._global_first_teacher_active == {
            "teacher-a": 30,
            "teacher-b": 19,
        }

        waiting = await acquire(hk, "teacher-c", "c-0")
        assert waiting.permit is None

        await hk_permits.pop().release()
        resumed = await acquire(hk, "teacher-c", "c-0")
        assert resumed.permit is not None
        assert coordinator._global_first_active == 49

        await resumed.permit.release()
        for permit in hk_permits + proxy_permits:
            await permit.release()

    asyncio.run(run())


def test_two_late_teachers_share_proxy_capacity_when_hk_is_already_full():
    coordinator = LocalPoolCoordinator(global_rpm=60000, global_burst=100)
    hk = _pool("hk", hard=30)
    proxy = _pool("proxy", hard=19)

    async def acquire(pool, teacher, waiter):
        return await coordinator.try_acquire(
            pool,
            capacity_limit=pool.hard_in_flight,
            borrowed=False,
            use_global_gate=True,
            teacher_hash=teacher,
            global_in_flight_limit=49,
            global_teacher_limit=30,
            waiter_id=waiter,
        )

    async def run():
        hk_permits = []
        for index in range(30):
            decision = await acquire(hk, "teacher-a", f"a-{index}")
            assert decision.permit is not None
            hk_permits.append(decision.permit)

        # Both late teachers already have queued work while HK is full. Their
        # waiters remain registered until one of their own requests is admitted.
        for index in range(10):
            assert (await acquire(hk, "teacher-b", f"b-{index}")).permit is None
            assert (await acquire(hk, "teacher-c", f"c-{index}")).permit is None

        proxy_permits = []
        for index in range(10):
            b = await acquire(proxy, "teacher-b", f"b-{index}")
            if b.permit is not None:
                proxy_permits.append(("teacher-b", b.permit))
            c = await acquire(proxy, "teacher-c", f"c-{index}")
            if c.permit is not None:
                proxy_permits.append(("teacher-c", c.permit))

        teacher_b_permits = sum(
            1 for teacher, _ in proxy_permits if teacher == "teacher-b"
        )
        teacher_c_permits = sum(
            1 for teacher, _ in proxy_permits if teacher == "teacher-c"
        )
        assert teacher_b_permits + teacher_c_permits == 19
        assert abs(teacher_b_permits - teacher_c_permits) <= 1
        assert coordinator._global_first_active == 49
        assert coordinator._global_first_teacher_active == {
            "teacher-a": 30,
            "teacher-b": teacher_b_permits,
            "teacher-c": teacher_c_permits,
        }

        for index in range(10):
            await coordinator.remove_first_attempt_waiter("teacher-b", f"b-{index}")
            await coordinator.remove_first_attempt_waiter("teacher-c", f"c-{index}")
        for permit in hk_permits:
            await permit.release()
        for _, permit in proxy_permits:
            await permit.release()

    asyncio.run(run())


def test_redis_group_gate_gives_a_late_peer_the_next_proxy_slot():
    hk = _pool("hk", hard=30)
    proxy = _pool("proxy", hard=19)

    async def run():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        coordinator = RedisPoolCoordinator(
            redis,
            namespace="test:ocr-first-attempt-group",
            global_rpm=60000,
            global_burst=100,
            operation_timeout_s=0.5,
        )

        async def acquire(pool, teacher, waiter):
            return await coordinator.try_acquire(
                pool,
                capacity_limit=pool.hard_in_flight,
                borrowed=False,
                use_global_gate=True,
                teacher_hash=teacher,
                global_in_flight_limit=49,
                global_teacher_limit=30,
                waiter_id=waiter,
            )

        hk_permits = []
        for index in range(30):
            decision = await acquire(hk, "teacher-a", f"a-{index}")
            assert decision.permit is not None
            hk_permits.append(decision.permit)

        # Register B and C while HK is full. B may take one proxy slot, but it
        # cannot take the next one while C is still the least-served waiter.
        assert (await acquire(hk, "teacher-b", "b-1")).permit is None
        assert (await acquire(hk, "teacher-c", "c-1")).permit is None
        first_b = await acquire(proxy, "teacher-b", "b-1")
        assert first_b.permit is not None
        blocked_b = await acquire(proxy, "teacher-b", "b-2")
        assert blocked_b.permit is None
        first_c = await acquire(proxy, "teacher-c", "c-1")
        assert first_c.permit is not None

        group_base = coordinator._first_attempt_base()
        assert await redis.zcard(f"{group_base}:leases") == 32
        assert await redis.zcard(f"{group_base}:teacher:teacher-a") == 30
        assert await redis.zcard(f"{group_base}:teacher:teacher-b") == 1
        assert await redis.zcard(f"{group_base}:teacher:teacher-c") == 1

        await coordinator.remove_first_attempt_waiter("teacher-b", "b-2")
        for permit in hk_permits + [first_b.permit, first_c.permit]:
            await permit.release()
        await redis.aclose()

    asyncio.run(run())


def test_full_hk_share_does_not_serialize_batch_behind_a_health_probe():
    calls = []
    first_hk_started = asyncio.Event()
    release = asyncio.Event()

    async def transport(pool, _payload, _request_id):
        calls.append(pool.name)
        if pool.name == "hk":
            first_hk_started.set()
            await release.wait()
        return pool.name

    scheduler = _scheduler(transport, share=1.0)

    async def run():
        first = asyncio.create_task(scheduler.request(
            {},
            batch_key="hk-primary-batch",
            paper_key="paper-1",
            teacher_id="teacher-a",
        ))
        await asyncio.wait_for(first_hk_started.wait(), timeout=0.2)
        second = asyncio.create_task(scheduler.request(
            {},
            batch_key="hk-primary-batch",
            paper_key="paper-2",
            teacher_id="teacher-a",
        ))
        for _ in range(20):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.01)
        assert calls[:2] == ["hk", "hk"]
        release.set()
        assert await asyncio.gather(first, second) == ["hk", "hk"]

    asyncio.run(run())
