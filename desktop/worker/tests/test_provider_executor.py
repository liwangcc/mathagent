from __future__ import annotations

import base64
from email.message import Message
import hashlib
import json
from pathlib import Path
import threading
import time
from urllib.error import HTTPError, URLError

import pytest

from mathagent_worker.provider_executor import (
    MAX_RESPONSE_BYTES,
    PROVIDER_RESPONSE_CONTRACT_VERSION,
    ProviderExecutor,
    ProviderExecutorError,
    ProviderProfile,
    ThreadSafeByteBudget,
    _parse_sse,
    estimated_payload_bytes,
)


class FakeResponse:
    def __init__(
        self,
        lines,
        *,
        status: int = 200,
        content_type: str = "text/event-stream; charset=utf-8",
    ) -> None:
        self._lines = [
            line
            for chunk in lines
            for line in chunk.splitlines(keepends=True)
        ]
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def __iter__(self):
        return iter(self._lines)


class DelayedResponse:
    def __init__(self, producer):
        self._producer = producer
        self.closed = threading.Event()

    def __iter__(self):
        return self._producer(self)

    def close(self):
        self.closed.set()


def _event(value: dict) -> bytes:
    return (
        "data:"
        + json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        + "\n\n"
    ).encode("utf-8")


def _completed_lines(text: str = '{"questions":[]}') -> list[bytes]:
    return [
        _event({"type": "response.output_text.delta", "delta": text}),
        _event({
            "type": "response.completed",
            "response": {"status": "completed", "output": []},
        }),
    ]


def _png(path: Path, marker: bytes) -> bytes:
    raw = b"\x89PNG\r\n\x1a\n" + marker
    path.write_bytes(raw)
    return raw


def _asset(raw: bytes, position: int) -> dict:
    return {
        "kind": f"input_{position:02d}",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "byte_size": len(raw),
        "media_type": "image/png",
    }


def _job(raw_images: list[bytes], **overrides) -> dict:
    value = {
        "request_digest": "a" * 64,
        "model": "gpt-5.6-sol",
        "prompt": "只按图片转录并判分。",
        "assets": [_asset(raw, index) for index, raw in enumerate(raw_images, 1)],
        "max_output_tokens": 32_768,
        "reasoning_effort": "high",
        "stream": True,
        "first_event_timeout_s": 60.0,
        "attempt_ttl_s": 180.0,
        "sse_idle_timeout_s": 120.0,
    }
    value.update(overrides)
    return value


def _profile(**overrides) -> ProviderProfile:
    values = {
        "base_url": "https://provider.example/v1",
        "model": "gpt-5.6-sol",
        "api_key": "local-secret-key",
    }
    values.update(overrides)
    return ProviderProfile(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"base_url": "http://provider.example/v1"},
        {"base_url": "https://user@provider.example/v1"},
        {"base_url": "https://provider.example/v1?key=secret"},
        {"base_url": "https://provider.example/v1/../admin"},
        {"base_url": "https://provider.example:bad/v1"},
        {"model": "bad model"},
        {"api_key": "short"},
        {"api_key": "secret\nkey"},
        {"request_timeout_s": 10},
    ],
)
def test_profile_rejects_unsafe_values_without_echoing_them(overrides) -> None:
    with pytest.raises(ProviderExecutorError) as captured:
        _profile(**overrides)
    assert captured.value.code == "provider_profile_invalid"
    assert str(captured.value) == "provider_profile_invalid"
    assert "secret" not in str(captured.value)
    assert "provider.example" not in str(captured.value)


def test_profile_repr_does_not_expose_url_or_key() -> None:
    rendered = repr(_profile())
    assert "local-secret-key" not in rendered
    assert "provider.example" not in rendered
    assert "gpt-5.6-sol" in rendered


def test_execute_rejects_server_model_that_differs_from_local_profile(
    tmp_path: Path,
) -> None:
    image = tmp_path / "one.png"
    raw = _png(image, b"one")
    job = {**_job([raw]), "model": "other-model"}
    executor = ProviderExecutor(
        opener=lambda *_args, **_kwargs: pytest.fail("provider must not be called")
    )

    with pytest.raises(ProviderExecutorError, match="provider_model_mismatch"):
        executor.execute(job, [image], _profile())


def test_execute_sends_ordered_hash_bound_images_with_fixed_responses_parameters(
    tmp_path: Path,
) -> None:
    left = tmp_path / "left.png"
    right = tmp_path / "right.png"
    left_raw = _png(left, b"left")
    right_raw = _png(right, b"right")
    captured = {}

    def opener(request, *, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse(_completed_lines())

    budget = ThreadSafeByteBudget(1024 * 1024)
    result = ProviderExecutor(byte_budget=budget, opener=opener).execute(
        _job([left_raw, right_raw]),
        [left, right],
        _profile(),
    )

    assert captured["url"] == "https://provider.example/v1/responses"
    assert captured["timeout"] == 60.0
    assert captured["headers"]["Authorization"] == "Bearer local-secret-key"
    assert captured["headers"]["Accept"] == "text/event-stream"
    assert captured["headers"]["X-request-id"] == "a" * 64
    body = captured["body"]
    assert body["model"] == "gpt-5.6-sol"
    assert body["reasoning"] == {"effort": "high"}
    assert body["stream"] is True
    assert body["max_output_tokens"] == 32_768
    assert "messages" not in body
    contents = body["input"][0]["content"]
    assert [item["type"] for item in contents] == [
        "input_image",
        "input_image",
        "input_text",
    ]
    assert base64.b64decode(contents[0]["image_url"].split(",", 1)[1]) == left_raw
    assert base64.b64decode(contents[1]["image_url"].split(",", 1)[1]) == right_raw
    assert result["contract_version"] == PROVIDER_RESPONSE_CONTRACT_VERSION
    assert result["content"] == '{"questions":[]}'
    assert result["request_digest"] == "a" * 64
    assert result["metrics"]["image_count"] == 2
    assert budget.used == 0
    encoded = json.dumps(result)
    assert "local-secret-key" not in encoded
    assert "provider.example" not in encoded


def test_completed_event_can_supply_text_without_delta(tmp_path: Path) -> None:
    image = tmp_path / "one.png"
    raw = _png(image, b"one")
    lines = [_event({
        "type": "response.completed",
        "response": {
            "status": "completed",
            "output": [{
                "content": [{"type": "output_text", "text": "fallback"}],
            }],
        },
    })]
    executor = ProviderExecutor(opener=lambda *_args, **_kwargs: FakeResponse(lines))

    result = executor.execute(_job([raw]), [image], _profile())

    assert result["content"] == "fallback"


def test_sse_first_event_timeout_is_enforced() -> None:
    def producer(response):
        assert not response.closed.wait(0.2)
        yield _event({"type": "response.output_text.delta", "delta": "late"})

    response = DelayedResponse(producer)
    with pytest.raises(ProviderExecutorError, match="provider_timeout"):
        _parse_sse(
            response,
            first_event_timeout_s=0.02,
            attempt_ttl_s=0.2,
            sse_idle_timeout_s=0.05,
        )
    assert response.closed.is_set()


def test_sse_idle_timeout_is_enforced_between_events() -> None:
    def producer(response):
        yield _event({"type": "response.output_text.delta", "delta": "first"})
        assert not response.closed.wait(0.2)
        yield _event({
            "type": "response.completed",
            "response": {"status": "completed", "output": []},
        })

    response = DelayedResponse(producer)
    with pytest.raises(ProviderExecutorError, match="provider_timeout"):
        _parse_sse(
            response,
            first_event_timeout_s=0.2,
            attempt_ttl_s=0.5,
            sse_idle_timeout_s=0.02,
        )
    assert response.closed.is_set()


def test_sse_absolute_attempt_ttl_is_enforced_even_with_keepalives() -> None:
    def producer(response):
        yield _event({"type": "response.output_text.delta", "delta": "first"})
        while not response.closed.is_set():
            yield b":keepalive\n\n"
            time.sleep(0.005)

    response = DelayedResponse(producer)
    with pytest.raises(ProviderExecutorError, match="provider_timeout"):
        _parse_sse(
            response,
            first_event_timeout_s=0.2,
            attempt_ttl_s=0.04,
            sse_idle_timeout_s=0.2,
        )
    assert response.closed.is_set()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("first_event_timeout_s", 4.0),
        ("attempt_ttl_s", 20.0),
        ("sse_idle_timeout_s", 9.0),
        ("first_event_timeout_s", 200.0),
        ("attempt_ttl_s", 700.0),
        ("sse_idle_timeout_s", 301.0),
    ],
)
def test_execute_rejects_out_of_range_stream_timeouts(
    tmp_path: Path,
    field: str,
    value: float,
) -> None:
    image = tmp_path / "one.png"
    raw = _png(image, b"one")
    with pytest.raises(ProviderExecutorError, match="provider_job_invalid"):
        ProviderExecutor(
            opener=lambda *_args, **_kwargs: pytest.fail("provider must not be called")
        ).execute(_job([raw], **{field: value}), [image], _profile())


def test_execute_honors_prompt_first_and_rejects_non_boolean_order(
    tmp_path: Path,
) -> None:
    image = tmp_path / "one.png"
    raw = _png(image, b"one")
    captured = {}

    def opener(request, *, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse(_completed_lines())

    executor = ProviderExecutor(opener=opener)
    executor.execute(
        _job([raw], prompt_first=True),
        [image],
        _profile(),
    )

    contents = captured["body"]["input"][0]["content"]
    assert [item["type"] for item in contents] == ["input_text", "input_image"]
    assert contents[0]["text"] == "只按图片转录并判分。"

    with pytest.raises(ProviderExecutorError, match="provider_job_invalid"):
        executor.execute(
            _job([raw], prompt_first="true"),
            [image],
            _profile(),
        )


@pytest.mark.parametrize(
    ("lines", "code"),
    [
        ([b"data:[DONE]\n\n"], "provider_completed_missing"),
        ([_event({"type": "response.output_text.delta", "delta": "x"})], "provider_completed_missing"),
        ([b"data:{bad json}\n\n"], "provider_sse_invalid"),
        ([_event({"type": "response.failed"})], "provider_response_failed"),
        ([_event({"type": "response.incomplete"})], "provider_response_incomplete"),
        ([_event({"type": "error", "error": {"message": "secret"}})], "provider_stream_error"),
    ],
)
def test_sse_rejects_incomplete_or_invalid_streams(
    tmp_path: Path,
    lines,
    code: str,
) -> None:
    image = tmp_path / "one.png"
    raw = _png(image, b"one")
    executor = ProviderExecutor(opener=lambda *_args, **_kwargs: FakeResponse(lines))

    with pytest.raises(ProviderExecutorError) as captured:
        executor.execute(_job([raw]), [image], _profile())

    assert captured.value.code == code
    assert str(captured.value) == code
    assert "secret" not in str(captured.value)


def test_sse_body_is_bounded_to_512_kib(tmp_path: Path) -> None:
    image = tmp_path / "one.png"
    raw = _png(image, b"one")
    oversized = [b":" + b"x" * MAX_RESPONSE_BYTES + b"\n"]
    executor = ProviderExecutor(
        opener=lambda *_args, **_kwargs: FakeResponse(oversized)
    )

    with pytest.raises(ProviderExecutorError) as captured:
        executor.execute(_job([raw]), [image], _profile())

    assert captured.value.code == "provider_response_too_large"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "provider_auth_failed"),
        (403, "provider_auth_failed"),
        (429, "provider_rate_limited"),
        (504, "provider_timeout"),
        (500, "provider_unavailable"),
        (400, "provider_request_rejected"),
    ],
)
def test_http_failures_map_to_short_non_sensitive_codes(
    tmp_path: Path,
    status: int,
    code: str,
) -> None:
    image = tmp_path / "one.png"
    raw = _png(image, b"one")

    def opener(request, **_kwargs):
        raise HTTPError(
            request.full_url + "?key=secret",
            status,
            "secret upstream message",
            {},
            None,
        )

    with pytest.raises(ProviderExecutorError) as captured:
        ProviderExecutor(opener=opener).execute(_job([raw]), [image], _profile())

    assert captured.value.code == code
    assert str(captured.value) == code
    assert "secret" not in str(captured.value)


def test_transport_error_does_not_echo_url_or_key(tmp_path: Path) -> None:
    image = tmp_path / "one.png"
    raw = _png(image, b"one")

    def opener(*_args, **_kwargs):
        raise URLError("https://provider.example/local-secret-key")

    with pytest.raises(ProviderExecutorError) as captured:
        ProviderExecutor(opener=opener).execute(_job([raw]), [image], _profile())

    assert str(captured.value) == "provider_transport_failed"


def test_image_count_order_size_hash_and_mime_are_fail_closed(tmp_path: Path) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first_raw = _png(first, b"first")
    second_raw = _png(second, b"second")
    executor = ProviderExecutor(
        opener=lambda *_args, **_kwargs: pytest.fail("provider must not be called")
    )

    cases = [
        (_job([]), []),
        (_job([first_raw, second_raw]), [second, first]),
        (
            _job([first_raw], assets=[{**_asset(first_raw, 1), "byte_size": len(first_raw) + 1}]),
            [first],
        ),
        (
            _job([first_raw], assets=[{**_asset(first_raw, 1), "sha256": "b" * 64}]),
            [first],
        ),
        (
            _job([first_raw], assets=[{**_asset(first_raw, 1), "media_type": "image/jpeg"}]),
            [first],
        ),
    ]
    extra_paths = []
    for index in range(4):
        path = tmp_path / f"extra-{index}.png"
        extra_paths.append(path)
    extra_raw = [_png(path, str(index).encode()) for index, path in enumerate(extra_paths)]
    cases.append((_job(extra_raw), extra_paths))

    for job, paths in cases:
        with pytest.raises(ProviderExecutorError) as captured:
            executor.execute(job, paths, _profile())
        assert captured.value.code in {
            "provider_assets_invalid",
            "provider_asset_identity_mismatch",
        }


def test_byte_budget_is_thread_safe_and_wakes_waiters() -> None:
    budget = ThreadSafeByteBudget(100)
    first_acquired = threading.Event()
    release_first = threading.Event()
    second_acquired = threading.Event()

    def first_worker():
        with budget.reserve(80):
            first_acquired.set()
            assert release_first.wait(2)

    def second_worker():
        assert first_acquired.wait(2)
        with budget.reserve(30, timeout_s=2):
            second_acquired.set()

    first = threading.Thread(target=first_worker)
    second = threading.Thread(target=second_worker)
    first.start()
    second.start()
    assert first_acquired.wait(2)
    time.sleep(0.05)
    assert not second_acquired.is_set()
    assert budget.used == 80
    release_first.set()
    first.join(2)
    second.join(2)
    assert second_acquired.is_set()
    assert budget.used == 0


def test_byte_budget_rejects_oversized_and_times_out() -> None:
    budget = ThreadSafeByteBudget(100)
    assert estimated_payload_bytes(3) == 15
    with pytest.raises(ProviderExecutorError, match="provider_payload_memory_limit"):
        budget.acquire(101)
    budget.acquire(80)
    try:
        with pytest.raises(ProviderExecutorError, match="provider_payload_memory_busy"):
            budget.acquire(30, timeout_s=0.01)
    finally:
        budget.release(80)
