"""Per-model DashScope budget: own window per model, tokens not bytes, wait not fail."""
import asyncio

import pytest

import config
from services import dashscope_budget


class _Limiter:
    def __init__(self):
        self.calls = []

    async def acquire_rate_budget(self, **kwargs):
        self.calls.append(kwargs)


def _payload(model, text, *, images=0, max_tokens=8000):
    content = [{"type": "text", "text": text}] + [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 4_000_000}}
        for _ in range(images)
    ]
    return {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": content}]}


def test_student_template_and_grading_flash_share_one_window():
    limiter = _Limiter()
    for phase in ("学生转录", "模板转录", "判分"):
        asyncio.run(dashscope_budget.acquire(
            limiter, model="qwen3.8-flash", payload=_payload("qwen3.8-flash", phase),
        ))
    assert [call["provider"] for call in limiter.calls] == ["dashscope:qwen3.8-flash"] * 3
    for call in limiter.calls:
        rpm, tpm = config.DASHSCOPE_MODEL_RATE_LIMITS["qwen3.8-flash"]
        assert (call["requests_per_minute"], call["tokens_per_minute"]) == (rpm, tpm)
        assert call["timeout_seconds"] == config.MODEL_RATE_BUDGET_WAIT_SECONDS > 30


def test_chinese_text_is_counted_as_tokens_not_utf8_bytes():
    text = "学生答案" * 2000  # 8000 characters, 24000 UTF-8 bytes
    reserved = dashscope_budget.estimate_request_tokens(_payload("qwen3.8-flash", text, max_tokens=0))
    assert 8000 <= reserved < 8100


def test_image_is_a_flat_allowance_regardless_of_base64_size():
    reserved = dashscope_budget.estimate_request_tokens(_payload("qwen3.8-flash", "", images=2, max_tokens=9000))
    assert reserved < 9000 + 2 * dashscope_budget.IMAGE_TOKENS + 50


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        ({"total_tokens": 17}, 17),
        ({"input_tokens": 11, "output_tokens": 6}, 17),
        ({"prompt_tokens": 11, "completion_tokens": 6}, 17),
        ({"total_tokens": 17, "input_tokens": 11, "output_tokens": 6}, 17),
    ],
)
def test_actual_usage_tokens_accepts_provider_usage_shapes(usage, expected):
    assert dashscope_budget.actual_usage_tokens(usage) == expected


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"input_tokens": 11},
        {"total_tokens": 17, "input_tokens": 11, "output_tokens": 5},
        {"input_tokens": 11, "prompt_tokens": 12, "output_tokens": 6},
        {"input_tokens": True, "output_tokens": 6},
    ],
)
def test_actual_usage_tokens_keeps_unknown_or_conflicting_usage_conservative(usage):
    assert dashscope_budget.actual_usage_tokens(usage) is None


class _Body:
    def __init__(self, text=None, body=None):
        if text is not None:
            self.text = text
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _sse(*events):
    import json

    return "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"


_FINAL = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}


@pytest.mark.parametrize(("response", "expected"), [
    # Usage in its own event after finish_reason.
    (_Body(_sse({"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]},
                {"choices": [], "usage": _FINAL})), _FINAL),
    # OpenAI-style null placeholders on every earlier chunk.
    (_Body(_sse({"choices": [{"delta": {"content": "x"}}], "usage": None},
                {"choices": [], "usage": _FINAL})), _FINAL),
    # Cumulative usage: the last concrete object wins.
    (_Body(_sse({"choices": [{"delta": {"content": "x"}}], "usage": {"total_tokens": 4}},
                {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": _FINAL})), _FINAL),
    # Content mentioning usage is JSON-escaped and never parsed as usage.
    (_Body(_sse({"choices": [{"delta": {"content": '{"usage": 1}'}, "finish_reason": "stop"}]})),
     None),
    (_Body('{"choices": [], "usage": {"total_tokens": 10}}'), {"total_tokens": 10}),
    (_Body("", {"usage": {"total_tokens": 10}}), {"total_tokens": 10}),
    (_Body(body={"usage": {"total_tokens": 10}}), {"total_tokens": 10}),
    (_Body(body=None), None),
    (_Body("not json"), None),
    (_Body('{"usage": [1, 2]}'), None),
    # A truncated usage tail makes usage unknown, never an earlier value.
    (_Body(_sse({"choices": [], "usage": {"total_tokens": 4}})
           + 'data: {"choices": [], "usage": {"total_tokens": \n\n'), None),
])
def test_response_usage_reads_only_the_final_reported_usage(response, expected):
    assert dashscope_budget.response_usage(response) == expected


@pytest.mark.parametrize(("started", "error", "unsent"), [
    (False, TimeoutError("lease"), True),
    (False, asyncio.CancelledError(), True),
    (True, asyncio.CancelledError(), False),
    (True, RuntimeError("unknown"), False),
])
def test_only_unstarted_or_connection_phase_requests_are_unsent(started, error, unsent):
    assert dashscope_budget.request_was_not_sent(started, error) is unsent


def test_connection_phase_transport_errors_are_unsent_and_later_ones_are_not():
    import httpx

    request = httpx.Request("POST", "https://unit.invalid/v1/chat/completions")
    for error in (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError):
        assert dashscope_budget.request_was_not_sent(True, error("x", request=request))
    for error in (httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.WriteError,
                  httpx.RemoteProtocolError):
        assert not dashscope_budget.request_was_not_sent(True, error("x", request=request))


@pytest.mark.parametrize("model", ["qwen-retired", "qwen3.7-plus", "qwen-vl-plus"])
def test_undeclared_or_retired_model_fails_closed(model):
    with pytest.raises(ValueError, match="no declared rate budget"):
        asyncio.run(dashscope_budget.acquire(_Limiter(), model=model, payload={}))
