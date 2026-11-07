"""Vision API (Qwen/DashScope) 试卷识别 — 替代 PaddleOCR

The code lives in one module per part; each imports only the parts above it:

    ocr_service_context         the per-request vision context (teacher, route, phase, batch)
    ocr_service_local_image     local image work: bounded threads, stall handoff, memory budgets
    ocr_service_validation      student identity and transcription checks on a vision answer
    ocr_service_transport       credentials, the legacy key pool, image encoding, response parsing
    ocr_service_template_route  teacher-template requests on the qwen3.8-max route
    ocr_service_papers          preparing a paper image, classifying a failure, the cached path

This module keeps the student OCR route itself: the pool in use (``_vision_pool``,
rebound by ``_sync_pool_from_profiles``), ``_post_vision_request``,
``_call_vision_request``, ``call_vision``/``call_vision_images``/``read_image``,
``get_ocr_readiness`` and the ``recognize_*`` entry points.  They stay together
because ``_vision_pool`` is rebound at run time and services/flash_grading.py
reads it, ``_post_vision_request`` and ``get_model_limiter`` from this module.
It re-exports every name, so ``from services.ocr_service import ...`` keeps
working; new code may import from the module that defines a name.
"""

# Everything this module imported before the split stays importable from it.
import asyncio
import base64
import concurrent.futures
import hashlib
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager, nullcontext
from contextvars import ContextVar
from pathlib import Path
import httpx
import inspect
from config import DASHSCOPE_BASE_URL as _DEFAULT_BASE_URL
from config import DASHSCOPE_API_KEY as _DEFAULT_API_KEY
from config import DASHSCOPE_MODEL as _DEFAULT_MODEL
from config import (
    DASHSCOPE_GLOBAL_CONCURRENCY,
    MODEL_TEACHER_BASE_CONCURRENCY,
    OCR_LOCAL_MAX_CONCURRENT,
    OCR_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES,
    OCR_PREPROCESS_LOCAL_MAX_CONCURRENT,
    OCR_PREPROCESS_MAX_EDGE,
    OCR_PREPROCESS_MODE,
    OCR_TEMPLATE_GPT_MAX_OUTPUT_TOKENS,
    OCR_TRANSCRIPTION_MAX_OUTPUT_TOKENS,
    UPLOAD_DIR,
)
from services import dashscope_budget
from services.key_pool import KeyPool, acquire_key, _parse_keys, _parse_weights, get_ocr_teacher_semaphore
from services.identity_regions import IDENTITY_LEFT_CROP_RATIO, IDENTITY_TOP_CROP_RATIO
from services.model_concurrency import RateBudgetTimeout, get_model_limiter
from services.processing_errors import diagnosed, record_attempt, report_failure
from services.preprocess import (
    _validate_source_dimensions,
    is_ocr_ready_image,
    preprocess_paper_image,
)
from services.analyzer import parse_llm_json
# The per-request vision context (ocr_service_context.py).
from services.ocr_service_context import (  # noqa: F401
    _vision_teacher_id,
    _vision_route_ordinal,
    _vision_phase_key,
    _vision_retry_phase_key,
    _vision_batch_paper_count,
    _vision_batch_deadline_s,
    set_vision_teacher_id,
    reset_vision_teacher_id,
    set_vision_route_ordinal,
    reset_vision_route_ordinal,
    set_vision_phase_key,
    reset_vision_phase_key,
    set_vision_batch_paper_count,
    reset_vision_batch_paper_count,
    set_vision_batch_deadline_s,
    reset_vision_batch_deadline_s,
)
# Local image work and memory budgets (ocr_service_local_image.py).
from services.ocr_service_local_image import (  # noqa: F401
    _LOCAL_IMAGE_WORK_STALL_SECONDS,
    _LOCAL_IMAGE_COMPONENT_TIMEOUT_SECONDS,
    _LOCAL_OCR_PAYLOAD_BUDGET_WAIT_SECONDS,
    _LOCAL_IMAGE_STALL_HEARTBEAT_SECONDS,
    _LOCAL_IMAGE_STALL_MARKER_STALE_SECONDS,
    _LOCAL_IMAGE_STALL_MARKER_DIR,
    _local_image_work_started,
    _local_image_work_lock,
    _stalled_image_settlers,
    LocalImagePipelineStalled,
    _local_ocr_request_semaphore,
    _local_ocr_preprocess_semaphore,
    _local_identity_preprocess_semaphore,
    _local_ocr_payload_budget,
    _local_ocr_request_loop,
    _local_ocr_preprocess_loop,
    _local_identity_preprocess_loop,
    _local_ocr_payload_budget_loop,
    _AsyncByteBudget,
    _get_local_ocr_request_semaphore,
    _get_local_ocr_preprocess_semaphore,
    _get_local_identity_preprocess_semaphore,
    _get_local_ocr_payload_budget,
    _settle_local_image_thread,
    _run_local_image_thread,
    _fresh_shared_image_stall_markers,
    _handoff_stalled_image_thread,
    get_local_image_readiness,
    _run_local_ocr_image_component,
    _estimated_vision_payload_bytes,
    _estimated_vision_images_payload_bytes,
)
# Identity and transcription checks (ocr_service_validation.py).
from services.ocr_service_validation import (  # noqa: F401
    _NAME_PATTERN,
    _STUDENT_NO_PATTERN,
    _ANSWER_STATES,
    IDENTITY_ROSTER_AUTO_MATCH_CONFIDENCE,
    IDENTITY_ROSTER_MAX_CANDIDATES,
    _sanitize_student_name,
    _sanitize_student_no,
    validate_visual_transcriptions,
    _normalize_roster_candidate_names,
    _build_identity_crop,
)
# Credentials, the legacy pool, encoding and parsing (ocr_service_transport.py).
from services.ocr_service_transport import (  # noqa: F401
    STUDENT_OCR_MAX_OUTPUT_TOKENS,
    _cap_student_ocr_max_tokens,
    _create_dashscope_pool,
    _profile_credentials,
    _parse_streamed_vision_response,
    _encode_vision_image,
)
# The template qwen3.8-max route (ocr_service_template_route.py).
from services.ocr_service_template_route import (  # noqa: F401
    _multi_pool_route_keys_for_phase,
    _multi_pool_route_keys,
    _multi_pool_response_validator,
    _get_template_ocr_scheduler_for_route,
    _accepts_keyword,
    _set_template_route_diagnostics,
    _call_multi_pool_vision_request,
    call_gpt_vision,
    call_template_max_text,
)
# Paper image preparation and failures (ocr_service_papers.py).
from services.ocr_service_papers import (  # noqa: F401
    _prepare_vision_image,
    _cleanup_prepared_image,
    classify_ocr_exception,
    ocr_failure_result,
    recognize_paper_with_cache,
)

logger = logging.getLogger(__name__)


_vision_pool = _create_dashscope_pool()


_pool_profile_signature: tuple | None = None


def _sync_pool_from_profiles() -> None:
    """让学生卷转录组的密钥无需重启即可生效（组级优先于环境变量）。

    One key per responsibility: a settings-group key, else DASHSCOPE_API_KEY, becomes a
    single-credential pool. Only when both are empty does the legacy DASHSCOPE_API_KEYS
    pool from _create_dashscope_pool() apply (see services/model_profiles.py).

    解析结果带 20 秒缓存，这里只比较一个签名，命中时几乎零开销。
    """
    global _vision_pool, _pool_profile_signature
    key, base = _profile_credentials("student_transcribe")
    signature = ("profile", key, base) if key else ("env",)
    if signature == _pool_profile_signature:
        return
    _pool_profile_signature = signature
    if key:
        _vision_pool = KeyPool(
            [key],
            max_concurrent_per_key=None,
            base_urls=[base] if base else None,
        )
        logger.info("DashScope student route uses one credential (settings group or DASHSCOPE_API_KEY)")
    else:
        _vision_pool = _create_dashscope_pool()


_local_image_readiness_probe: asyncio.Task | None = None
_local_image_readiness_probe_loop: asyncio.AbstractEventLoop | None = None


# Readiness probes measure the local image pipeline, so they must never
# queue behind the image components they observe. asyncio.to_thread shares
# one default executor with _run_local_image_thread; a saturated OCR wave
# then starves this probe and the availability gate misreads busy as fault.
# Follow system_readiness: give probes their own lane and cache verdicts.
_readiness_probe_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="ocr-readiness",
)
_local_image_readiness_cache: dict | None = None
_local_image_readiness_cache_at = 0.0
_LOCAL_IMAGE_READINESS_CACHE_TTL_SECONDS = 5.0


MAX_RETRIES = 2  # one first attempt plus one automatic retry
RETRY_DELAY = 2.0  # base backoff for 429, doubles each retry


async def _post_vision_request(url: str, headers: dict, payload: dict, api_key: str):
    """Send one Vision request with independent quota and concurrency gates."""
    limiter = get_model_limiter()
    # Student OCR, template Flash and grading now consume the same model
    # window and shared concurrency backboard; API keys do not add capacity.
    rate_budget_reservation = await dashscope_budget.reserve(
        limiter, model=str(payload.get("model") or _DEFAULT_MODEL), payload=payload,
    )
    request_started = False
    try:
        lease = await limiter.acquire(
            provider="dashscope",
            api_key=api_key,
            teacher_id=_vision_teacher_id.get(),
            global_capacity=DASHSCOPE_GLOBAL_CONCURRENCY,
            credential_capacity=DASHSCOPE_GLOBAL_CONCURRENCY,
            teacher_base_capacity=MODEL_TEACHER_BASE_CONCURRENCY,
            # Legacy generic burst/reserve knobs must not idle the shared Flash
            # window. Competing teachers rotate at the per-request Redis gate.
            teacher_burst_capacity=DASHSCOPE_GLOBAL_CONCURRENCY,
            global_reserve_capacity=0,
        )
        async with lease:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10, read=120, write=30, pool=10)
            ) as client:
                request_started = True
                response = await client.post(url, headers=headers, json=payload)
    except BaseException as exc:
        # Lease timeout/cancellation and connection-phase failures never reach
        # the provider and are refunded. Once bytes may have been sent, the
        # reservation stays conservative until its rolling window expires.
        if dashscope_budget.request_was_not_sent(request_started, exc):
            await dashscope_budget.cancel(limiter, rate_budget_reservation)
        raise
    # Usage is billed whether or not the caller later accepts the content.
    await dashscope_budget.settle_response(limiter, rate_budget_reservation, response)
    return response


@diagnosed("transcription")
async def _call_vision_request(
    image_path: str | list[str],
    prompt: str,
    max_tokens: int = 9000,
    *,
    response_validator=None,
    prompt_first: bool = False,
    reasoning_effort: str | None = None,
    max_attempts: int = MAX_RETRIES,
    image_handoff_callback=None,
    image_settle_callbacks=(),
    request_receipt: dict | None = None,
) -> str:
    """Call the production Qwen/DashScope transcription route.

    One request may contain all frozen template columns. A failed provider or
    strict-contract response gets exactly one automatic retry; all other 4xx
    responses fail immediately.
    """
    max_tokens = _cap_student_ocr_max_tokens(max_tokens)
    # The student route is pinned in config; a stale deployment/environment
    # override must not restore the retired Plus route at request time.
    model = _DEFAULT_MODEL

    image_paths = [str(image_path)] if isinstance(image_path, str) else [
        str(path) for path in image_path
    ]
    if not 1 <= len(image_paths) <= 12 or len(set(image_paths)) != len(image_paths):
        raise ValueError("Qwen OCR requires 1-12 unique images")

    def encode_images():
        return [_encode_vision_image(path) for path in image_paths]

    encoded_images = await _run_local_ocr_image_component(
        encode_images,
        semaphore=_get_local_ocr_preprocess_semaphore(),
        on_handoff=image_handoff_callback,
        settle_callbacks=image_settle_callbacks,
    )
    image_content = [
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
        for b64, mime in encoded_images
    ]
    text_content = {"type": "text", "text": prompt}

    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    selected_reasoning_effort = str(
        reasoning_effort or os.environ.get("OCR_TRANSCRIPTION_REASONING_EFFORT", "none")
    ).strip().lower()
    if selected_reasoning_effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}:
        raise ValueError("reasoning_effort must be none, minimal, low, medium, high, xhigh, or max")
    if selected_reasoning_effort not in {"none", "minimal"}:
        raise ValueError("student transcription requires thinking disabled (reasoning_effort=none)")

    payload = {
        "model": model,
        # Qwen Flash requires the official two-key disabled combination.
        "enable_thinking": False,
        "reasoning_effort": "none",
        "messages": [{
            "role": "user",
            "content": (
                [text_content, *image_content]
                if prompt_first
                else [*image_content, text_content]
            ),
        }],
        "max_completion_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
    }
    del encoded_images

    last_error: Exception | None = None
    attempt = 0
    while attempt < max_attempts:
        _sync_pool_from_profiles()
        # 从号池取Key，无号池时走单Key兼容
        _release_key = None
        _released = False
        if _vision_pool:
            try:
                api_key, _release_key, pool_base_url = await acquire_key(_vision_pool)
            except RuntimeError:
                if last_error is not None:
                    raise last_error
                raise
        else:
            _profile_key, _profile_base = _profile_credentials("student_transcribe")
            api_key = _profile_key or os.environ.get("DASHSCOPE_API_KEY", _DEFAULT_API_KEY)
            pool_base_url = _profile_base if _profile_key else ""

        def _release_once() -> None:
            nonlocal _released
            if _vision_pool and _release_key and not _released:
                _vision_pool.release(_release_key)
                _released = True

        base_url = pool_base_url or os.environ.get("DASHSCOPE_BASE_URL", _DEFAULT_BASE_URL)
        url = f"{base_url.rstrip('/')}/chat/completions"

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        attempt_started = time.monotonic()
        attempt_number = attempt + 1
        last_error = None
        try:
            resp = await _post_vision_request(url, headers, payload, api_key)
            status = resp.status_code
            if status in {401, 403}:
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    auth_error: Exception = exc
                else:
                    auth_error = RuntimeError(f"Vision API returned HTTP {status}")
                _release_once()
                last_error = auth_error
                if _vision_pool:
                    _vision_pool.mark_invalid(api_key, status)
                    attempt += 1
                    if attempt < max_attempts and _vision_pool.has_enabled_keys():
                        continue
                raise auth_error
            if status == 429:
                retry_after = None
                ra = resp.headers.get("retry-after") or resp.headers.get("x-ratelimit-reset-requests")
                if ra:
                    try:
                        retry_after = float(ra)
                    except (ValueError, TypeError):
                        pass
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                else:
                    last_error = RuntimeError("Vision API returned HTTP 429")
                _release_once()
                if _vision_pool:
                    _vision_pool.mark_429(api_key, retry_after)
                attempt += 1
                logger.warning("Vision API 429, 换Key重试 (attempt %d/%d)", attempt, max_attempts)
                if not _vision_pool and attempt < max_attempts:
                    await asyncio.sleep(retry_after or RETRY_DELAY * (2 ** (attempt - 1)))
                continue
            if status >= 500:
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                else:
                    last_error = RuntimeError(f"Vision API returned HTTP {status}")
                _release_once()
                attempt += 1
                delay = RETRY_DELAY * (2 ** (attempt - 1))
                logger.warning("Vision API %d, retrying (attempt %d/%d)",
                               status, attempt, max_attempts)
                if attempt < max_attempts:
                    await asyncio.sleep(delay)
                continue
            resp.raise_for_status()
            data = _parse_streamed_vision_response(resp)
            # Never accept a response for which the provider reports reasoning.
            # ``enable_thinking=false`` is already sent on every attempt; this
            # fail-closed check catches a gateway/model that ignores the flag.
            _msg = (data.get("choices") or [{}])[0].get("message") or {}
            _reasoning = _msg.get("reasoning_content")
            _had_reasoning = bool(
                _reasoning.strip() if isinstance(_reasoning, str) else _reasoning
            )
            if _had_reasoning:
                _release_once()
                payload["enable_thinking"] = False
                attempt += 1
                delay = RETRY_DELAY * (2 ** (attempt - 1))
                last_error = RuntimeError("vision provider returned reasoning_content")
                logger.warning(
                    "Vision API ignored enable_thinking=false; rejecting response "
                    "(attempt %d/%d)",
                    attempt, max_attempts,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(delay)
                continue

            # 空响应防护：模型偶发返回空正文（典型为思考链耗尽 max_token，
            # 只有 reasoning_content 而无 content，finish_reason=length）。
            # 此时 content 为空串/None，若直接返回会让下游 JSON 解析静默失败、难定位。
            # 这里当作可重试错误处理，并强制下一轮彻底关闭 thinking。
            _content = _msg.get("content")
            _finish = (data.get("choices") or [{}])[0].get("finish_reason")
            if not (_content and _content.strip()):
                _release_once()
                payload["enable_thinking"] = False  # 强制后续重试关闭thinking
                attempt += 1
                delay = RETRY_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    "Vision API 空响应 (finish_reason=%s, 有思考=%s), "
                    "关thinking重试 (attempt %d/%d)",
                    _finish, _had_reasoning, attempt, max_attempts,
                )
                last_error = RuntimeError(
                    f"empty content (finish_reason={_finish}, had_reasoning={_had_reasoning})"
                )
                if attempt < max_attempts:
                    await asyncio.sleep(delay)
                continue
            if response_validator is not None:
                try:
                    validated = response_validator(_content)
                    if inspect.isawaitable(validated):
                        await validated
                except Exception as exc:
                    _release_once()
                    attempt += 1
                    last_error = exc
                    logger.warning(
                        "Qwen OCR strict contract rejected (attempt %d/%d)",
                        attempt,
                        max_attempts,
                    )
                    if attempt < max_attempts:
                        await asyncio.sleep(RETRY_DELAY * (2 ** (attempt - 1)))
                    continue
            if _vision_pool:
                _vision_pool.mark_success(api_key)
            _release_once()
            if request_receipt is not None:
                def model_id(value):
                    text = str(value or "").strip()
                    return text if re.fullmatch(r"[A-Za-z0-9._:/-]{1,128}", text) else ""

                request_receipt.update({
                    "phase": "student_transcription",
                    "route": "dashscope",
                    "requested_model": model_id(model),
                    # Missing response identity is unknown, never the alias.
                    "response_model": model_id(data.get("model")),
                    "finish_reason": str(_finish or "")[:32],
                    "attempts": attempt_number,
                    "enable_thinking": payload["enable_thinking"],
                    "reasoning_effort": payload["reasoning_effort"],
                    "elapsed_seconds": round(time.monotonic() - attempt_started, 3),
                })
                # 计费口径只认供应商回报的 usage；没有回报就不写，绝不估算冒充实测。
                usage = data.get("usage") if isinstance(data, dict) else None
                if isinstance(usage, dict):
                    billed = {}
                    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                        value = usage.get(name)
                        if isinstance(value, bool):
                            continue
                        try:
                            billed[name] = int(value)
                        except (TypeError, ValueError):
                            continue
                    details = usage.get("prompt_tokens_details")
                    if isinstance(details, dict):
                        try:
                            billed["image_tokens"] = int(details.get("image_tokens"))
                        except (TypeError, ValueError):
                            pass
                    if billed:
                        request_receipt["usage"] = billed
            return _content

        except asyncio.CancelledError:
            _release_once()
            raise
        except httpx.TransportError as e:
            _release_once()
            attempt += 1
            delay = RETRY_DELAY * (2 ** (attempt - 1))
            logger.warning("Vision API transport error (%s), retrying (attempt %d/%d)",
                           type(e).__name__, attempt, max_attempts)
            last_error = e
            if attempt < max_attempts:
                await asyncio.sleep(delay)
        except RateBudgetTimeout as e:
            # A quota wait is scoped to this concrete HTTP attempt. If it
            # expires, release any local credential lease and retry according
            # to the same bounded attempt policy rather than bypassing TPM.
            _release_once()
            attempt += 1
            delay = RETRY_DELAY * (2 ** (attempt - 1))
            logger.warning(
                "Vision rate budget wait timed out, retrying (attempt %d/%d)",
                attempt,
                max_attempts,
            )
            last_error = e
            if attempt < max_attempts:
                await asyncio.sleep(delay)
        except httpx.HTTPStatusError as e:
            _release_once()
            last_error = e
            raise
        except (json.JSONDecodeError, KeyError, IndexError, ValueError, TypeError) as e:
            _release_once()
            # 响应解析错误（JSON 损坏 / 字段缺失 / 类型不符）：可重试。
            # 收窄异常类型，避免吞掉 SyntaxError/ImportError 等编程错误还重试。
            attempt += 1
            delay = RETRY_DELAY * (2 ** (attempt - 1))
            last_error = e
            if attempt < max_attempts:
                await asyncio.sleep(delay)
        except Exception as e:
            _release_once()
            last_error = e
            raise
        finally:
            if last_error is not None:
                record_attempt(last_error, stage="transcription", model=model, route="dashscope",
                               attempt=attempt_number, retry_count=attempt_number - 1,
                               duration_ms=(time.monotonic() - attempt_started) * 1000)

    raise last_error or RuntimeError(
        f"Vision API call failed after {max_attempts} attempts"
    )


async def call_vision(
    image_path: str,
    prompt: str,
    max_tokens: int = 9000,
    *,
    response_validator=None,
    prompt_first: bool = True,
    reasoning_effort: str | None = None,
    max_attempts: int = MAX_RETRIES,
    request_receipt: dict | None = None,
) -> str:
    """Run one student-image Qwen lifecycle inside the memory window."""
    max_tokens = _cap_student_ocr_max_tokens(max_tokens)
    payload_bytes = _estimated_vision_payload_bytes(image_path)
    async with _get_local_ocr_request_semaphore():
        payload_budget = _get_local_ocr_payload_budget()
        reserved_bytes = await payload_budget.acquire(
            payload_bytes,
            timeout_seconds=_LOCAL_OCR_PAYLOAD_BUDGET_WAIT_SECONDS,
        )
        handed_off = False

        def mark_handed_off() -> None:
            nonlocal handed_off
            handed_off = True

        async def release_handed_off_budget() -> None:
            await payload_budget.release(reserved_bytes)

        try:
            return await _call_vision_request(
                image_path,
                prompt,
                max_tokens=max_tokens,
                response_validator=(
                    _multi_pool_response_validator(
                        prompt,
                        response_validator=response_validator,
                    )
                    if response_validator is not None
                    or "[task=student_transcription_only]" in prompt
                    else None
                ),
                prompt_first=prompt_first,
                reasoning_effort=reasoning_effort,
                max_attempts=max_attempts,
                **({"request_receipt": request_receipt} if request_receipt is not None else {}),
                image_handoff_callback=mark_handed_off,
                image_settle_callbacks=(release_handed_off_budget,),
            )
        finally:
            if not handed_off:
                await payload_budget.release(reserved_bytes)


async def call_vision_images(
    image_paths: list[str],
    prompt: str,
    max_tokens: int = 9000,
    *,
    response_validator=None,
    prompt_first: bool = True,
    reasoning_effort: str | None = None,
    max_attempts: int = MAX_RETRIES,
    request_receipt: dict | None = None,
) -> str:
    """Send ordered template-column images to Qwen in one request."""

    max_tokens = _cap_student_ocr_max_tokens(max_tokens)
    paths = [str(path) for path in image_paths]
    if not 1 <= len(paths) <= 12 or len(set(paths)) != len(paths):
        raise ValueError("OCR multi-image payload must contain 1-12 unique images")
    payload_bytes = _estimated_vision_images_payload_bytes(paths)
    async with _get_local_ocr_request_semaphore():
        payload_budget = _get_local_ocr_payload_budget()
        reserved_bytes = await payload_budget.acquire(
            payload_bytes,
            timeout_seconds=_LOCAL_OCR_PAYLOAD_BUDGET_WAIT_SECONDS,
        )
        handed_off = False

        def mark_handed_off() -> None:
            nonlocal handed_off
            handed_off = True

        async def release_handed_off_budget() -> None:
            await payload_budget.release(reserved_bytes)

        try:
            return await _call_vision_request(
                paths,
                prompt,
                max_tokens=max_tokens,
                response_validator=(
                    _multi_pool_response_validator(
                        prompt,
                        response_validator=response_validator,
                    )
                    if response_validator is not None
                    or "[task=student_transcription_only]" in prompt
                    else None
                ),
                prompt_first=prompt_first,
                reasoning_effort=reasoning_effort,
                max_attempts=max_attempts,
                **({"request_receipt": request_receipt} if request_receipt is not None else {}),
                image_handoff_callback=mark_handed_off,
                image_settle_callbacks=(release_handed_off_budget,),
            )
        finally:
            if not handed_off:
                await payload_budget.release(reserved_bytes)


# Uploaded images reach the transcription route as files. These formats pass
# through unchanged; anything else Pillow can read is re-encoded as PNG.
_READ_IMAGE_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
}


def _write_read_image(path: str, raw: bytes, *, reencode: bool) -> None:
    if not reencode:
        Path(path).write_bytes(raw)
        return
    import io
    from PIL import Image

    with Image.open(io.BytesIO(raw)) as image:
        _validate_source_dimensions(image)
        image.convert("RGB").save(path, format="PNG")


async def read_image(
    image: bytes | bytearray | memoryview | str,
    mime: str,
    prompt: str,
    *,
    teacher_id: str = "system",
    max_tokens: int = 2048,
) -> str:
    """Read one uploaded image with qwen3.8-flash and return the model's text.

    Serves 识图 and roster photos. ``image`` is raw bytes or Base64 text. The
    request takes the student-transcription route: thinking off, the shared
    DashScope budget and the OCR lanes and payload budget.
    """
    raw = (
        base64.b64decode(image, validate=True)
        if isinstance(image, str)
        else bytes(image)
    )
    if not raw:
        raise ValueError("image is empty")
    suffix = _READ_IMAGE_SUFFIXES.get(str(mime or "").strip().lower())
    with tempfile.TemporaryDirectory(prefix="mathagent-read-image-") as folder:
        path = os.path.join(folder, "image" + (suffix or ".png"))
        await asyncio.to_thread(_write_read_image, path, raw, reencode=suffix is None)
        token = set_vision_teacher_id(teacher_id)
        try:
            return await call_vision(path, prompt, max_tokens=max_tokens)
        finally:
            reset_vision_teacher_id(token)


async def get_ocr_readiness() -> dict:
    """Return Qwen student-OCR readiness without sending a paid request."""
    _sync_pool_from_profiles()
    _profile_key, _profile_base = _profile_credentials("student_transcribe")
    configured_key = bool(
        _vision_pool
        or _profile_key
        or os.environ.get("DASHSCOPE_API_KEY", _DEFAULT_API_KEY).strip()
    )
    configured_base = bool(
        _profile_base
        or os.environ.get("DASHSCOPE_BASE_URL", _DEFAULT_BASE_URL).strip()
    )
    if not configured_key or not configured_base:
        return {
            "provider": "ocr",
            "configured": False,
            "ready": False,
            "reason": "qwen_not_configured",
        }
    global _local_image_readiness_probe, _local_image_readiness_probe_loop
    global _local_image_readiness_cache, _local_image_readiness_cache_at
    loop = asyncio.get_running_loop()
    if (
        _local_image_readiness_probe is None
        or _local_image_readiness_probe_loop is not loop
        or _local_image_readiness_probe.done()
    ):
        _local_image_readiness_probe = loop.run_in_executor(
            _readiness_probe_executor, get_local_image_readiness
        )
        _local_image_readiness_probe_loop = loop
    probe = _local_image_readiness_probe
    try:
        local_image = await asyncio.wait_for(asyncio.shield(probe), timeout=3.0)
    except asyncio.TimeoutError:
        # A slow probe under OCR load means busy, never broken: serve the
        # last known verdict instead of flipping readiness to false.
        if _local_image_readiness_cache is not None:
            return dict(_local_image_readiness_cache)
        return {
            "provider": "ocr",
            "configured": True,
            "ready": False,
            "reason": "local_image_readiness_timed_out",
        }
    except Exception:
        logger.exception("local image readiness probe failed")
        return {
            "provider": "ocr",
            "configured": True,
            "ready": False,
            "reason": "local_image_readiness_failed",
        }
    finally:
        if probe.done() and _local_image_readiness_probe is probe:
            _local_image_readiness_probe = None
    if not local_image["ready"]:
        verdict = {
            "provider": "ocr",
            "configured": True,
            "ready": False,
            "reason": local_image["reason"],
        }
    else:
        verdict = {
            "provider": "ocr",
            "configured": True,
            "ready": True,
            "reason": "",
        }
    # Only successful verdicts back the busy fallback (the last successful
    # heartbeat).  A stall must always be re-measured so bad news is never
    # served stale-ready and fail-closed stays sharp.
    if verdict["ready"]:
        _local_image_readiness_cache = verdict
        _local_image_readiness_cache_at = time.monotonic()
    return dict(verdict)

async def recognize_paper(image_path: str, teacher_id: str = "") -> dict:
    """Transcribe a whole page without a template (photo bank import).

    Runs under the per-worker image lifecycle bound.
    """
    token = set_vision_teacher_id(teacher_id)
    try:
        return await _recognize_paper_limited(image_path, teacher_id)
    finally:
        reset_vision_teacher_id(token)


async def _recognize_paper_limited(image_path: str, teacher_id: str) -> dict:
    """Separate CPU-heavy preprocessing from the wider request-memory window."""
    processed_path = await _prepare_vision_image(image_path)
    try:
        return await _recognize_paper_preprocessed(processed_path, teacher_id)
    finally:
        _cleanup_prepared_image(image_path, processed_path)


async def _recognize_paper_preprocessed(image_path: str, teacher_id: str = "") -> dict:
    """Transcribe one preprocessed page, including the student's name and number.

    Returns ``{"questions": [...], "student_name": "", "student_no": ""}`` or
    an ``ocr_failure_result``. call_vision owns the single provider and
    contract retry, so this makes one request.
    """
    max_tokens = _cap_student_ocr_max_tokens(OCR_TRANSCRIPTION_MAX_OUTPUT_TOKENS)
    prompt = """[task=student_transcription_only]
你是中国高中数学试卷 OCR，只做视觉转录，不解题、不判分。
这是差分后的学生试卷（已经完成模板配准和差分）；印刷题干通常已经去除，但图片中可能残留少量不完整题干、印刷线条或差分残留。
按图识别每道题的题号、可见题干残留和学生答案；如果输入包含多栏，按从左到右视为同一页。
question_text 只能抄图片中仍然可见的题干文字；没有可见残留时返回空字符串。绝不重建、补全、猜测或改写被差分去除的题干，也不要把印刷残留写入 student_answer。
student_answer 只抄学生实际写下的笔迹（即 student_answer 只抄学生实际写下的内容），不纠正、不补全、不改写、不合并、不移动，不参考标准答案或数学常识。根号、分数、正负号、括号、数字和字母按图原样抄写。
无可见笔迹时返回 answer_state=blank 和空字符串（空白返回 blank）；有笔迹但无法可靠辨认或归属时返回 answer_state=unreadable 和 [无法识别]。
同时识别学生姓名和学号；没有或看不清时返回空字符串。

question_type 只能是 choice、fill 或 solve。选择题先按图抄录可见内容；不要把数字或公式主动改成选项字母，已确认选择题的归一化由服务端处理。不要根据残缺题干、标准答案或数学知识猜答案（不要根据题干猜答案）。

严格返回 JSON，不要 Markdown 或其他文字：
{
  "questions": [
    {
      "question_number": "1",
      "question_text": "图片中仍可见的题干残留；没有可见残留时为''，不得补全",
      "student_answer": "学生原始笔迹；空答为''；不清楚为'[无法识别]'",
      "answer_state": "blank|visible|unreadable",
      "question_type": "choice|fill|solve"
    }
  ],
  "student_name": "姓名或空字符串",
  "student_no": "学号或空字符串"
}"""

    from services.teaching_context import stage_prompt
    prompt = stage_prompt(prompt)

    # 每老师并发限制：包住实际的 Vision 调用，使同一老师跨并发批次
    # 的在途 OCR 不超过 OCR_PER_TEACHER_LIMIT。teacher_id 为空时不限。
    from config import OCR_PER_TEACHER_LIMIT
    teacher_sem = (
        await get_ocr_teacher_semaphore(teacher_id, OCR_PER_TEACHER_LIMIT)
        if teacher_id
        else nullcontext()
    )

    raw_result = None
    try:
        async with teacher_sem:
            raw_result = await call_vision(image_path, prompt, max_tokens=max_tokens)
        data = parse_llm_json(raw_result)
        questions = validate_visual_transcriptions(data.get("questions", []))
        if questions:
            return {
                "questions": questions,
                "student_name": _sanitize_student_name(data.get("student_name", "")),
                "student_no": _sanitize_student_no(data.get("student_no", "")),
            }
    except Exception as error:
        # ocr_failure_result classifies the error (retryable, error code).
        return ocr_failure_result(error)
    logger.warning("OCR返回空questions, raw: %s", (raw_result or "")[:200])
    return ocr_failure_result(
        RuntimeError("OCR识别返回空结果"),
        retryable=True,
        error_code="empty_provider_response",
    )


async def recognize_student_info_with_roster(
    image_path: str,
    roster_names: list[str],
    teacher_id: str = "",
) -> dict:
    """Retry unresolved identities from high-resolution top and left regions.

    The top crop covers the original first 25% of the paper. The complete
    leftmost 15% is rotated clockwise below it so sideways binding information
    is readable and a name crossing the upper boundary is not clipped. The
    single-image Vision API receives both source regions without resizing.

    The roster is the closed candidate set for automatic assignment.  A genuinely
    new student is not auto-matched: it remains available only through the
    teacher's explicit manual-create confirmation path.
    """
    token = set_vision_teacher_id(teacher_id)
    crop_path = image_path
    try:
        async with _get_local_identity_preprocess_semaphore():
            crop_path = await _run_local_ocr_image_component(
                _build_identity_crop,
                image_path,
            )

        roster_names = list(dict.fromkeys(
            str(name).strip() for name in roster_names if str(name).strip()
        ))
        roster = "、".join(roster_names)
        prompt = f"""识别图片中学生手写的姓名和学号。
图片由两个未缩放区域上下拼接：上方是试卷顶部填写区，下方是顺时针旋转后的左侧装订信息栏；两区可能有重叠。
班级名单：{roster or '未提供'}
规则：
1. raw_name 只抄图片实际字迹，看不清返回空字符串。
2. candidate_names 只能从名单中选择，且必须与 raw_name 完全一致；无法确认时返回空数组，不得猜测或强行匹配。
3. student_no 看不清返回空字符串。
4. confidence 为 raw_name 的视觉置信度，范围 0 到 1。
严格返回 JSON：{{"raw_name":"","candidate_names":[],"student_no":"","confidence":0.0}}"""
        identity_call_kwargs = {"max_tokens": 512}
        try:
            vision_params = inspect.signature(call_vision).parameters
        except (TypeError, ValueError):
            vision_params = {}
        if (
            "max_attempts" in vision_params
            or any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in vision_params.values()
            )
        ):
            identity_call_kwargs["max_attempts"] = 2
        raw = await call_vision(crop_path, prompt, **identity_call_kwargs)
        data = parse_llm_json(raw)
        student_no = _sanitize_student_no(str(data.get("student_no") or ""))
        try:
            confidence = max(0.0, min(1.0, float(data.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0
        candidate_names = _normalize_roster_candidate_names(
            data.get("candidate_names", data.get("candidates", [])), roster_names,
        )
        raw_name = _sanitize_student_name(str(
            data.get("raw_name", data.get("name", "")) or ""
        ))
        # A closed candidate list must not turn into forced classification. The
        # model first transcribes the visible writing without roster pressure;
        # automatic assignment is possible only when that transcription exactly
        # equals the single roster candidate.
        proposed_candidates = bool(data.get("candidate_names", data.get("candidates")))
        if not raw_name or raw_name not in candidate_names:
            candidate_names = []
        # Accept the previous single-name shape only when it exactly names a
        # current roster member, keeping an interrupted rollout backward-compatible.
        if not candidate_names and "raw_name" not in data:
            candidate_names = _normalize_roster_candidate_names(
                [data.get("name", "")], roster_names,
            )

        if len(candidate_names) == 1 and confidence >= IDENTITY_ROSTER_AUTO_MATCH_CONFIDENCE:
            name = candidate_names[0]
            match_status = "unique_high_confidence"
        elif len(candidate_names) > 1:
            name = ""
            match_status = "ambiguous"
        elif len(candidate_names) == 1:
            name = ""
            match_status = "low_confidence"
        else:
            name = ""
            match_status = "no_match"
        return {
            "name": name,
            "student_no": student_no,
            "confidence": confidence,
            "candidate_names": candidate_names,
            "match_status": match_status,
            # Tells a blank identity area apart from an unmatched name;
            # any candidate the model proposed also counts as writing.
            "name_written": bool(raw_name) or proposed_candidates,
        }
    except Exception as exc:
        logger.warning("Roster-aware identity recognition failed for %s: %s", image_path, exc)
        return {
            "name": "", "student_no": "", "confidence": 0.0,
            "candidate_names": [], "match_status": "failed",
        }
    finally:
        reset_vision_teacher_id(token)
        if crop_path != image_path and os.path.exists(crop_path):
            try:
                os.remove(crop_path)
            except OSError:
                pass


async def recognize_answer_sheet(image_path: str) -> list[dict]:
    """Recognize an answer-key sheet image, returning [{question_no, answer}, ...].

    Used for photo-import bank: the user uploads a separate page of pure answers
    (题号 + 答案), no question text. Robust to messy formatting.
    """
    prompt = """识别这张数学答案页中的全部题号和对应答案。
只读题号和答案，不读题干；答案按图原样抄写，不化简、不改写。
分数写 a/b，根号写 √x；多空用分号分隔，多选用逗号分隔；无法识别返回空字符串。
必须按图片中的每个题号返回一次，不得遗漏或重复。
严格返回 JSON 数组，不要其他文字：
[
    {"question_no": "1", "answer": "5/12"},
    {"question_no": "2", "answer": "x=2"}
]"""
    raw_result = None
    try:
        raw_result = await call_vision(image_path, prompt, max_tokens=4096)
        data = parse_llm_json(raw_result)
        # 兼容 {"answers":[...]} 或直接数组
        if isinstance(data, dict):
            data = data.get("answers") or data.get("items") or []
        if not isinstance(data, list):
            return []
        result = []
        for item in data:
            if not isinstance(item, dict):
                continue
            qn = str(item.get("question_no") or item.get("question_number") or "").strip()
            ans = str(item.get("answer") or "").strip()
            if qn:
                result.append({"question_no": qn, "answer": ans})
        return result
    except Exception as e:
        report_failure(e, stage="transcription")
        return []
