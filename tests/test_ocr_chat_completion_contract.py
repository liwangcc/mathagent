"""Exercise actual HTTP request construction, never a live provider."""

import asyncio
from dataclasses import replace

import pytest

from tests.test_ocr_pool_scheduler import _install_chat_transport, _scheduler
from services.ocr_pool_scheduler import OcrPoolError


def _payload(**overrides):
    return {"input": [{"role": "user", "content": [{"type": "input_text", "text": "Q"}]}], **overrides}


@pytest.mark.parametrize("model", ["qwen3.8-max", "qwen3.8-flash"])
@pytest.mark.parametrize("phase", [
    "template-directory-v1", "template-directory-solve-v1",
    "template-directory-drawing-v1", "template-drawing-reference-v1",
])
def test_qwen_template_effort_is_mapped_without_prompt_substring(monkeypatch, model, phase):
    captured = {}
    _install_chat_transport(monkeypatch, [
        {"choices": [{"delta": {"content": '{"ok":true}'}, "finish_reason": "stop"}]},
    ], captured)
    scheduler = _scheduler(None)
    pool = replace(scheduler.accelerator, model=model, api_protocol="chat_completions")
    payload = {
        "input": [{"role": "user", "content": [{"type": "input_text", "text": phase}]}],
        "model_reasoning_effort": "high", "max_output_tokens": 16000,
    }
    assert asyncio.run(scheduler._send_stream(pool, payload, phase)) == '{"ok":true}'
    body = captured["body"]
    assert body["reasoning_effort"] == "high"
    assert body["enable_thinking"] is True
    assert "model_reasoning_effort" not in body
    assert body["max_tokens"] == 16000
    assert body["messages"][0]["content"][0]["text"] == phase
    assert payload["model_reasoning_effort"] == "high"


@pytest.mark.parametrize("effort,expected,thinking", [
    ("none", "none", False), ("minimal", "low", True),
    ("low", "low", True), ("medium", "medium", True),
    ("high", "high", True), ("xhigh", "xhigh", True), ("max", "max", True),
])
def test_qwen_effort_levels_map_to_documented_provider_contract(monkeypatch, effort, expected, thinking):
    captured = {}
    _install_chat_transport(monkeypatch, [
        {"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}]},
    ], captured)
    scheduler = _scheduler(None)
    pool = replace(scheduler.accelerator, model="qwen3.8-max", api_protocol="chat_completions")
    asyncio.run(scheduler._send_stream(pool, _payload(model_reasoning_effort=effort), "levels"))
    assert captured["body"]["reasoning_effort"] == expected
    assert captured["body"]["enable_thinking"] is thinking


def test_qwen37_transcription_disables_thinking_without_internal_field(monkeypatch):
    captured = {}
    _install_chat_transport(monkeypatch, [
        {"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}]},
    ], captured)
    scheduler = _scheduler(None)
    pool = replace(scheduler.accelerator, model="qwen3.7-plus", api_protocol="chat_completions")
    asyncio.run(scheduler._send_stream(pool, _payload(model_reasoning_effort="none"), "ocr"))
    assert captured["body"]["enable_thinking"] is False
    assert "model_reasoning_effort" not in captured["body"]
    assert "reasoning_effort" not in captured["body"]


@pytest.mark.parametrize("finish,expected,retryable", [
    (None, "incomplete_stream", True), ("content_filter", "content_filtered", False),
    ("tool_calls", "unsupported_finish_reason", True),
    ("unknown", "unsupported_finish_reason", True),
    ("length", "truncated_stream", True), ("max_tokens", "truncated_stream", True),
])
def test_chat_never_accepts_unfinished_or_refused_json(monkeypatch, finish, expected, retryable):
    captured = {}
    _install_chat_transport(monkeypatch, [
        {"choices": [{"delta": {"content": '{"ok":true}'}, "finish_reason": finish}]},
    ], captured)
    scheduler = _scheduler(None)
    pool = replace(scheduler.accelerator, api_protocol="chat_completions")
    with pytest.raises(OcrPoolError, match=expected) as error:
        asyncio.run(scheduler._send_stream(pool, _payload(), "finish"))
    assert error.value.retryable is retryable


def test_chat_keeps_explicit_done_compatibility(monkeypatch):
    captured = {}
    _install_chat_transport(monkeypatch, [
        {"choices": [{"delta": {"content": '{"ok":true}'}}]}, "[DONE]",
    ], captured)
    scheduler = _scheduler(None)
    pool = replace(scheduler.accelerator, api_protocol="chat_completions")
    assert asyncio.run(scheduler._send_stream(pool, _payload(), "done")) == '{"ok":true}'
