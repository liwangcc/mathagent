import asyncio
import json
import os
import subprocess
import sys

import httpx
import pytest

from services import analyzer
from services.key_pool import KeyPool
from services import ocr_service_template_route


def _max_route_config(**overrides):
    child_env = os.environ.copy()
    child_env["PYTHON_DOTENV_DISABLED"] = "1"
    child_env["OCR_TEMPLATE_GPT_FALLBACK_BASE_URL"] = "https://qwen.example.invalid/v1"
    child_env["OCR_TEMPLATE_GPT_FALLBACK_API_KEY"] = "test-only-key"
    child_env["OCR_TEMPLATE_GPT_FALLBACK_MODEL"] = "qwen3.8max"
    child_env.pop("OCR_TEMPLATE_GPT_FALLBACK_HARD_IN_FLIGHT", None)
    # config errors are Chinese; never decode them with the Windows locale codec.
    child_env["PYTHONUTF8"] = "1"
    child_env.update(overrides)
    return subprocess.run(
        [sys.executable, "-c", "import config; print(config.OCR_TEMPLATE_QWEN_FALLBACK_MAX_RPM, "
         "config.OCR_TEMPLATE_GPT_FALLBACK_HARD_IN_FLIGHT)"],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        env=child_env,
        capture_output=True,
        encoding="utf-8",
    )


def test_max_token_plan_has_no_rate_gate_even_with_legacy_rpm_knobs():
    """Token Plan has no RPM/TPM: leftover GPT/Qwen RPM settings must not pace it."""
    result = _max_route_config(
        OCR_TEMPLATE_GPT_FALLBACK_MAX_RPM="30",
        OCR_TEMPLATE_QWEN_FALLBACK_MAX_RPM="100",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0.0 4"


def test_max_token_plan_in_flight_is_bounded_by_tier_concurrency():
    result = _max_route_config(OCR_TEMPLATE_GPT_FALLBACK_HARD_IN_FLIGHT="9")
    assert result.returncode != 0
    assert "OCR_TEMPLATE_GPT_FALLBACK_HARD_IN_FLIGHT" in result.stderr


def test_streamed_chat_content_ignores_reasoning_and_joins_deltas():
    body = (
        'data: {"choices":[{"delta":{"reasoning_content":"先计算"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"{\\"result\\": "}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"\\"correct\\"}"}}]}\n\n'
        'data: [DONE]\n\n'
    )

    assert analyzer._parse_streamed_chat_content(body) == '{"result": "correct"}'


def test_call_api_merges_provider_specific_body(monkeypatch):
    sent = {}

    class _Response:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    async def fake_post_model_request(**kwargs):
        sent.update(kwargs["payload"])
        return _Response()

    monkeypatch.setattr(analyzer, "_post_model_request", fake_post_model_request)

    result = asyncio.run(
        analyzer._call_api(
            pool=None,
            messages=[{"role": "user", "content": "x"}],
            model="mimo-v4-pro",
            single_key_env="TEST_MIMO_KEY",
            single_base_env="TEST_MIMO_URL",
            single_key_default="test-key",
            single_base_default="https://example.invalid/v1",
            temperature=0.1,
            extra_body={"thinking": {"type": "disabled"}},
        )
    )

    assert result == "ok"
    assert sent["temperature"] == 0.1
    assert sent["thinking"] == {"type": "disabled"}


def test_qwen_ocr_uses_deterministic_non_thinking_parameters(monkeypatch, tmp_path):
    from services import ocr_service

    captured = {}

    class _Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{
                "finish_reason": "stop",
                "message": {"content": '{"ok": true}'},
            }]}

    async def fake_post_vision_request(url, headers, payload, api_key):
        captured.update(payload)
        return _Response()

    image_path = tmp_path / "paper.jpg"
    image_path.write_bytes(b"jpeg-probe")
    monkeypatch.setenv("DASHSCOPE_MODEL", "qwen3.7-plus")
    monkeypatch.setattr(ocr_service, "_vision_pool", None)
    monkeypatch.setattr(ocr_service, "_post_vision_request", fake_post_vision_request)

    result = asyncio.run(
        ocr_service._call_vision_request(
            str(image_path), "return json", max_tokens=8192
        )
    )

    assert result == '{"ok": true}'
    assert captured["model"] == "qwen3.8-flash"
    assert captured["enable_thinking"] is False
    assert captured["reasoning_effort"] == "none"
    assert captured["temperature"] == 0
    assert captured["max_completion_tokens"] == 8192
    assert captured["stream"] is True


def test_qwen_stream_response_is_joined_without_reasoning(monkeypatch, tmp_path):
    from services import ocr_service

    captured = {}

    class _Response:
        status_code = 200
        text = (
            'data: {"choices":[{"delta":{"reasoning_content":"先看图"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"{\\"ok\\": "}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"true}"}}]}\n\n'
            'data: [DONE]\n\n'
        )

        def raise_for_status(self):
            return None

    async def fake_post_vision_request(url, headers, payload, api_key):
        captured.update(payload)
        return _Response()

    image_path = tmp_path / "paper.jpg"
    image_path.write_bytes(b"jpeg-probe")
    monkeypatch.setattr(ocr_service, "_vision_pool", None)
    monkeypatch.setattr(ocr_service, "_post_vision_request", fake_post_vision_request)
    monkeypatch.setattr(ocr_service.asyncio, "sleep", lambda _delay: None)

    # The first streamed response carries reasoning and must be rejected.  This
    # fixture only checks the parser shape; the existing retry test covers the
    # fail-closed retry path with a JSON response.
    result = ocr_service._parse_streamed_vision_response(_Response())
    assert result["choices"][0]["message"]["content"] == '{"ok": true}'
    assert result["choices"][0]["message"]["reasoning_content"] == "先看图"


def test_student_vision_caps_requested_output_tokens_at_provider_limit(
    monkeypatch, tmp_path
):
    from services import ocr_service

    image_path = tmp_path / "paper.jpg"
    image_path.write_bytes(b"jpeg-probe")
    captured = {}

    async def fake_call_vision_request(_image_path, _prompt, **kwargs):
        captured.update(kwargs)
        return '{"questions":[]}'

    monkeypatch.setattr(ocr_service, "_call_vision_request", fake_call_vision_request)

    result = asyncio.run(
        ocr_service.call_vision(
            str(image_path), "return json", max_tokens=100_000
        )
    )

    assert result == '{"questions":[]}'
    assert captured["max_tokens"] == 9000


def test_low_level_student_vision_caps_provider_payload_tokens(
    monkeypatch, tmp_path
):
    """The provider-facing Qwen request must enforce the ceiling itself."""
    from services import ocr_service

    captured = {}

    class _Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{
                "finish_reason": "stop",
                "message": {"content": '{"ok": true}'},
            }]}

    async def fake_post_vision_request(_url, _headers, payload, _api_key):
        captured.update(payload)
        return _Response()

    image_path = tmp_path / "paper.jpg"
    image_path.write_bytes(b"jpeg-probe")
    monkeypatch.setattr(ocr_service, "_vision_pool", None)
    monkeypatch.setattr(
        ocr_service, "_post_vision_request", fake_post_vision_request
    )

    result = asyncio.run(
        ocr_service._call_vision_request(
            str(image_path), "return json", max_tokens=100_000
        )
    )

    assert result == '{"ok": true}'
    assert captured["max_completion_tokens"] == 9000


def test_qwen_ocr_ignores_retired_model_alias(monkeypatch, tmp_path):
    from services import ocr_service

    captured = {}

    class _Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{
                "finish_reason": "stop",
                "message": {"content": '{"ok": true}'},
            }]}

    async def fake_post_vision_request(url, headers, payload, api_key):
        captured.update(payload)
        return _Response()

    image_path = tmp_path / "paper.jpg"
    image_path.write_bytes(b"jpeg-probe")
    monkeypatch.setenv("DASHSCOPE_MODEL", "qwen3.7-plus-2026-05-26")
    monkeypatch.setattr(ocr_service, "_vision_pool", None)
    monkeypatch.setattr(ocr_service, "_post_vision_request", fake_post_vision_request)

    result = asyncio.run(
        ocr_service._call_vision_request(
            str(image_path), "return json", max_tokens=2048
        )
    )

    assert result == '{"ok": true}'
    assert captured["model"] == "qwen3.8-flash"


def test_qwen_ocr_rejects_reported_reasoning_even_when_content_is_present(monkeypatch, tmp_path):
    from services import ocr_service

    payloads = []

    class _Response:
        status_code = 200

        def __init__(self, message):
            self._message = message

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"finish_reason": "stop", "message": self._message}]}

    responses = [
        _Response({"content": '{"unsafe": true}', "reasoning_content": "solving"}),
        _Response({"content": '{"safe": true}'}),
    ]

    async def fake_post_vision_request(url, headers, payload, api_key):
        payloads.append(dict(payload))
        return responses.pop(0)

    async def no_wait(_delay):
        return None

    image_path = tmp_path / "paper.jpg"
    image_path.write_bytes(b"jpeg-probe")
    monkeypatch.setattr(ocr_service, "_vision_pool", None)
    monkeypatch.setattr(ocr_service, "_post_vision_request", fake_post_vision_request)
    monkeypatch.setattr(ocr_service.asyncio, "sleep", no_wait)

    result = asyncio.run(
        ocr_service._call_vision_request(str(image_path), "return json")
    )

    assert result == '{"safe": true}'
    assert len(payloads) == 2
    assert all(payload["enable_thinking"] is False for payload in payloads)


def test_legacy_qwen_unauthorized_key_switches_pool_key(monkeypatch, tmp_path):
    from services import ocr_service

    pool = KeyPool(["bad-key", "good-key"], weights=[100, 1])
    seen = []

    async def fake_post(url, _headers, _payload, api_key):
        seen.append(api_key)
        request = httpx.Request("POST", url)
        if api_key == "bad-key":
            return httpx.Response(401, request=request, text="invalid token")
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{
                "finish_reason": "stop",
                "message": {"content": '{"ok": true}'},
            }]},
        )

    image_path = tmp_path / "paper.jpg"
    image_path.write_bytes(b"jpeg-probe")
    monkeypatch.setattr(ocr_service, "_vision_pool", pool)
    monkeypatch.setattr(ocr_service, "_post_vision_request", fake_post)

    result = asyncio.run(
        ocr_service._call_vision_request(str(image_path), "return json")
    )

    assert result == '{"ok": true}'
    assert seen == ["bad-key", "good-key"]
    assert pool.status()[0]["invalid_status_code"] == 401


def test_legacy_qwen_retries_generic_transport_error(monkeypatch, tmp_path):
    from services import ocr_service

    calls = 0

    async def fake_post(url, _headers, _payload, _api_key):
        nonlocal calls
        calls += 1
        request = httpx.Request("POST", url)
        if calls == 1:
            raise httpx.ReadError("connection dropped", request=request)
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{
                "finish_reason": "stop",
                "message": {"content": '{"ok": true}'},
            }]},
        )

    async def no_wait(_delay):
        return None

    image_path = tmp_path / "paper.jpg"
    image_path.write_bytes(b"jpeg-probe")
    monkeypatch.setattr(ocr_service, "_vision_pool", None)
    monkeypatch.setattr(ocr_service, "_post_vision_request", fake_post)
    monkeypatch.setattr(ocr_service.asyncio, "sleep", no_wait)

    assert asyncio.run(
        ocr_service._call_vision_request(str(image_path), "return json")
    ) == '{"ok": true}'
    assert calls == 2


def test_qwen_retry_requests_a_fresh_dashscope_rate_budget_each_attempt(
    monkeypatch, tmp_path
):
    from services import ocr_service

    budget_calls = []
    http_calls = 0

    class _Lease:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class _Limiter:
        async def acquire_rate_budget(self, **kwargs):
            budget_calls.append(kwargs)

        async def acquire(self, **_kwargs):
            return _Lease()

    class _Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, headers, json):
            nonlocal http_calls
            http_calls += 1
            request = httpx.Request("POST", url, headers=headers)
            if http_calls == 1:
                return httpx.Response(500, request=request, text="temporary")
            return httpx.Response(
                200,
                request=request,
                json={
                    "choices": [{
                        "finish_reason": "stop",
                        "message": {"content": '{"ok": true}'},
                    }]
                },
            )

    image_path = tmp_path / "paper.jpg"
    image_path.write_bytes(b"jpeg-probe")
    monkeypatch.setattr(ocr_service, "_vision_pool", None)
    monkeypatch.setattr(ocr_service, "get_model_limiter", lambda: _Limiter())
    monkeypatch.setattr(ocr_service.httpx, "AsyncClient", _Client)

    async def no_wait(_delay):
        return None

    monkeypatch.setattr(ocr_service.asyncio, "sleep", no_wait)

    result = asyncio.run(
        ocr_service._call_vision_request(
            str(image_path), "return json", max_tokens=512, max_attempts=2
        )
    )

    assert result == '{"ok": true}'
    assert http_calls == 2
    assert len(budget_calls) == 2
    # Student OCR retries share the same Flash budget as template/grading calls.
    assert all(call["provider"] == "dashscope:qwen3.8-flash" for call in budget_calls)
    assert all(call["requests_per_minute"] == 30_000 for call in budget_calls)
    assert all(call["tokens_per_minute"] == 5_000_000 for call in budget_calls)
    assert all(call["reserved_tokens"] > 0 for call in budget_calls)


def test_ocr_readiness_does_not_send_model_request(monkeypatch):
    from services import ocr_service

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-qwen-key")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://qwen.example.invalid/v1")
    monkeypatch.setattr(
        ocr_service,
        "_post_vision_request",
        lambda *_args, **_kwargs: pytest.fail("readiness must not call a model"),
    )

    state = asyncio.run(ocr_service.get_ocr_readiness())

    assert state == {
        "provider": "ocr",
        "configured": True,
        "ready": True,
        "reason": "",
    }


def test_multi_pool_vision_uses_high_responses_and_anonymous_batch_scope(
    monkeypatch, tmp_path
):
    from services import ocr_service
    from services import ocr_pool_scheduler

    captured = {}

    class _Scheduler:
        async def request_template_fallback_pool(self, payload, **kwargs):
            captured["payload"] = payload
            captured.update(kwargs)
            content = json.dumps({
                "questions": [{
                    "question_number": "1",
                    "question_text": "x",
                    "student_answer": "",
                    "answer_state": "blank",
                    "question_type": "fill",
                }],
                "student_name": "",
                "student_no": "",
            })
            kwargs["validator"](content)
            return content

    batch_dir = tmp_path / "quick_batch_abc123"
    batch_dir.mkdir()
    image_path = batch_dir / "paper_preprocessed.jpg"
    image_path.write_bytes(b"jpeg-probe")
    monkeypatch.setattr(
        ocr_pool_scheduler, "get_template_ocr_pool_scheduler", lambda: _Scheduler()
    )

    result = asyncio.run(
        ocr_service.call_gpt_vision(
            str(image_path),
            "student_answer answer_state strict JSON",
            max_tokens=512,
            prompt_first=True,
        )
    )

    assert json.loads(result)["questions"][0]["answer_state"] == "blank"
    payload = captured["payload"]
    assert payload["stream"] is True
    assert payload["max_output_tokens"] == 512
    assert [item["type"] for item in payload["input"][0]["content"]] == [
        "input_text",
        "input_image",
    ]
    assert payload["input"][0]["content"][0] == {
        "type": "input_text",
        "text": "student_answer answer_state strict JSON",
    }
    assert "messages" not in payload
    assert "reasoning" not in payload
    assert payload["model_reasoning_effort"] == "low"
    assert "reasoning_effort" not in payload
    assert "temperature" not in payload
    assert "max_completion_tokens" not in payload
    assert "stream_options" not in payload
    assert captured["request_key"] == "quick_batch_abc123:paper"


def test_unique_preprocess_artifacts_keep_stable_scheduler_paper_key(tmp_path):
    from services import ocr_service

    batch_dir = tmp_path / "quick_batch_abc123"
    batch_dir.mkdir()
    first = batch_dir / ("paper_preprocessed-" + "a" * 32 + ".jpg")
    second = batch_dir / ("paper_preprocessed-" + "b" * 32 + ".jpg")

    first_route = ocr_service._multi_pool_route_keys(str(first))
    second_route = ocr_service._multi_pool_route_keys(str(second))

    assert first_route == second_route
    assert first_route[1] == "quick_batch_abc123:paper"


def test_multi_pool_transcription_validator_rejects_order_mismatch():
    from services import ocr_service

    validator = ocr_service._multi_pool_response_validator(
        'student_answer answer_state\nquestion_numbers=["1", "2"]\n'
    )
    content = json.dumps({
        "questions": [
            {"question_number": "2", "student_answer": "", "answer_state": "blank"},
            {"question_number": "1", "student_answer": "", "answer_state": "blank"},
        ]
    })

    with pytest.raises(ValueError, match="question order mismatch"):
        validator(content)


def test_explicit_template_fallback_route_uses_qwen_pool(monkeypatch, tmp_path):
    from services import ocr_pool_scheduler, ocr_service

    captured = {}

    class _Scheduler:
        async def request_template_fallback_pool(self, payload, **kwargs):
            captured["payload"] = payload
            captured.update(kwargs)
            return '{"question_anchors":[]}'

        async def request_retry_pool(self, *_args, **_kwargs):
            raise AssertionError("fallback route must not call YS")

    image_path = tmp_path / "template-fallback-evidence-test.png"
    image_path.write_bytes(b"crop-bytes")
    monkeypatch.setattr(
        ocr_pool_scheduler,
        "get_template_ocr_pool_scheduler",
        lambda: _Scheduler(),
    )
    monkeypatch.setattr(
        ocr_service_template_route,
        "_encode_vision_image",
        lambda _path: ("encoded", "image/png"),
    )
    monkeypatch.setattr(
        ocr_service,
        "_encode_vision_image",
        lambda _path: ("encoded", "image/png"),
    )

    result = asyncio.run(ocr_service._call_multi_pool_vision_request(
        str(image_path),
        "template fallback prompt",
        pool_route="fallback",
        reasoning_effort="high",
    ))

    assert result == '{"question_anchors":[]}'
    assert captured["request_key"].endswith(":template-fallback-evidence-test.png")
    assert captured["payload"]["model_reasoning_effort"] == "high"


def test_student_ocr_uses_qwen_even_when_template_gpt_pool_is_disabled(monkeypatch, tmp_path):
    from services import ocr_service

    image_path = tmp_path / "paper.jpg"
    image_path.write_bytes(b"jpeg-probe")

    async def qwen(*_args, **_kwargs):
        return '{"questions":[]}'

    monkeypatch.setattr(ocr_service, "_call_vision_request", qwen)

    assert asyncio.run(
        ocr_service.call_vision(str(image_path), "return json")
    ) == '{"questions":[]}'
