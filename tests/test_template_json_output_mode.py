"""Offline contracts for the explicit opt-in JSON output mode.

No network, no credential and no paid provider is touched: Flash contracts
inspect the payload a service hands to the transport stub, and the OCR contract
follows the opt-in from the Responses-shaped internal payload through the real
Qwen chat adapter to the provider wire body.
"""
import asyncio
import copy
import json

import httpx
import pytest

from services import flash_grading as flash
from services import ocr_pool_scheduler as sched
from services import ocr_service as ocr
from services.key_pool import KeyPool
from services import ocr_service_template_route


class Limiter:
    async def is_credential_invalid(self, **_kw):
        return False

    async def credential_cooldown_seconds(self, **_kw):
        return 0

    async def mark_credential_invalid(self, **_kw):
        return None

    async def mark_credential_cooldown(self, **_kw):
        return None


class _Stop(Exception):
    """Sentinel that stops a contract test right after payload hand-off."""


def _forbid_transport(*_args, **_kwargs):
    raise AssertionError("these contracts must not reach a transport")


def _event(content, *, finish=None, model=flash.FLASH_MODEL):
    return {
        "model": model,
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": finish}],
    }


def _response():
    content = json.dumps({"results": {"1": {"result": "correct", "explanation": "", "hint": "", "model_answer": "", "standard_answer": ""}}})
    text = "data: " + json.dumps(_event(content, finish="stop")) + "\n\ndata: [DONE]\n\n"
    return httpx.Response(
        200,
        text=text,
        headers={},
        request=httpx.Request("POST", "https://unit.invalid/v1/chat/completions"),
    )


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(
        ocr, "_vision_pool", KeyPool(["test-only-json-key"], max_concurrent_per_key=None)
    )
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-json-key")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://unit.invalid/v1")
    monkeypatch.setattr(ocr, "get_model_limiter", lambda: Limiter())
    monkeypatch.setattr(flash, "_credential_failures", {})
    monkeypatch.setattr(flash, "_semaphore", None)
    monkeypatch.setattr(flash, "_semaphore_loop", None)
    monkeypatch.setattr(ocr, "_post_vision_request", _forbid_transport)


# --------------------------------------------------------------------------- #
# Flash
# --------------------------------------------------------------------------- #
def _verdict_response():
    content = json.dumps({"result": "correct", "p_correct": 0.9, "reason": "作图符合要求"})
    text = "data: " + json.dumps(_event(content, finish="stop")) + "\n\ndata: [DONE]\n\n"
    return httpx.Response(
        200,
        text=text,
        headers={},
        request=httpx.Request("POST", "https://unit.invalid/v1/chat/completions"),
    )


def test_text_judge_opts_into_json_mode_on_the_wire(monkeypatch):
    """The grading judge must ask the provider for JSON output on the wire.

    Incident 742ca8b080544eb09292137688139a7f: the text judge left JSON to
    chance and free-form completions burned ~40s each as parse failures.
    """
    captured = []

    async def post(url, headers, payload, api_key):
        captured.append(copy.deepcopy(payload))
        return _response()

    monkeypatch.setattr(ocr, "_post_vision_request", post)
    judged = asyncio.run(
        flash.judge_text_paper(
            [{"question_no": "1", "question_text": "1+1=?", "standard_answer": "2"}],
        )
    )
    assert captured and captured[0]["response_format"]["type"] == "json_schema"
    schema = captured[0]["response_format"]["json_schema"]
    assert schema["strict"] is True
    assert schema["schema"]["properties"]["results"]["required"] == ["1"]
    assert captured[0].get("enable_thinking") is True
    assert judged["1"]["result"] == "correct"


def test_reference_drawing_judge_opts_into_json_mode_on_the_wire(monkeypatch):
    captured = []

    async def post(url, headers, payload, api_key):
        captured.append(copy.deepcopy(payload))
        return _verdict_response()

    monkeypatch.setattr(ocr, "_post_vision_request", post)
    from services import drawing_grading as drawing

    checks = asyncio.run(drawing.assess_inputs({
        "overlays": ["data:image/png;base64,iVBORw0KGgo="],
        "variant_prompts": ["画图核对"],
        "variant_source_images": [None],
    }))
    assert captured and captured[0].get("response_format") == {"type": "json_object"}
    assert checks[0]["result"] == "correct"


def test_manual_drawing_judge_opts_into_json_mode_on_the_wire(monkeypatch):
    captured = []

    async def post(url, headers, payload, api_key):
        captured.append(copy.deepcopy(payload))
        return _verdict_response()

    monkeypatch.setattr(ocr, "_post_vision_request", post)
    import numpy as np
    from services import drawing_grading as drawing

    verdict = asyncio.run(drawing.assess_manual_input(
        np.zeros((10, 10, 3), dtype=np.uint8),
        {"question_text": "在网格中作图", "drawing_regions": [[0, 0, 5, 5]]},
    ))
    assert captured and captured[0].get("response_format") == {"type": "json_object"}
    assert verdict["model_called"] is True


def test_flash_default_payload_is_unchanged_without_the_opt_in():
    messages = [{"role": "user", "content": "{}"}]
    implicit = flash._payload(messages, "text")
    explicit = flash._payload(messages, "text", json_object=False)
    assert implicit == {**flash.FLASH_PARAMETERS, "messages": messages}
    assert "response_format" not in implicit
    assert json.dumps(implicit, sort_keys=True) == json.dumps(explicit, sort_keys=True)


def test_flash_hierarchy_json_mode_reaches_the_wire_with_thinking_intact(monkeypatch):
    captured = []

    async def post(url, headers, payload, api_key):
        captured.append(copy.deepcopy(payload))
        return _response()

    monkeypatch.setattr(ocr, "_post_vision_request", post)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "层级"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
            ],
        }
    ]
    result = asyncio.run(
        flash.call_flash(
            messages,
            phase="template_hierarchy",
            reasoning_effort="high",
            max_tokens=8000,
            enable_thinking=True,
            json_object=True,
        )
    )
    body = captured[0]
    assert body["response_format"] == {"type": "json_object"}
    assert "text" not in body
    # The thinking tier is passed through exactly as requested.
    assert body["enable_thinking"] is True
    assert body["reasoning_effort"] == "high"
    assert body["max_tokens"] == 8000
    assert body["stream"] is True and body["temperature"] == 0
    assert body["model"] == flash.FLASH_MODEL
    assert result["parameters"]["response_format"] == {"type": "json_object"}

    asyncio.run(flash.call_flash([{"role": "user", "content": "{}"}], phase="text"))
    assert len(captured) == 2
    assert "response_format" not in captured[1]


def test_flash_json_object_rejects_bad_values_without_transport():
    messages = [{"role": "user", "content": "{}"}]
    for bad in ("true", 1, 0, None, {"type": "json_object"}):
        with pytest.raises(flash.FlashGradingError):
            flash._payload(messages, "text", json_object=bad)
    with pytest.raises(flash.FlashGradingError):
        asyncio.run(flash.call_flash(messages, phase="text", json_object="yes"))


# --------------------------------------------------------------------------- #
# OCR service -> scheduler -> provider wire
# --------------------------------------------------------------------------- #
class _CapturingScheduler:
    """Records the internal Responses payload the OCR service hands over."""

    def __init__(self):
        self.payloads = []

    async def request_template_fallback_pool(self, payload, **_kwargs):
        self.payloads.append(copy.deepcopy(payload))
        raise _Stop()


class _FakeHttpClient:
    """Client stub that captures the built body and answers with a provider 500."""

    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    def build_request(self, method, url, headers=None, json=None):
        _FakeHttpClient.captured = {"url": url, "headers": headers, "body": json}
        return httpx.Request(method, url)

    async def send(self, request, stream=True):  # noqa: ARG002
        return httpx.Response(500, text="upstream-error", headers={}, request=request)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


def _wire_body(monkeypatch, payload, *, protocol, model="qwen3.8-max"):
    monkeypatch.setattr(sched.httpx, "AsyncClient", _FakeHttpClient)
    _FakeHttpClient.captured = {}
    pool = sched.PoolSpec(
        name="unit-template-pool",
        base_url="https://unit.invalid/v1",
        api_key="test-only-json-key",
        model=model,
        max_rpm=60.0,
        hard_in_flight=1,
        first_event_timeout_s=5.0,
        attempt_ttl_s=5.0,
        api_protocol=protocol,
    )
    scheduler = object.__new__(sched.OcrPoolScheduler)
    scheduler.connect_timeout_s = 5.0
    scheduler.write_timeout_s = 5.0
    scheduler.sse_idle_timeout_s = 5.0
    with pytest.raises(sched.OcrPoolError):
        asyncio.run(scheduler._send_stream(pool, payload, "unit-request-id"))
    return _FakeHttpClient.captured


def test_ocr_opt_in_survives_the_adapter_to_the_qwen_wire(monkeypatch):
    monkeypatch.setattr(
        ocr_service_template_route,
        "_encode_vision_image",
        lambda path: ("aGVsbG8=", "image/png"),
    )
    monkeypatch.setattr(ocr, "_encode_vision_image", lambda path: ("aGVsbG8=", "image/png"))
    scheduler = _CapturingScheduler()
    for flag in (True, False):
        with pytest.raises(_Stop):
            asyncio.run(
                ocr._call_multi_pool_vision_request(
                    "unit-template.png",
                    "定位题号",
                    json_object=flag,
                    scheduler=scheduler,
                    pool_route="fallback",
                )
            )
    opted_in, legacy = scheduler.payloads
    assert opted_in["text"] == {"format": {"type": "json_object"}}
    assert "text" not in legacy

    wire = _wire_body(monkeypatch, opted_in, protocol="chat_completions")
    assert wire["url"].endswith("/chat/completions")
    assert wire["body"]["response_format"] == {"type": "json_object"}
    assert "text" not in wire["body"]
    assert wire["body"]["model"] == "qwen3.8-max"
    assert wire["body"]["stream"] is True and wire["body"]["temperature"] == 0
    assert wire["body"]["messages"][0]["role"] == "user"

    untouched = _wire_body(
        monkeypatch, legacy, protocol="chat_completions", model="qwen3.8-max"
    )
    assert "response_format" not in untouched["body"]
    assert "text" not in untouched["body"]

    # The internal Responses metadata is never forwarded to a non-Qwen gateway.
    responses_gateway = _wire_body(
        monkeypatch, opted_in, protocol="responses", model="gpt-unit-responses"
    )
    assert "text" not in responses_gateway["body"]
    assert "response_format" not in responses_gateway["body"]
    assert responses_gateway["body"]["input"] == opted_in["input"]

    chat_gateway = _wire_body(
        monkeypatch, opted_in, protocol="chat_completions", model="gpt-unit-chat"
    )
    assert "response_format" not in chat_gateway["body"]
    assert "text" not in chat_gateway["body"]

    # A Qwen pool served over Responses keeps the field in its native spelling.
    qwen_responses = _wire_body(
        monkeypatch, opted_in, protocol="responses", model="qwen3.8-max-unit"
    )
    assert qwen_responses["body"]["text"] == {"format": {"type": "json_object"}}
    assert "response_format" not in qwen_responses["body"]


def test_call_gpt_vision_forwards_the_opt_in(tmp_path, monkeypatch):
    seen = {}

    async def recorder(image_path, prompt, max_tokens=6000, **kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(ocr_service_template_route, "_call_multi_pool_vision_request", recorder)
    image = tmp_path / "unit-template.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    assert asyncio.run(ocr.call_gpt_vision(str(image), "定位题号", json_object=True)) == "ok"
    assert seen["json_object"] is True
    assert seen["prompt_first"] is False


def test_ocr_json_object_rejects_bad_values_without_encoding(monkeypatch):
    monkeypatch.setattr(ocr_service_template_route, "_encode_vision_image", _forbid_transport)
    monkeypatch.setattr(ocr, "_encode_vision_image", _forbid_transport)
    for bad in ("json_object", 1, 0, None, {"type": "json_object"}):
        with pytest.raises(ValueError):
            asyncio.run(
                ocr._call_multi_pool_vision_request(
                    "unit-template.png",
                    "定位题号",
                    json_object=bad,
                    scheduler=_CapturingScheduler(),
                    pool_route="canonical",
                )
            )
