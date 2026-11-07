"""Student Flash migration: pinned wire identity and one shared payg budget.

These tests use synthetic image bytes, mock HTTP and the production Redis Lua
scripts through fakeredis. They make no provider calls and load no dotenv files.
"""
import asyncio
import json
import os
from pathlib import Path
import runpy

import fakeredis.aioredis
import httpx
import pytest

import config
from services import flash_grading as flash
from services import template_column_layout as layout
from services import ocr_service as ocr
from services.key_pool import KeyPool
from services.model_concurrency import RedisModelLimiter
from services import template_column_layout_provider


@pytest.mark.parametrize("legacy_model", [
    "qwen3.7-plus", "qwen3.7-plus-2026-05-26", "qwen-vl-plus", "qwen3.8-max",
])
def test_stale_model_override_cannot_restore_plus(monkeypatch, tmp_path, legacy_model):
    image = tmp_path / "evidence.png"
    image.write_bytes(b"synthetic-image")
    payloads = []

    async def post(url, headers, payload, api_key):
        payloads.append(payload)
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "model": "qwen3.8-flash", "choices": [{
                "message": {"content": "{}"}, "finish_reason": "stop",
            }],
        })

    monkeypatch.setenv("DASHSCOPE_MODEL", legacy_model)
    monkeypatch.setenv("OCR_TRANSCRIPTION_REASONING_EFFORT", "none")
    monkeypatch.setattr(ocr, "_vision_pool", None)
    monkeypatch.setattr(ocr, "_post_vision_request", post)
    receipt = {}
    assert asyncio.run(ocr.call_vision_images(
        [str(image)], "transcription", request_receipt=receipt,
    )) == "{}"
    assert payloads[0]["model"] == "qwen3.8-flash"
    assert payloads[0]["enable_thinking"] is False
    assert payloads[0]["reasoning_effort"] == "none"
    assert payloads[0]["temperature"] == 0
    assert payloads[0]["stream"] is True
    assert receipt["requested_model"] == "qwen3.8-flash"
    assert receipt["response_model"] == "qwen3.8-flash"
    assert receipt["reasoning_effort"] == "none"


@pytest.mark.parametrize("capacity,base,burst,expected,expected_burst", [
    ("500", "30", "60", 256, 60),
    ("128", "30", "60", 128, 60),
    # The versioned production drop-in still supplies the former 352 burst.
    ("500", "96", "352", 256, 256),
    ("256", "96", "352", 256, 256),
])
def test_config_pins_flash_and_caps_legacy_global_capacity(
    monkeypatch, capacity, base, burst, expected, expected_burst,
):
    # Execute the entire module against an isolated environment without
    # replacing the live config module or relying on Windows DLL startup.
    env = os.environ.copy()
    env.update({
        "PYTHON_DOTENV_DISABLED": "1",
        "DASHSCOPE_MODEL": "qwen3.7-plus",
        "TEMPLATE_CUT_MODEL": "qwen3.7-plus",
        "DASHSCOPE_GLOBAL_CONCURRENCY": capacity,
        "MODEL_TEACHER_BASE_CONCURRENCY": base,
        "MODEL_TEACHER_BURST_CONCURRENCY": burst,
        "MODEL_GLOBAL_RESERVE_CONCURRENCY": "30",
        "OCR_TRANSCRIPTION_REASONING_EFFORT": "none",
    })
    with monkeypatch.context() as isolated:
        isolated.setattr(os, "environ", env)
        loaded = runpy.run_path(str(Path(config.__file__)))
    assert {
        "model": loaded["DASHSCOPE_MODEL"],
        "template_model": loaded["TEMPLATE_CUT_MODEL"],
        "capacity": loaded["DASHSCOPE_GLOBAL_CONCURRENCY"],
        "burst": loaded["MODEL_TEACHER_BURST_CONCURRENCY"],
        "models": list(loaded["DASHSCOPE_MODEL_RATE_LIMITS"]),
    } == {
        "model": "qwen3.8-flash", "template_model": "qwen3.8-flash",
        "capacity": expected,
        "burst": expected_burst, "models": ["qwen3.8-flash"],
    }


def test_mixed_ocr_layout_and_grading_share_capacity_across_workers(monkeypatch, tmp_path):
    """Layout's 1-slot lane must neither shrink nor bypass the shared 3-slot gate."""
    image = tmp_path / "evidence.png"
    image.write_bytes(b"synthetic-image")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-shared-key")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://mock.invalid/v1")
    monkeypatch.setenv("OCR_TRANSCRIPTION_REASONING_EFFORT", "none")
    monkeypatch.setattr(ocr, "_vision_pool", None)
    monkeypatch.setattr(flash, "_credential_failures", {})
    monkeypatch.setattr(ocr, "DASHSCOPE_GLOBAL_CONCURRENCY", 3)
    monkeypatch.setattr(ocr, "MODEL_TEACHER_BASE_CONCURRENCY", 1)
    monkeypatch.setattr(config, "DASHSCOPE_GLOBAL_CONCURRENCY", 3)
    monkeypatch.setattr(config, "MODEL_TEACHER_BASE_CONCURRENCY", 1)
    monkeypatch.setattr(config, "MODEL_TEACHER_BURST_CONCURRENCY", 3)
    monkeypatch.setattr(config, "MODEL_GLOBAL_RESERVE_CONCURRENCY", 0)
    monkeypatch.setattr(template_column_layout_provider, "DASHSCOPE_GLOBAL_CONCURRENCY", 3)
    monkeypatch.setattr(template_column_layout_provider, "TEMPLATE_CUT_KEY_CONCURRENCY", 1)

    real_client = httpx.AsyncClient

    async def run():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        worker_a = RedisModelLimiter(redis, acquire_timeout_seconds=2, operation_timeout_seconds=1)
        worker_b = RedisModelLimiter(redis, acquire_timeout_seconds=2, operation_timeout_seconds=1)
        monkeypatch.setattr(ocr, "get_model_limiter", lambda: worker_a)
        entered = []
        two_ocr_started = asyncio.Event()
        layout_started = asyncio.Event()
        release_http = asyncio.Event()
        state = {"active": 0, "peak": 0}

        async def handler(request):
            payload = json.loads(request.content)
            kind = payload["messages"][0]["content"]
            kind = kind if isinstance(kind, str) else kind[0]["text"]
            entered.append(kind)
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            if entered.count("ocr") == 2:
                two_ocr_started.set()
            if kind == "layout":
                layout_started.set()
            try:
                await release_http.wait()
                return httpx.Response(200, json={
                    "model": "qwen3.8-flash", "choices": [{
                        "message": {"content": "{}"}, "finish_reason": "stop",
                    }],
                })
            finally:
                state["active"] -= 1

        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(ocr.httpx, "AsyncClient", lambda **kwargs: real_client(
            transport=transport, **kwargs,
        ))
        tasks = []
        try:
            tasks.extend(asyncio.create_task(ocr.call_vision_images(
                [str(image)], "ocr",
            )) for _ in range(2))
            await asyncio.wait_for(two_ocr_started.wait(), timeout=1)
            async with real_client(transport=transport) as client:
                tasks.append(asyncio.create_task(layout._post_with_shared_lease(
                    client=client, limiter=worker_b, api_key="test-only-shared-key",
                    base_url="https://mock.invalid/v1",
                    pool=KeyPool(["test-only-shared-key"], max_concurrent_per_key=1),
                    teacher_id="system", request_json={
                        "model": "qwen3.8-flash",
                        "messages": [{"role": "user", "content": "layout"}],
                    },
                )))
                # Two student requests already hold the shared credential; a
                # layout call must still use the third aggregate permit.
                await asyncio.wait_for(layout_started.wait(), timeout=1)
                tasks.append(asyncio.create_task(flash.call_flash(
                    [{"role": "user", "content": "grading"}],
                    phase="text", teacher_id="system",
                )))
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(tasks[-1]), timeout=0.03)
                assert "grading" not in entered
                global_key = worker_a._keys("dashscope", "test-only-shared-key", "system")[0]
                assert await redis.zcard(global_key) == 3
                assert global_key == worker_b._keys("dashscope", "other-key", "other-teacher")[0]
                release_http.set()
                results = await asyncio.gather(*tasks)
                assert results[:2] == ["{}", "{}"]
                assert results[2].status_code == 200
                assert results[3]["content"] == "{}"
                assert state["peak"] == 3
                assert entered == ["ocr", "ocr", "layout", "grading"]
                assert await redis.zcard(global_key) == 0
        finally:
            release_http.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await redis.aclose()

    asyncio.run(run())
