"""Offline contracts for the explicitly selected Flash grading route."""
import asyncio
import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import config
from services import analyzer, flash_grading as flash, ocr_service as ocr
from services.key_pool import KeyPool
from routers import answer_sheet_availability
from routers import answer_sheet_assets
from routers import answer_sheet_student_upload
from services import ocr_service_template_route


_PRODUCTION_POST = ocr._post_vision_request


def _content(result="correct", question_no="1"):
    return json.dumps({"results": {question_no: {"result": result, "explanation": "", "hint": "", "model_answer": "", "standard_answer": ""}}})


def _event(content=None, *, finish=None, model=flash.FLASH_MODEL, reasoning=None):
    delta = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    return {"model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _response(events=None, *, status=200, headers=None, done=True):
    if events is None:
        events = [_event(_content(), finish="stop")]
    text = "\n\n".join("data: " + json.dumps(event) for event in events)
    if done:
        text += "\n\ndata: [DONE]\n\n"
    return httpx.Response(status, text=text, headers=headers or {}, request=httpx.Request("POST", "https://unit.invalid/v1/chat/completions"))


class Limiter:
    async def is_credential_invalid(self, **_kw):
        return False

    async def credential_cooldown_seconds(self, **_kw):
        return 0

    async def mark_credential_invalid(self, **_kw):
        return None

    async def mark_credential_cooldown(self, **_kw):
        return None


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(ocr, "_vision_pool", KeyPool(["test-only-flash-key"], max_concurrent_per_key=None))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-flash-key")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://unit.invalid/v1")
    monkeypatch.setattr(ocr, "get_model_limiter", lambda: Limiter())
    monkeypatch.setattr(flash, "_credential_failures", {})
    monkeypatch.setattr(flash, "_semaphore", None)
    monkeypatch.setattr(flash, "_semaphore_loop", None)

    async def forbidden_post(*_args, **_kwargs):
        raise AssertionError("A model request was not mocked")

    monkeypatch.setattr(ocr, "_post_vision_request", forbidden_post)


def test_sse_model_identity_stop_usage_and_reasoning_separation():
    events = [
        _event(reasoning="INTERNAL_REASONING_NOT_RETURNED"),
        _event('{"results":'), _event("[]}", finish="stop"),
        {"model": flash.FLASH_MODEL, "choices": [], "usage": {
            "prompt_tokens": 11, "completion_tokens": 12, "total_tokens": 23,
            "completion_tokens_details": {"reasoning_tokens": 4},
            "credential": "must-not-return",
        }},
    ]
    result = flash.parse_flash_response(_response(events))
    assert result == {
        "content": '{"results":[]}', "model": flash.FLASH_MODEL,
        "usage": {"prompt_tokens": 11, "completion_tokens": 12, "total_tokens": 23,
                  "completion_tokens_details": {"reasoning_tokens": 4}},
    }
    assert "INTERNAL_REASONING" not in str(result)


@pytest.mark.parametrize("events,done", [
    ([_event(_content(), model="qwen3.7-plus", finish="stop")], True),
    ([_event(_content(), model=None, finish="stop")], True),
    ([_event(_content())], True),
    ([_event(_content(), finish="length")], True),
    ([_event(_content(), finish="stop")], False),
    ([_event(_content(), finish="stop"), {"error": {"message": "secret-response"}}], True),
    ([_event(_content(), finish="stop"), _event("extra")], True),
    ([_event(_content(), finish="stop"), _event(finish="stop")], True),
    ([_event("```json\n{}\n```\nanalysis", finish="stop")], True),
    ([_event("analysis\n```json\n{}\n```", finish="stop")], True),
    ([_event("```json\n{}\n```\n```json\n{}\n```", finish="stop")], True),
    ([_event("```json\n{} {}\n```", finish="stop")], True),
    ([_event("analysis\n{}", finish="stop")], True),
    ([_event('{"x": NaN}', finish="stop")], True),
    ([_event('{"x":1,"x":2}', finish="stop")], True),
    ([_event("[]", finish="stop")], True),
    ([_event(None, reasoning="reasoning only", finish="stop")], True),
    ([{"model": flash.FLASH_MODEL, "choices": [{"index": 1, "delta": {"content": "{}"}, "finish_reason": "stop"}]}], True),
    ([{"model": flash.FLASH_MODEL, "choices": [{"delta": {"tool_calls": [{}]}, "finish_reason": "stop"}]}], True),
])
def test_invalid_sse_is_fail_closed_without_raw_content(events, done):
    with pytest.raises(flash.FlashGradingError) as caught:
        flash.parse_flash_response(_response(events, done=done))
    assert "secret-response" not in str(caught.value)


def test_complete_nonstream_json_is_accepted_but_model_and_finish_required():
    body = {"model": flash.FLASH_MODEL, "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
    assert flash.parse_flash_response(httpx.Response(200, json=body))["content"] == "{}"
    body["choices"][0]["finish_reason"] = None
    with pytest.raises(flash.FlashGradingError):
        flash.parse_flash_response(httpx.Response(200, json=body))


@pytest.mark.parametrize("content", ["```json\n{}\n```", "```\n{}\n```", " \n```json\r\n{}\r\n```\n "])
def test_single_complete_json_fence_is_unwrapped(content):
    parsed = flash.parse_flash_response(_response([_event(content, finish="stop")]))
    assert parsed["content"] == "{}"
    assert parsed["model"] == flash.FLASH_MODEL


@pytest.mark.parametrize("phase,messages", [
    ("unknown", [{"role": "user", "content": "x"}]),
    ("text", [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,eA=="}}]}]),
    ("text", [{"role": "user", "content": [{"type": "audio", "data": "x"}]}]),
    ("text", [{"role": "assistant", "content": "x"}]),
    ("drawing", [{"role": "user", "content": "x"}]),
    ("drawing", [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://remote.invalid/student.png"}}]}]),
])
def test_message_phase_boundaries_rejected_before_transport(phase, messages):
    with pytest.raises(flash.FlashGradingError):
        asyncio.run(flash.call_flash(messages, phase=phase))


def test_actual_wire_uses_explicit_flash_parameters_same_route_and_teacher(monkeypatch):
    calls = []
    messages = [{"role": "user", "content": "{}"}]
    original = copy.deepcopy(messages)

    async def post(url, headers, payload, api_key):
        calls.append((url, headers, copy.deepcopy(payload), api_key, ocr._vision_teacher_id.get()))
        return _response()

    monkeypatch.setattr(ocr, "_post_vision_request", post)
    result = asyncio.run(flash.call_flash(messages, phase="text", teacher_id="teacher_25"))
    url, headers, body, key, teacher = calls[0]
    assert len(calls) == 1
    assert url == "https://unit.invalid/v1/chat/completions"
    assert headers["Authorization"] == f"Bearer {key}"
    assert teacher == "teacher_25"
    assert {k: v for k, v in body.items() if k != "messages"} == {
        "model": "qwen3.8-flash", "enable_thinking": True, "reasoning_effort": "medium",
        "temperature": 0, "stream": True, "max_tokens": 8000,
    }
    assert body["messages"] == original == messages
    assert result["parameters"] == flash.FLASH_PARAMETERS
    assert result["model"] == "qwen3.8-flash"
    assert result["receipt"]["upstream_attempts"] == 1
    assert result["receipt"]["request_sha256"] == hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert key not in json.dumps(result)
    assert "https://unit.invalid" not in json.dumps(result)
    assert ocr._vision_teacher_id.get() == "system"
    assert ocr._vision_pool.status()[0]["in_flight"] == 0


def test_template_hierarchy_uses_payg_low_while_grading_keeps_medium(monkeypatch):
    calls = []
    def forbidden_subscription(*args, **kwargs):
        raise AssertionError("Flash hierarchy must use the payg route")
    monkeypatch.setattr(
        ocr_service_template_route,
        "_get_template_ocr_scheduler_for_route",
        forbidden_subscription,
    )

    async def post(url, headers, payload, api_key):
        calls.append((url, payload["reasoning_effort"]))
        return _response()

    monkeypatch.setattr(ocr, "_post_vision_request", post)
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "层级"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
    ]}]
    result = asyncio.run(flash.call_flash(messages, phase="template_hierarchy", reasoning_effort="low", max_tokens=8000))
    asyncio.run(flash.call_flash([{"role": "user", "content": "{}"}], phase="text"))
    assert calls == [("https://unit.invalid/v1/chat/completions", "low"),
                     ("https://unit.invalid/v1/chat/completions", "medium")]
    assert result["parameters"]["reasoning_effort"] == "low"
    assert result["parameters"]["max_tokens"] == 8000


def test_report_phase_returns_plain_text_with_the_grading_parameters(monkeypatch):
    calls = []

    async def post(url, headers, payload, api_key):
        calls.append((copy.deepcopy(payload), ocr._vision_teacher_id.get()))
        return _response([_event("  【总体情况】函数需要复习。\n", finish="stop")])

    monkeypatch.setattr(ocr, "_post_vision_request", post)
    result = asyncio.run(flash.call_flash(
        [{"role": "user", "content": "批改统计"}], phase="report", teacher_id="teacher_3",
    ))
    body, teacher = calls[0]
    assert result["content"] == "【总体情况】函数需要复习。"
    assert teacher == "teacher_3"
    assert {k: v for k, v in body.items() if k != "messages"} == {
        "model": "qwen3.8-flash", "enable_thinking": True, "reasoning_effort": "medium",
        "temperature": 0, "stream": True, "max_tokens": 8000,
    }
    # Every other phase still requires one JSON object.
    with pytest.raises(flash.FlashGradingError):
        flash.parse_flash_response(_response([_event("【总体情况】", finish="stop")]))


def test_report_phase_rejects_images_and_blank_text():
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,eA=="}}
    with pytest.raises(flash.FlashGradingError):
        asyncio.run(flash.call_flash([{"role": "user", "content": [image]}], phase="report"))
    with pytest.raises(flash.FlashGradingError):
        flash.parse_flash_response(_response([_event("  \n", finish="stop")]), plain_text=True)


def test_report_text_is_one_report_phase_request(monkeypatch):
    calls = []

    async def fake_call_flash(messages, **kwargs):
        calls.append((messages, kwargs))
        return {"content": "报告正文"}

    monkeypatch.setattr(flash, "call_flash", fake_call_flash)
    messages = [{"role": "user", "content": "统计"}]
    assert asyncio.run(analyzer.generate_report_text(messages, teacher_id="teacher_9")) == "报告正文"
    assert calls == [(messages, {"teacher_id": "teacher_9", "phase": "report"})]


def test_missing_payg_does_not_fall_back_to_subscription(monkeypatch):
    monkeypatch.setattr(ocr, "_vision_pool", None)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "")
    messages = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
    ]}]
    with pytest.raises(flash.FlashGradingError, match="credentials"):
        asyncio.run(flash.call_flash(messages, phase="template_hierarchy"))


def test_single_key_compatibility_and_async_response_validator(monkeypatch):
    monkeypatch.setattr(ocr, "_vision_pool", None)
    seen = []

    async def post(_url, _headers, _payload, api_key):
        assert api_key == "test-only-flash-key"
        return _response()

    async def validate(content):
        seen.append(content)

    monkeypatch.setattr(ocr, "_post_vision_request", post)
    asyncio.run(flash.call_flash([{"role": "user", "content": "{}"}], phase="text", response_validator=validate))
    assert seen == [_content()]


def test_shared_account_gate_receives_actual_flash_body(monkeypatch):
    # Restore only the production admission/post helper, keeping the HTTP
    # client fully mocked. No model URL can be reached by this test.
    budget, leases, payloads = [], [], []

    class Lease:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            return False

    class SharedLimiter(Limiter):
        async def acquire_rate_budget(self, **kwargs):
            budget.append(kwargs)
        async def acquire(self, **kwargs):
            leases.append(kwargs)
            return Lease()

    class Client:
        def __init__(self, **_kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            return False
        async def post(self, _url, *, headers, json):
            payloads.append(json)
            return _response()

    monkeypatch.setattr(ocr, "_post_vision_request", _PRODUCTION_POST)
    monkeypatch.setattr(ocr, "get_model_limiter", lambda: SharedLimiter())
    monkeypatch.setattr(ocr.httpx, "AsyncClient", Client)
    asyncio.run(flash.call_flash([{"role": "user", "content": "{}"}], phase="text", teacher_id="teacher_2"))
    # Each DashScope model owns its RPM/TPM window; the concurrency lease stays
    # one shared backboard for teacher fairness.
    assert budget[0]["provider"] == "dashscope:qwen3.8-flash"
    assert leases[0]["provider"] == "dashscope"
    assert budget[0]["reserved_tokens"] >= 8000
    assert budget[0]["timeout_seconds"] == config.MODEL_RATE_BUDGET_WAIT_SECONDS
    assert leases[0]["teacher_id"] == "teacher_2"
    assert payloads[0]["model"] == "qwen3.8-flash"
    assert payloads[0]["reasoning_effort"] == "medium"


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_http_failure_one_attempt_and_no_global_ocr_credential_poisoning(monkeypatch, status):
    calls = []

    async def post(*_args):
        calls.append(True)
        return _response(status=status, headers={"retry-after": "2"})

    monkeypatch.setattr(ocr, "_post_vision_request", post)
    with pytest.raises(flash.FlashGradingError, match=f"HTTP {status}"):
        asyncio.run(flash.call_flash([{"role": "user", "content": "{}"}], phase="text"))
    assert len(calls) == 1
    assert ocr._vision_pool.readiness()["enabled_keys"] == 1
    assert ocr._vision_pool.status()[0]["in_flight"] == 0
    if status != 500:
        assert flash.get_flash_readiness()["ready"] is False


def test_credential_echo_and_transport_exception_are_sanitized(monkeypatch, caplog):
    async def post(*_args):
        return _response([_event('{"echo":"test-only-flash-key"}', finish="stop")])

    monkeypatch.setattr(ocr, "_post_vision_request", post)
    with pytest.raises(flash.FlashGradingError) as caught:
        asyncio.run(flash.call_flash([{"role": "user", "content": "{}"}], phase="text"))
    assert "test-only-flash-key" not in str(caught.value)

    async def broken(*_args):
        raise RuntimeError("test-only-flash-key https://private-gateway.invalid")

    monkeypatch.setattr(ocr, "_post_vision_request", broken)
    with pytest.raises(flash.FlashGradingError) as caught:
        asyncio.run(flash.call_flash([{"role": "user", "content": "{}"}], phase="text"))
    assert "test-only-flash-key" not in str(caught.value) + caplog.text
    assert "private-gateway" not in str(caught.value) + caplog.text


def test_cancellation_releases_key_and_restores_teacher(monkeypatch):
    async def post(*_args):
        raise asyncio.CancelledError()

    monkeypatch.setattr(ocr, "_post_vision_request", post)

    async def exercise():
        token = ocr.set_vision_teacher_id("outer")
        try:
            with pytest.raises(asyncio.CancelledError):
                await flash.call_flash([{"role": "user", "content": "{}"}], phase="text", teacher_id="inner")
            assert ocr._vision_teacher_id.get() == "outer"
        finally:
            ocr.reset_vision_teacher_id(token)

    asyncio.run(exercise())
    assert ocr._vision_pool.status()[0]["in_flight"] == 0


def test_timeout_bounds_call_without_changing_thinking_or_retry(monkeypatch):
    calls = []

    async def post(*_args):
        calls.append(True)
        await asyncio.sleep(1)

    monkeypatch.setattr(ocr, "_post_vision_request", post)
    monkeypatch.setattr(flash, "FLASH_DEADLINE_SECONDS", 0.01)
    with pytest.raises(flash.FlashGradingError):
        asyncio.run(flash.call_flash([{"role": "user", "content": "{}"}], phase="text"))
    assert calls == [True]
    assert ocr._vision_pool.status()[0]["in_flight"] == 0


def test_text_and_drawing_share_bounded_concurrency(monkeypatch):
    active = maximum = 0

    async def post(*_args):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.005)
        active -= 1
        return _response()

    monkeypatch.setattr(ocr, "_post_vision_request", post)
    monkeypatch.setattr(flash, "FLASH_LOCAL_MAX_CONCURRENT", 2)

    async def exercise():
        text = [{"role": "user", "content": "{}"}]
        image = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,eA=="}}]}]
        await asyncio.gather(*(
            flash.call_flash(image if i % 2 else text, phase="drawing" if i % 2 else "text")
            for i in range(8)
        ))

    asyncio.run(exercise())
    assert maximum == 2
    assert active == 0


def test_text_prompt_scope_transcription_and_stage_are_separate():
    from services.teaching_context import make_teaching_context, use_teaching_context
    questions = [{
        "question_no": "3", "question_text": "高中是学生引用的原文",
        "standard_answer": "2", "student_answer": "[无法识别]",
        "solution_steps": ["教师确认步骤"], "answer_variants": ["2.0"],
        "scope_hint": "text_only",
    }]
    with use_teaching_context(make_teaching_context("primary")):
        messages = flash.build_text_messages(questions)
    assert "你是小学数学批改助手" in messages[0]["content"]
    assert "高中是学生引用的原文" in messages[0]["content"]
    assert "scope_hint=text_only时，只评文字与计算" in messages[0]["content"]
    assert "多个所求答案分别判断" in messages[0]["content"]
    assert "OCR容错" in messages[0]["content"]
    assert "系统数值归一" not in messages[0]["content"]
    assert "[无法识别]" not in json.dumps(json.loads(messages[0]["content"].split("教师确认资料：")[1]), ensure_ascii=False)
    assert json.loads(messages[1]["content"])["answers"][0]["student_answer"] == "[无法识别]"
    assert "教师确认步骤" in messages[0]["content"]
    assert "image_url" not in json.dumps(messages)


def test_text_judgment_preserves_contract_and_attaches_model_receipt(monkeypatch):
    async def post(*_args):
        return _response([_event(_content("wrong"), finish="stop")])

    monkeypatch.setattr(ocr, "_post_vision_request", post)
    result = asyncio.run(flash.judge_text_paper([{"question_no": "1", "student_answer": "3", "standard_answer": "2"}]))
    assert result["1"]["result"] == "wrong"
    assert result["1"]["grading_receipt"]["model"] == "qwen3.8-flash"
    assert result["1"]["grading_receipt"]["parameters"]["reasoning_effort"] == "xhigh"


@pytest.mark.parametrize("response_content", [
    '{"results":[]}', '{"results":[{"question_no":"2","result":"correct"}]}',
    '{"results":[{"question_no":"1","result":"correct"},{"question_no":"1","result":"correct"}]}',
    '{"results":[{"question_no":"1","result":"garbage"}]}',
])
def test_text_bad_schema_is_uncacheable_review_not_zero(monkeypatch, response_content):
    async def post(*_args):
        return _response([_event(response_content, finish="stop")])

    monkeypatch.setattr(ocr, "_post_vision_request", post)
    result = asyncio.run(flash.judge_text_paper([{"question_no": "1", "student_answer": "2"}]))
    assert result["1"]["result"] == "needs_review"
    assert getattr(result, "__task_queue_cacheable__", True) is False
    assert "awarded_score" not in result["1"]


def test_flash_routes_whole_and_single_text_with_the_teacher_context(monkeypatch):
    calls = []

    async def post(*_args):
        calls.append(ocr._vision_teacher_id.get())
        return _response()

    monkeypatch.setattr(ocr, "_post_vision_request", post)
    async def exercise():
        token = analyzer.set_grading_teacher_id("teacher_17")
        try:
            whole = await analyzer.judge_paper([{"question_no": "1", "student_answer": "2"}])
            single = await analyzer.judge_answer("1+1", "2", standard_answer="2")
            return whole, single
        finally:
            analyzer.reset_grading_teacher_id(token)

    whole, single = asyncio.run(exercise())
    assert whole["1"]["result"] == single["result"] == "correct"
    assert calls == ["teacher_17", "teacher_17"]


def test_flash_readiness_is_model_scoped_without_mutating_ocr_pool(monkeypatch):
    seen = []

    class Unavailable(Limiter):
        async def is_credential_invalid(self, **kw):
            seen.append(kw["model"])
            return True

    monkeypatch.setattr(config, "GRADING_PROVIDER", "qwen3.8-flash")
    assert analyzer.get_grading_readiness()["ready"] is True
    result = asyncio.run(analyzer.get_grading_readiness_async(Unavailable()))
    assert result["ready"] is False
    assert result["model"] == flash.FLASH_MODEL
    assert seen == [flash.FLASH_MODEL]
    assert ocr._vision_pool.readiness()["enabled_keys"] == 1


def _import_config(tmp_path, grading_provider=None) -> subprocess.CompletedProcess:
    """Import ``config`` in a fresh interpreter with GRADING_PROVIDER set or unset."""
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.pop("GRADING_PROVIDER", None)
    if grading_provider is not None:
        env["GRADING_PROVIDER"] = grading_provider
    env["PYTHON_DOTENV_DISABLED"] = "1"
    env.setdefault("JWT_SECRET", "test-only-jwt-secret-with-at-least-32-characters")
    env["PYTHONPATH"] = str(project_root)
    return subprocess.run(
        [sys.executable, "-c", "import config; print(config.GRADING_PROVIDER)"],
        cwd=tmp_path, env=env, text=True, capture_output=True,
    )


@pytest.mark.parametrize("raw", [None, "  QWEN3.8-Flash  "])
def test_grading_provider_defaults_to_flash(tmp_path, raw):
    completed = _import_config(tmp_path, raw)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "qwen3.8-flash"


@pytest.mark.parametrize("retired", ["mimo", "gpt-sol"])
def test_retired_grading_providers_stop_startup(tmp_path, retired):
    completed = _import_config(tmp_path, retired)
    assert completed.returncode != 0
    assert f"GRADING_PROVIDER={retired} 已退役" in completed.stderr


class _NonRedisLimiter:
    pass


def _answer_sheet_gate(monkeypatch, *, redis: bool, sync_ready: bool, async_ready: bool):
    """Run the answer-sheet readiness gate with every other dependency ready."""
    from routers import answer_sheet
    from services import model_concurrency, ocr_teacher_fairness

    calls = []

    class _RedisLimiter(model_concurrency.RedisModelLimiter):
        def __init__(self):
            pass

        async def ping(self):
            calls.append(("ping",))

    limiter = _RedisLimiter() if redis else _NonRedisLimiter()

    async def storage(*_args, **_kwargs):
        return {"ready": True}

    async def ocr_ready():
        return {"configured": True, "ready": True}

    def sync_readiness():
        calls.append(("sync",))
        return {"configured": True, "ready": sync_ready}

    async def async_readiness(used_limiter=None):
        calls.append(("async", used_limiter))
        return {"configured": True, "ready": async_ready}

    async def capacity(_teacher_id):
        return {"status": "available"}

    monkeypatch.setattr(config, "ANSWER_SHEET_GRADING_ENABLED", True)
    monkeypatch.setattr(answer_sheet_student_upload, "get_upload_storage_readiness_async", storage)
    monkeypatch.setattr(answer_sheet_availability, "get_upload_storage_readiness_async", storage)
    monkeypatch.setattr(answer_sheet_assets, "get_upload_storage_readiness_async", storage)
    monkeypatch.setattr(ocr, "get_ocr_readiness", ocr_ready)
    monkeypatch.setattr(model_concurrency, "get_model_limiter", lambda: limiter)
    monkeypatch.setattr(analyzer, "get_grading_readiness", sync_readiness)
    monkeypatch.setattr(analyzer, "get_grading_readiness_async", async_readiness)
    monkeypatch.setattr(ocr_teacher_fairness, "public_teacher_availability", capacity)
    result = asyncio.run(answer_sheet._answer_sheet_service_availability(SimpleNamespace(id=1)))
    return result, calls, limiter


@pytest.mark.parametrize("sync_ready, expected", [(True, "available"), (False, "unavailable")])
def test_answer_sheet_gate_uses_the_generic_grading_readiness(monkeypatch, sync_ready, expected):
    result, calls, _limiter = _answer_sheet_gate(
        monkeypatch, redis=False, sync_ready=sync_ready, async_ready=not sync_ready,
    )
    assert result["status"] == expected
    assert calls == [("sync",)], "without the shared limiter only the local readiness decides"
    if not sync_ready:
        assert result["message"] == "数学批改服务尚未就绪，请稍后重试或联系管理员。"


@pytest.mark.parametrize("sync_ready, async_ready, expected", [
    (True, False, "unavailable"),
    (False, True, "available"),
])
def test_answer_sheet_gate_asks_the_shared_limiter_when_redis_backs_it(
    monkeypatch, sync_ready, async_ready, expected,
):
    result, calls, limiter = _answer_sheet_gate(
        monkeypatch, redis=True, sync_ready=sync_ready, async_ready=async_ready,
    )
    assert result["status"] == expected
    assert calls == [("ping",), ("sync",), ("async", limiter)]
