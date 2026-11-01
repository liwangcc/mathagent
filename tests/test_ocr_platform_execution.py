from __future__ import annotations

import asyncio
import json

import pytest

from services.ocr_pool_scheduler import (
    LocalPoolCoordinator,
    OcrPoolScheduler,
    PoolSpec,
)


def _pool(name: str, *, profile: str) -> PoolSpec:
    return PoolSpec(
        name=name,
        base_url=f"https://{name}.example/v1",
        api_key=f"key-{name}",
        model="gpt-5.6-sol",
        max_rpm=60_000,
        hard_in_flight=4,
        first_event_timeout_s=0.5,
        attempt_ttl_s=2.0,
        burst=100,
        provider_profile=profile,
    )


def _scheduler(transport) -> OcrPoolScheduler:
    return OcrPoolScheduler(
        primary=_pool("primary", profile="primary"),
        failover_pool=None,
        accelerator=_pool("accelerator", profile="accelerator"),
        retry_pool=_pool("retry", profile="retry"),
        coordinator=LocalPoolCoordinator(global_rpm=60_000, global_burst=100),
        global_first_attempt_rpm=60_000,
        primary_soft_in_flight=2,
        accelerator_share=0.0,
        batch_deadline_s=10.0,
        final_guard_s=0.1,
        max_batch_retries=1,
        circuit_failure_threshold=3,
        circuit_cooldown_s=1.0,
        connect_timeout_s=0.5,
        write_timeout_s=0.5,
        sse_idle_timeout_s=0.5,
        transport=transport,
    )


def test_pool_rejects_platform_model_execution() -> None:
    with pytest.raises(ValueError, match="execute on the server"):
        PoolSpec(
            name="platform",
            base_url="",
            api_key="",
            model="gpt-5.6-sol",
            max_rpm=30,
            hard_in_flight=1,
            first_event_timeout_s=30.0,
            attempt_ttl_s=60.0,
            execution_target="platform_node",
        )


def test_scheduler_uses_server_transport_only() -> None:
    calls = []

    async def server_transport(pool, payload, request_id):
        calls.append((pool, payload, request_id))
        return '{"ok":true}'

    result = asyncio.run(
        _scheduler(server_transport).request(
            {"input": [{"role": "user", "content": "test"}]},
            batch_key="batch-server-only",
            paper_key="paper-1",
            teacher_id="teacher-7",
        )
    )

    assert json.loads(result) == {"ok": True}
    assert len(calls) == 1
    assert calls[0][0].execution_target == "server"
    assert calls[0][2].startswith("ocr-")


def test_retired_pool_graph_constructor_is_gone() -> None:
    """The HK/proxy/VIP graph has no constructor left to reach it."""
    import services.ocr_pool_scheduler as scheduler_module

    assert not hasattr(OcrPoolScheduler, "from_config")
    assert not hasattr(scheduler_module, "get_ocr_pool_scheduler")


def test_student_vision_service_uses_server_owned_qwen_route(monkeypatch, tmp_path) -> None:
    from services import ocr_service

    image = tmp_path / "paper.jpg"
    image.write_bytes(b"image")
    captured = {}

    async def qwen_request(image_path, prompt, max_tokens=8192, **kwargs):
        captured["image_path"] = image_path
        captured["prompt"] = prompt
        captured["max_tokens"] = max_tokens
        captured["kwargs"] = kwargs
        return '{"ok":true}'

    # Student OCR no longer uses the GPT scheduler. Keep this test offline and
    # assert that the production entry point selects the Qwen adapter directly.
    monkeypatch.setattr(ocr_service, "_call_vision_request", qwen_request)

    token = ocr_service.set_vision_teacher_id("teacher-7")
    try:
        result = asyncio.run(
            ocr_service.call_vision(
                str(image),
                "return JSON",
                max_tokens=512,
                prompt_first=True,
            )
        )
    finally:
        ocr_service.reset_vision_teacher_id(token)

    assert json.loads(result) == {"ok": True}
    assert captured["image_path"] == str(image)
    assert captured["prompt"] == "return JSON"
    assert captured["max_tokens"] == 512
    assert captured["kwargs"]["prompt_first"] is True
