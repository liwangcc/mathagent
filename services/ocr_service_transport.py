"""The pieces of a DashScope vision request: credentials, the legacy key pool, image encoding, response parsing.

- ``_profile_credentials`` reads a settings-page duty group's (api_key,
  base_url); ``_create_dashscope_pool`` builds the legacy fallback ``KeyPool``
  from ``DASHSCOPE_API_KEYS`` / ``DASHSCOPE_API_KEY`` (``None`` without keys).
- ``_encode_vision_image`` reads and encodes one image;
  ``_parse_streamed_vision_response`` turns the SSE stream (or a single JSON
  object) into the chat response shape.
- ``_cap_student_ocr_max_tokens`` keeps every student-OCR output limit positive
  and at most ``STUDENT_OCR_MAX_OUTPUT_TOKENS``.

``_post_vision_request`` and the pool actually in use (``_vision_pool``) stay in
services/ocr_service.py: ``_sync_pool_from_profiles`` rebinds the pool there, and
services/flash_grading.py reads both, and ``get_model_limiter``, from there.

Moved unchanged from services/ocr_service.py, which still exposes these names.
"""

import base64
import json
import logging
import os

import httpx

from services.key_pool import KeyPool, _parse_keys, _parse_weights

# services.ocr_service's logger, so these records keep their logger name.
logger = logging.getLogger("services.ocr_service")


# Preserve the validated student-transcription application output budget.
# Callers may request less; changing models must not expand the payload or
# pretend that the retired Plus provider ceiling still governs Flash.
STUDENT_OCR_MAX_OUTPUT_TOKENS = 9000


def _cap_student_ocr_max_tokens(max_tokens: int) -> int:
    """Return a positive student-OCR output limit no greater than 9000.

    The public helpers historically accepted an integer and passed it through
    unchanged.  Retain that API while making the provider ceiling an invariant
    at every request boundary (including callers that compute a limit from the
    number of questions).
    """
    if isinstance(max_tokens, bool):
        raise ValueError("max_tokens must be a positive integer")
    try:
        normalized = int(max_tokens)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("max_tokens must be a positive integer") from exc
    if normalized < 1:
        raise ValueError("max_tokens must be a positive integer")
    return min(normalized, STUDENT_OCR_MAX_OUTPUT_TOKENS)

# 号池（模块加载时初始化，无多Key配置时为None走单Key模式）
def _create_dashscope_pool():
    """创建DashScope视觉模型KeyPool。无配置时返回None（降级为单Key模式）。

    Legacy fallback only: used when neither the student_transcribe settings group nor
    DASHSCOPE_API_KEY yields a key. Same-vendor keys share one per-IP rate limit, so
    listing several in DASHSCOPE_API_KEYS gives failover, not capacity.
    """
    keys = _parse_keys("DASHSCOPE_API_KEYS", "DASHSCOPE_API_KEY")
    if not keys:
        return None
    weights = _parse_weights("DASHSCOPE_API_KEY_WEIGHTS", len(keys))
    # Credentials are routing/failover choices. Provider and teacher capacity
    # is enforced by the shared model limiter, never by API-key count.
    pool = KeyPool(keys, max_concurrent_per_key=None, weights=weights)
    logger.info(
        "DashScope KeyPool initialized with %d credential(s); "
        "provider/teacher capacity is coordinated by Redis",
        len(keys),
    )
    return pool


def _profile_credentials(group: str) -> tuple[str, str]:
    """设置页「职责组」配置的 (api_key, base_url)。解析失败一律回退空值。"""
    try:
        from services.model_profiles import credentials

        return credentials(group)
    except Exception:
        return "", ""


def _parse_streamed_vision_response(response: httpx.Response) -> dict:
    """Normalize a DashScope streaming response to the chat response shape.

    Qwen's streaming endpoint returns SSE chunks while the legacy test doubles
    and a few compatible gateways still return one JSON object.  Accept both
    forms, but keep reasoning content visible to the caller so it can be
    rejected by the existing fail-closed guard.
    """
    try:
        raw = response.text
    except (AttributeError, RuntimeError):
        raw = ""
    if not raw:
        try:
            value = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError("empty vision response") from exc
        if not isinstance(value, dict):
            raise ValueError("vision response must be an object")
        return value

    stripped = raw.lstrip()
    if stripped.startswith("{"):
        value = json.loads(stripped)
        if not isinstance(value, dict):
            raise ValueError("vision response must be an object")
        return value

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    usage: dict = {}
    response_model = ""
    finish_reason = "stop"
    saw_event = False
    for line in raw.splitlines():
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        event = json.loads(data)
        if not isinstance(event, dict):
            continue
        saw_event = True
        if isinstance(event.get("model"), str) and event["model"].strip():
            response_model = event["model"].strip()
        if isinstance(event.get("usage"), dict):
            usage = event["usage"]
        for choice in event.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            finish_reason = str(choice.get("finish_reason") or finish_reason)
            delta = choice.get("delta") or {}
            message = choice.get("message") or {}
            for source in (delta, message):
                if not isinstance(source, dict):
                    continue
                content = source.get("content")
                if isinstance(content, str):
                    content_parts.append(content)
                reasoning = source.get("reasoning_content")
                if isinstance(reasoning, str):
                    reasoning_parts.append(reasoning)
    if not saw_event:
        raise ValueError("streamed vision response contained no SSE events")
    message = {"content": "".join(content_parts)}
    reasoning_content = "".join(reasoning_parts)
    if reasoning_content:
        message["reasoning_content"] = reasoning_content
    return {
        "choices": [{"finish_reason": finish_reason, "message": message}],
        "usage": usage,
        **({"model": response_model} if response_model else {}),
    }


def _encode_vision_image(image_path: str) -> tuple[str, str]:
    """Read and encode one image off the event loop."""
    with open(image_path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode()
    ext = os.path.splitext(image_path)[1].lower().lstrip(".")
    mime_map = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
    }
    return encoded, mime_map.get(ext, "image/png")
