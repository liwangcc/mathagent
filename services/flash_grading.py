"""Separate text/drawing grading and template hierarchy on Qwen Flash; OCR remains transcription-only.

Credentials and account quotas are shared with the existing DashScope route.
The model, reasoning parameters, response parser and grading contract are not
borrowed from the OCR caller. One invocation makes at most one paid request.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
import os
import re
import time
from urllib.parse import urlsplit

from services.retry_backoff import parse_retry_after_seconds
from services.processing_errors import current_processing_stage, describe_failure, diagnosed, processing_stage, record_attempt
from services.text_grading_rules import (
    TEXT_GRADING_DETAIL_INSTRUCTIONS,
    TEXT_GRADING_RESULTS,
    TEXT_GRADING_RULES,
    TEXT_GRADING_RULES_REVISION,
    text_grading_output_example,
)


logger = logging.getLogger(__name__)
FLASH_MODEL = "qwen3.8-flash"
FLASH_PARAMETERS = {
    "model": FLASH_MODEL,
    "enable_thinking": True,
    "reasoning_effort": "medium",
    "temperature": 0,
    "stream": True,
    "max_tokens": 8000,
}
FLASH_TEXT_PROMPT_VERSION = f"flash-text-v{TEXT_GRADING_RULES_REVISION}"
# Only text judgment uses the validated higher effort; other Flash roles keep
# their existing defaults/overrides. Keep v8 prompt bytes and Schema unchanged.
FLASH_TEXT_REASONING_EFFORT = "xhigh"
FLASH_DEADLINE_SECONDS = 300.0
# Report text is plain prose; every other phase returns one strict JSON object.
_PHASES = {"text", "report", "drawing", "template_hierarchy"}
_PLAIN_TEXT_PHASES = {"report"}


def _configured_flash_local_max_concurrent() -> int:
    """Return the per-worker Flash grading concurrency window (at least 1).

    The window bounds how many Flash grading calls one worker keeps in flight
    (request body plus streamed response).  Production selects it through
    ``FLASH_LOCAL_MAX_CONCURRENT`` may explicitly lower it. By default use
    the shared Flash window, not the retired four-request development cap.
    """
    from config import DASHSCOPE_GLOBAL_CONCURRENCY

    try:
        value = int(os.environ.get("FLASH_LOCAL_MAX_CONCURRENT", str(DASHSCOPE_GLOBAL_CONCURRENCY)))
    except (TypeError, ValueError):
        return DASHSCOPE_GLOBAL_CONCURRENCY
    return min(value, DASHSCOPE_GLOBAL_CONCURRENCY) if value >= 1 else DASHSCOPE_GLOBAL_CONCURRENCY


FLASH_LOCAL_MAX_CONCURRENT = _configured_flash_local_max_concurrent()
_MAX_REQUEST_BYTES = 32 * 1024 * 1024
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_semaphore: asyncio.Semaphore | None = None
_semaphore_loop = None
# Model-scoped failures must not disable the shared OCR credential pool.
_credential_failures: dict[str, tuple[bool, float]] = {}


class FlashGradingError(RuntimeError):
    """A sanitized failure; callers must not turn it into an automatic zero."""


def _ocr():
    # Lazy import prevents analyzer -> Flash -> OCR -> analyzer import cycles.
    from services import ocr_service
    return ocr_service


def _credential_id(api_key: str, base_url: str) -> str:
    return hashlib.sha256(
        (FLASH_MODEL + "\0" + base_url.rstrip("/") + "\0" + api_key).encode()
    ).hexdigest()


def _local_failure(api_key: str, base_url: str) -> tuple[bool, float]:
    invalid, until = _credential_failures.get(_credential_id(api_key, base_url), (False, 0.0))
    return invalid, max(0.0, until - time.monotonic())


def _route():
    ocr = _ocr()
    base_url = os.environ.get("DASHSCOPE_BASE_URL", ocr._DEFAULT_BASE_URL).strip()
    # 设置页「学生卷批阅」组的凭据优先于环境变量（组级 → 环境变量 → 默认值）。
    # One key per responsibility: the group key, else DASHSCOPE_API_KEY, is the only route.
    # The legacy DASHSCOPE_API_KEYS pool below is reached only when both are empty; it is
    # failover, not capacity (same-vendor keys share one per-IP rate limit).
    profile_key, profile_base = ocr._profile_credentials("student_grade")
    if profile_key:
        resolved_base = (profile_base or base_url).strip()
        return ocr, None, resolved_base, [(profile_key, resolved_base)]
    pool = ocr._vision_pool
    if pool is not None:
        routes = [(key, override or base_url) for key, override in pool.enabled_credentials()]
    else:
        key = os.environ.get("DASHSCOPE_API_KEY", ocr._DEFAULT_API_KEY).strip()
        routes = [(key, base_url)] if key else []
    return ocr, pool, base_url, routes


def get_flash_readiness() -> dict:
    """Return local route readiness without a model request or secret output."""
    _service, pool, _base, routes = _route()
    state = pool.readiness() if pool is not None else {
        "configured": bool(routes), "ready": bool(routes),
        "configured_keys": len(routes), "enabled_keys": len(routes),
        "immediately_available_keys": len(routes),
    }
    usable = [route for route in routes if route[1] and not any(_local_failure(*route))]
    ready = bool(usable and state.get("ready"))
    return {
        **state, "provider": FLASH_MODEL, "model": FLASH_MODEL,
        "quota_provider": "dashscope", "ready": ready,
        "immediately_available_keys": min(int(state.get("immediately_available_keys", 0)), len(usable)),
        "reason": "" if ready else (
            "credentials_not_configured" if not state.get("configured")
            else "endpoint_not_configured" if routes and not any(base for _, base in routes)
            else "credentials_unavailable"
        ),
    }


async def _shared_failure(limiter, api_key: str, base_url: str) -> bool:
    identity = dict(provider="dashscope", api_key=api_key, base_url=base_url, model=FLASH_MODEL)
    invalid = getattr(limiter, "is_credential_invalid", None)
    cooldown = getattr(limiter, "credential_cooldown_seconds", None)
    if callable(invalid) and await invalid(**identity):
        return True
    if callable(cooldown):
        seconds = float(await cooldown(**identity))
        if not math.isfinite(seconds) or seconds > 0:
            return True
    return False


async def get_flash_readiness_async(limiter=None) -> dict:
    ocr, _pool, _base, routes = _route()
    state = get_flash_readiness()
    if not state["ready"]:
        return state
    limiter = limiter or ocr.get_model_limiter()
    healthy = sum([
        bool(base_url) and not any(_local_failure(api_key, base_url))
        and not await _shared_failure(limiter, api_key, base_url)
        for api_key, base_url in routes
    ])
    if not healthy:
        state.update(ready=False, immediately_available_keys=0, reason="credentials_unavailable")
    return state


def _local_semaphore() -> asyncio.Semaphore:
    global _semaphore, _semaphore_loop
    loop = asyncio.get_running_loop()
    if _semaphore is None or _semaphore_loop is not loop:
        _semaphore = asyncio.Semaphore(FLASH_LOCAL_MAX_CONCURRENT)
        _semaphore_loop = loop
    return _semaphore


def _reject_constant(_value):
    raise ValueError("non-finite JSON number")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _strict_json(raw: str):
    return json.loads(raw, parse_constant=_reject_constant, object_pairs_hook=_unique_object)


def _final_json_content(content: str, *, output_adapter=None) -> str:
    """Permit a single complete JSON fence, never prose or extra objects."""
    stripped = content.strip()
    if stripped.startswith("```"):
        fenced = re.fullmatch(r"```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```", stripped)
        if fenced is None:
            raise ValueError("invalid JSON fence")
        stripped = fenced.group(1).strip()
    parsed = _strict_json(stripped)
    if output_adapter is not None:
        normalized = output_adapter(parsed)
        if normalized != parsed:
            stripped = json.dumps(normalized, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        parsed = normalized
    if not isinstance(parsed, dict):
        raise ValueError("content must be one strict JSON object")
    return stripped


def _safe_usage(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
            result[key] = item
    details = value.get("completion_tokens_details")
    if isinstance(details, dict):
        count = details.get("reasoning_tokens")
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            result["completion_tokens_details"] = {"reasoning_tokens": count}
    return result


def parse_flash_response(response, *, output_adapter=None, raw_capture=None, plain_text=False) -> dict:
    """Validate model identity and stream completion; never return reasoning."""
    raw = response.text
    if not isinstance(raw, str) or not raw.strip() or len(raw.encode()) > _MAX_RESPONSE_BYTES:
        raise FlashGradingError("Flash response is empty or too large")
    stripped = raw.lstrip()
    streaming = not stripped.startswith("{")
    events = []
    parts = []
    done = False
    try:
        if not streaming:
            events = [_strict_json(stripped)]
        else:
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    if line.startswith(("event:", "id:", "retry:")):
                        continue
                    raise ValueError("invalid SSE framing")
                data = line[5:].strip()
                if data == "[DONE]":
                    if done:
                        raise ValueError("duplicate stream terminator")
                    done = True
                    continue
                if done:
                    raise ValueError("data after stream terminator")
                events.append(_strict_json(data))
        parts, models, usage = [], set(), {}
        stopped = False
        for event in events:
            if not isinstance(event, dict) or "error" in event:
                raise ValueError("provider error or non-object event")
            model = event.get("model")
            if model is not None:
                if model != FLASH_MODEL:
                    raise ValueError("unexpected model")
                models.add(model)
            choices = event.get("choices", [])
            if not isinstance(choices, list) or len(choices) > 1:
                raise ValueError("expected one choice")
            if choices and model != FLASH_MODEL:
                raise ValueError("choice missing model identity")
            for choice in choices:
                if not isinstance(choice, dict):
                    raise ValueError("unexpected choice")
                index = choice.get("index", 0)
                if not isinstance(index, int) or isinstance(index, bool) or index != 0:
                    raise ValueError("unexpected choice")
                source = choice.get("delta" if streaming else "message")
                if not isinstance(source, dict) or source.get("tool_calls") or source.get("function_call"):
                    raise ValueError("expected assistant text")
                content = source.get("content")
                if content is not None and not isinstance(content, str):
                    raise ValueError("non-text content")
                if content:
                    if stopped:
                        raise ValueError("content after finish")
                    parts.append(content)
                finish = choice.get("finish_reason")
                if finish is not None:
                    if finish != "stop" or stopped:
                        raise ValueError("incomplete or duplicate finish")
                    stopped = True
            if "usage" in event:
                usage = _safe_usage(event["usage"])
        content = "".join(parts)
        if raw_capture is not None:
            # Preserve the provider's exact final text before any deterministic
            # envelope adaptation so a later contract rejection can be studied.
            raw_capture["content"] = content
        if models != {FLASH_MODEL} or not stopped or (streaming and not done):
            raise ValueError("missing model or stream completion")
        if plain_text:
            content = content.strip()
            if not content:
                raise ValueError("empty content")
        else:
            content = _final_json_content(content, output_adapter=output_adapter)
    except (TypeError, ValueError, KeyError) as exc:
        error = FlashGradingError("Flash returned an invalid model/JSON/stream contract")
        # Private in-memory evidence only. Public diagnostics never serialize it.
        error._model_output_text = "".join(parts)
        error._output_error_type = type(exc).__name__
        error._output_error_message = str(exc)
        raise error from None
    return {"content": content, "model": FLASH_MODEL, "usage": usage}


def _payload(
    messages: list[dict],
    phase: str,
    *,
    enable_thinking: bool | None = None,
    reasoning_effort: str | None = None,
    max_tokens: int | None = None,
    json_object: bool = False,
    response_schema: dict | None = None,
) -> dict:
    if response_schema is not None and (phase != "text" or json_object or not isinstance(response_schema, dict)):
        raise FlashGradingError("Strict schema requires the text grading phase")
    if phase not in _PHASES:
        raise FlashGradingError("Flash phase must be text, report, drawing, or template_hierarchy")
    if not isinstance(json_object, bool):
        raise FlashGradingError("Flash json_object must be a boolean")
    if not isinstance(messages, list) or not messages or len(messages) > 32:
        raise FlashGradingError("Flash requires bounded messages")
    images = 0
    for message in messages:
        if not isinstance(message, dict) or set(message) != {"role", "content"}:
            raise FlashGradingError("Invalid Flash message")
        if message["role"] not in {"system", "user"}:
            raise FlashGradingError("Invalid Flash role")
        content = message["content"]
        if isinstance(content, str):
            continue
        if not isinstance(content, list) or not content:
            raise FlashGradingError("Invalid Flash content")
        for part in content:
            if not isinstance(part, dict):
                raise FlashGradingError("Invalid Flash content block")
            if part.get("type") == "text" and set(part) == {"type", "text"} and isinstance(part["text"], str):
                continue
            if phase not in {"drawing", "template_hierarchy"} or part.get("type") != "image_url" or set(part) != {"type", "image_url"}:
                raise FlashGradingError("Text grading cannot receive images or unknown blocks")
            image = part["image_url"]
            if not isinstance(image, dict) or not set(image) <= {"url", "detail"}:
                raise FlashGradingError("Invalid drawing image")
            url = image.get("url")
            if not isinstance(url, str) or not url.startswith(("data:image/png;base64,", "data:image/jpeg;base64,", "data:image/webp;base64,")):
                raise FlashGradingError("Flash image phases require inline bound images")
            images += 1
    if (phase in {"drawing", "template_hierarchy"} and not 1 <= images <= 12) or (phase == "text" and images):
        raise FlashGradingError("Invalid image count for grading phase")
    try:
        encoded = json.dumps(messages, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode()) > _MAX_REQUEST_BYTES:
            raise ValueError("oversized request")
        frozen_messages = _strict_json(encoded)
    except (TypeError, ValueError):
        raise FlashGradingError("Invalid or oversized Flash request") from None
    parameters = dict(FLASH_PARAMETERS)
    if enable_thinking is not None:
        parameters["enable_thinking"] = bool(enable_thinking)
    if reasoning_effort is not None:
        parameters["reasoning_effort"] = reasoning_effort
    elif enable_thinking is False:
        # DashScope rejects a reasoning level when thinking is explicitly off.
        parameters.pop("reasoning_effort", None)
    if max_tokens is not None:
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
            raise FlashGradingError("Flash max_tokens must be a positive integer")
        parameters["max_tokens"] = max_tokens
    if json_object:
        # Opt-in native JSON mode for the template phases. Qwen documents
        # json_object as best effort while thinking is enabled, so the caller's
        # schema validator remains the only acceptance gate. This flag alone
        # does not repair/retry text; envelope adaptation is separately opt-in.
        parameters["response_format"] = {"type": "json_object"}
    if response_schema is not None:
        parameters["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "text_grading", "strict": True,
                "schema": _strict_json(json.dumps(response_schema, ensure_ascii=False, allow_nan=False)),
            },
        }
    return {**parameters, "messages": frozen_messages}


async def _record_failure(limiter, api_key: str, base_url: str, status: int, headers) -> None:
    identity = dict(provider="dashscope", api_key=api_key, base_url=base_url, model=FLASH_MODEL)
    if status in {401, 403}:
        _credential_failures[_credential_id(api_key, base_url)] = (True, 0.0)
        marker = getattr(limiter, "mark_credential_invalid", None)
        if callable(marker):
            await marker(**identity, status_code=status)
    elif status == 429:
        seconds = parse_retry_after_seconds(headers.get("retry-after")) or 30.0
        _credential_failures[_credential_id(api_key, base_url)] = (False, time.monotonic() + seconds)
        marker = getattr(limiter, "mark_credential_cooldown", None)
        if callable(marker):
            await marker(**identity, cooldown_seconds=seconds)


@diagnosed("model")
async def call_flash(
    messages: list[dict], *, teacher_id=None, phase: str, response_validator=None,
    enable_thinking: bool | None = None, reasoning_effort: str | None = None,
    max_tokens: int | None = None,
    json_object: bool = False,
    response_schema: dict | None = None,
    output_adapter=None, diagnostic_phase: str | None = None,
) -> dict:
    """Send a single bounded Flash request and return a secret-free receipt.

    The ``report`` phase returns plain text; every other phase must answer
    with one strict JSON object.

    ``response_validator`` receives only final JSON text, never reasoning.
    Text and drawing callers share the same local/admission limits. The shared
    DashScope account gate sees the actual Flash model in the request body.
    """
    if output_adapter is not None and (phase != "template_hierarchy" or not callable(output_adapter) or response_validator is None):
        raise FlashGradingError("Output adaptation requires a validated template image contract")
    payload = _payload(
        messages,
        phase,
        enable_thinking=enable_thinking,
        reasoning_effort=reasoning_effort,
        max_tokens=max_tokens,
        json_object=json_object,
        response_schema=response_schema,
    )
    ocr, pool, default_base, routes = _route()
    if not routes:
        raise FlashGradingError("Flash grading credentials are not configured")
    release_key = None
    token = None
    started = time.monotonic()
    result = None
    raw_capture = {}
    try:
        async with asyncio.timeout(FLASH_DEADLINE_SECONDS):
            async with _local_semaphore():
                limiter = ocr.get_model_limiter()
                # Skip already-known Flash-only failures without spending an
                # upstream attempt or disabling this credential for OCR.
                api_key = base_url = ""
                for _ in range(len(routes)):
                    if pool is not None:
                        candidate, release_key, override = await ocr.acquire_key(pool)
                        candidate_base = override or default_base
                    else:
                        candidate, candidate_base = routes[0]
                    if not any(_local_failure(candidate, candidate_base)) and not await _shared_failure(limiter, candidate, candidate_base):
                        api_key, base_url = candidate, candidate_base
                        break
                    if pool is not None and release_key is not None:
                        pool.release(release_key)
                        release_key = None
                if not api_key:
                    raise FlashGradingError("Flash grading credentials are unavailable")
                parsed_url = urlsplit(base_url)
                if parsed_url.scheme not in {"https", "http"} or not parsed_url.hostname or parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
                    raise FlashGradingError("Flash grading endpoint is invalid")
                if teacher_id is None:
                    from services.analyzer import _grading_teacher_id
                    teacher_id = _grading_teacher_id.get()
                    if teacher_id == "system":
                        teacher_id = ocr._vision_teacher_id.get()
                token = ocr.set_vision_teacher_id(str(teacher_id or "system"))
                url = base_url.rstrip("/") + "/chat/completions"
                response = await ocr._post_vision_request(
                    url, {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    payload, api_key,
                )
                status = response.status_code
                if status != 200:
                    await _record_failure(limiter, api_key, base_url, status, response.headers)
                    error = FlashGradingError(f"Flash grading HTTP {status}")
                    error.status_code = status
                    raise error
                # A misconfigured gateway must not echo a credential into an
                # assessment, error message, saved result or request receipt.
                if api_key and api_key in response.text:
                    raise FlashGradingError("Flash response contains protected credential data")
                with processing_stage(current_processing_stage("grading"), model=FLASH_MODEL,
                                      route="dashscope", phase=diagnostic_phase or phase):
                    result = parse_flash_response(
                        response, output_adapter=output_adapter, raw_capture=raw_capture,
                        plain_text=phase in _PLAIN_TEXT_PHASES,
                    )
                    if response_validator is not None:
                        validated = response_validator(result["content"])
                        if inspect.isawaitable(validated):
                            await validated
                request_sha = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                return {
                    **result, "parameters": {key: value for key, value in payload.items() if key != "messages"},
                    "receipt": {
                        "phase": diagnostic_phase or phase, "transport_phase": phase, "request_sha256": request_sha,
                        "endpoint_sha256": hashlib.sha256(url.encode()).hexdigest(),
                        "content_sha256": hashlib.sha256(result["content"].encode()).hexdigest(),
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "upstream_attempts": 1,
                    },
                }
    except asyncio.CancelledError:
        raise
    except FlashGradingError as exc:
        describe_failure(exc, stage=current_processing_stage("grading"), model=FLASH_MODEL, route="dashscope", phase=diagnostic_phase or phase,
                         duration_ms=(time.monotonic() - started) * 1000, attempt=1, retry_count=0)
        raise
    except Exception as exc:
        if result is not None or raw_capture.get("content"):
            exc._model_output_text = raw_capture.get("content") or result["content"]
            exc._output_error_type = type(exc).__name__
            exc._output_error_message = str(exc)
        describe_failure(exc, stage=current_processing_stage("grading"), model=FLASH_MODEL, route="dashscope", phase=diagnostic_phase or phase,
                         duration_ms=(time.monotonic() - started) * 1000, attempt=1, retry_count=0)
        raise FlashGradingError("Flash grading request or response validation failed") from exc
    finally:
        if token is not None:
            ocr.reset_vision_teacher_id(token)
        if pool is not None and release_key is not None:
            pool.release(release_key)


_TEXT_RULES = TEXT_GRADING_RULES


def build_text_response_schema(question_numbers: list[str], detail_level: str = "detailed") -> dict:
    """Require exactly the frozen judgment identities, including sparse slots."""
    if detail_level not in {"simple", "detailed"}:
        raise FlashGradingError("Invalid grading detail level")
    if (
        not question_numbers
        or any(not isinstance(qn, str) or not qn or qn != qn.strip() for qn in question_numbers)
        or len(set(question_numbers)) != len(question_numbers)
    ):
        raise FlashGradingError("Missing or duplicate text question number")
    fields = {"result": {"type": "string", "enum": list(TEXT_GRADING_RESULTS)}}
    if detail_level == "detailed":
        fields.update({name: {"type": "string"} for name in ("explanation", "hint", "model_answer", "standard_answer")})
    item = {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}
    return {
        "type": "object",
        "properties": {"results": {
            "type": "object", "properties": {qn: item for qn in question_numbers},
            "required": list(question_numbers), "additionalProperties": False,
        }},
        "required": ["results"], "additionalProperties": False,
    }


def parse_text_judgment(content: str, question_numbers: list[str], detail_level: str = "detailed") -> dict[str, dict]:
    """Adapt exact object keys to the unchanged whole-paper scoring validator."""
    from services.analyzer import StructuredOutputError, _validate_paper_judgment

    schema = build_text_response_schema(question_numbers, detail_level)
    parsed = _strict_json(content)
    if not isinstance(parsed, dict) or set(parsed) != {"results"} or not isinstance(parsed["results"], dict):
        raise StructuredOutputError("results must be a question-keyed object")
    results = parsed["results"]
    if set(results) != set(question_numbers):
        missing = sorted(set(question_numbers) - set(results))
        extra = sorted(set(results) - set(question_numbers))
        raise StructuredOutputError(f"question identity mismatch: missing={missing}, extra={extra}")
    expected_fields = set(schema["properties"]["results"]["properties"][question_numbers[0]]["required"])
    rows = []
    for qn in question_numbers:
        item = results[qn]
        if not isinstance(item, dict) or set(item) != expected_fields:
            raise StructuredOutputError(f"invalid judgment fields for question_no={qn}")
        rows.append({"question_no": qn, **item})
    return _validate_paper_judgment(json.dumps({"results": rows}, ensure_ascii=False, allow_nan=False), question_numbers)


def build_text_messages(
    questions: list[dict], detail_level: str = "detailed", *,
    teacher_material: list[dict] | None = None,
) -> list[dict]:
    from services.teaching_context import stage_prompt
    if detail_level not in {"detailed", "simple"}:
        raise FlashGradingError("Invalid grading detail level")
    if not isinstance(questions, list) or not questions:
        raise FlashGradingError("Text grading requires questions")
    refs, answers, qnos = [], [], set()
    for question in questions:
        if not isinstance(question, dict):
            raise FlashGradingError("Invalid text question")
        qn = str(question.get("question_no", "")).strip()
        if not qn or qn in qnos:
            raise FlashGradingError("Missing or duplicate text question number")
        qnos.add(qn)
        if any(key in question for key in ("image_url", "images", "image", "image_path", "drawing_evidence")):
            raise FlashGradingError("Text grading cannot receive image inputs")
        item = {"question_no": qn}
        for name in ("question_text", "standard_answer", "grading_method", "scope_hint"):
            value = question.get(name)
            if value is not None and not isinstance(value, str):
                raise FlashGradingError("Text rubric fields must be strings")
            if value is not None:
                item[name] = value
        for name in ("answer_variants", "solution_steps"):
            values = question.get(name) or []
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise FlashGradingError("Text rubric steps must be text")
            if values:
                item[name] = values
        answer = question.get("student_answer") or ""
        if not isinstance(answer, str):
            raise FlashGradingError("Student transcription must be text")
        refs.append(item)
        answers.append({"question_no": qn, "student_answer": answer})
    if teacher_material is not None:
        from services.text_grading_context import validate_teacher_material
        try:
            refs = validate_teacher_material(teacher_material, questions)
        except ValueError as exc:
            raise FlashGradingError(str(exc)) from exc
    output = text_grading_output_example(detail_level)
    instructions = "本次只判对错，不生成解析。"
    if detail_level == "detailed":
        instructions = TEXT_GRADING_DETAIL_INSTRUCTIONS
    system = stage_prompt(_TEXT_RULES) + "\n" + instructions
    if teacher_material is not None:
        from services.text_grading_context import TEXT_REFERENCE_INSTRUCTIONS
        system += "\n" + TEXT_REFERENCE_INSTRUCTIONS
    system += "\n仅返回JSON对象，results的键与学生answers中的question_no完全一致，不缺、不重、不增加；result只允许correct、partial、wrong。"
    system += "\n输出结构：" + json.dumps({"results": {"输入题号": output}}, ensure_ascii=False, separators=(",", ":"))
    system += "\n教师确认资料：" + json.dumps(refs, ensure_ascii=False, separators=(",", ":"))
    return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps({"answers": answers}, ensure_ascii=False, separators=(",", ":"))}]


async def judge_text_paper(
    questions: list[dict], detail_level: str = "detailed", *, teacher_id=None,
    teacher_material: list[dict] | None = None,
) -> dict[str, dict]:
    """Keep the existing text judgment mapping and uncacheable failure marker."""
    from services.analyzer import _fallback_paper_result
    if not questions:
        return {}
    qnos = [str(q.get("question_no", "")) for q in questions if isinstance(q, dict)]
    try:
        messages = build_text_messages(questions, detail_level, teacher_material=teacher_material)
        response = await call_flash(
            messages, teacher_id=teacher_id, phase="text",
            reasoning_effort=FLASH_TEXT_REASONING_EFFORT,
            response_schema=build_text_response_schema(qnos, detail_level),
            response_validator=lambda content: parse_text_judgment(content, qnos, detail_level),
        )
        judgments = parse_text_judgment(response["content"], qnos, detail_level)
        for judged in judgments.values():
            judged["grading_receipt"] = {
                **response["receipt"], "model": response["model"],
                "parameters": response["parameters"], "usage": response["usage"],
                "prompt_version": FLASH_TEXT_PROMPT_VERSION,
            }
        return judgments
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        info = record_attempt(exc, stage="grading", model=FLASH_MODEL, route="dashscope")
        result = _fallback_paper_result(qnos, info["message"])
        result.processing_error = exc
        return result
