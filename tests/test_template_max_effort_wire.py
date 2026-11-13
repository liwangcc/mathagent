"""The effort a template Max call asks for is the effort on the wire, whatever the model is called.

Two places used to drop it without a word: the template route handed the
scheduler a fixed ``high`` (which the scheduler writes over the payload's
effort), and the chat adapter sent ``reasoning_effort`` only to models named
``qwen3.8*``, so a renamed model such as qwen4-max-preview ran at the
provider's default tier.
"""

import asyncio
from dataclasses import replace

import pytest
from PIL import Image

from tests.test_ocr_pool_scheduler import _AsyncSseStream, _pool, _scheduler
from tests.test_template_max_dead_stream_resend import ANSWER, ANSWERED, serve_streams


def max_scheduler(model):
    """A real scheduler whose Max pool speaks the provider's chat protocol over the default transport."""
    scheduler = _scheduler(None)
    scheduler.template_fallback_pool = replace(_pool("max", ttl=30.0), model=model, api_protocol="chat_completions")
    return scheduler


def send_template_image(tmp_path, monkeypatch, model, **options):
    """One template Max call from the pipeline's entry point; returns the HTTP body and the route diagnostics."""
    import services.ocr_pool_scheduler as scheduler_module
    from services import ocr_service

    image = tmp_path / "page.png"
    Image.new("RGB", (40, 40), "white").save(image)
    bodies = serve_streams(monkeypatch, [_AsyncSseStream(ANSWERED)])
    scheduler = max_scheduler(model)
    monkeypatch.setattr(scheduler_module, "get_template_ocr_pool_scheduler", lambda *, pool_route=None: scheduler)
    diagnostics = {}
    answer = asyncio.run(ocr_service.call_gpt_vision(
        str(image), "定位", json_object=True, route_diagnostics=diagnostics, **options))
    assert answer == ANSWER and len(bodies) == 1
    return bodies[0], diagnostics


@pytest.mark.parametrize("model", ["qwen4-max-preview", "qwen3.8-max"])
@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_the_template_call_sends_the_requested_effort_to_any_qwen_model(tmp_path, monkeypatch, model, effort):
    body, diagnostics = send_template_image(tmp_path, monkeypatch, model, reasoning_effort=effort)

    assert body["model"] == model
    assert body["reasoning_effort"] == effort
    assert body["enable_thinking"] is True
    assert body["response_format"] == {"type": "json_object"}
    assert "model_reasoning_effort" not in body
    assert diagnostics["requested_reasoning_effort"] == diagnostics["actual_reasoning_effort"] == effort


def test_a_template_call_without_an_effort_sends_the_configured_default(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_REASONING_EFFORT", "medium")

    body, _ = send_template_image(tmp_path, monkeypatch, "qwen4-max-preview")

    assert body["reasoning_effort"] == "medium"


def test_the_max_pool_refuses_an_effort_the_provider_does_not_know():
    scheduler = max_scheduler("qwen4-max-preview")

    with pytest.raises(ValueError, match="template Qwen reasoning must be one of"):
        asyncio.run(scheduler.request_template_fallback_pool(
            {"input": []}, request_key="page-1", reasoning_effort="extreme"))
