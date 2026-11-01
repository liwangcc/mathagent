"""Vision API (Qwen/DashScope) 试卷识别 — 替代 PaddleOCR"""

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

logger = logging.getLogger(__name__)

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

_vision_pool = _create_dashscope_pool()


def _profile_credentials(group: str) -> tuple[str, str]:
    """设置页「职责组」配置的 (api_key, base_url)。解析失败一律回退空值。"""
    try:
        from services.model_profiles import credentials

        return credentials(group)
    except Exception:
        return "", ""


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

_LOCAL_IMAGE_WORK_STALL_SECONDS = 120.0
_LOCAL_IMAGE_COMPONENT_TIMEOUT_SECONDS = 120.0
# Pool dispatch and execution have separate deadlines, and a template retry may
# then enter the normal first/retry scheduler.  A shorter independent timeout on
# this memory queue misclassifies healthy backpressure as a local image fault.
# The owning batch/template deadline remains authoritative and cancellation-safe.
_LOCAL_OCR_PAYLOAD_BUDGET_WAIT_SECONDS: float | None = None
_LOCAL_IMAGE_STALL_HEARTBEAT_SECONDS = 10.0
_LOCAL_IMAGE_STALL_MARKER_STALE_SECONDS = 45.0
_LOCAL_IMAGE_STALL_MARKER_DIR = Path(UPLOAD_DIR) / ".system-readiness"
_local_image_work_started: dict[int, float] = {}
_local_image_work_lock = threading.Lock()
_stalled_image_settlers: set[asyncio.Task] = set()


class LocalImagePipelineStalled(RuntimeError):
    """A native image component exceeded the bounded business wait."""

# The context variable lets low-level call_vision participate in the shared
# fair limiter without widening every template-recognition function signature.
_vision_teacher_id: ContextVar[str] = ContextVar("vision_teacher_id", default="system")
_vision_route_ordinal: ContextVar[int | None] = ContextVar("vision_route_ordinal", default=None)
_vision_phase_key: ContextVar[str] = ContextVar("vision_phase_key", default="")
_vision_retry_phase_key: ContextVar[str] = ContextVar(
    "vision_retry_phase_key", default=""
)
_vision_batch_paper_count: ContextVar[int | None] = ContextVar(
    "vision_batch_paper_count", default=None
)
_vision_batch_deadline_s: ContextVar[float | None] = ContextVar(
    "vision_batch_deadline_s", default=None
)
_local_ocr_request_semaphore: asyncio.Semaphore | None = None
_local_ocr_preprocess_semaphore: asyncio.Semaphore | None = None
_local_identity_preprocess_semaphore: asyncio.Semaphore | None = None
_local_ocr_payload_budget = None
_local_ocr_request_loop: asyncio.AbstractEventLoop | None = None
_local_ocr_preprocess_loop: asyncio.AbstractEventLoop | None = None
_local_identity_preprocess_loop: asyncio.AbstractEventLoop | None = None
_local_ocr_payload_budget_loop: asyncio.AbstractEventLoop | None = None
_local_image_readiness_probe: asyncio.Task | None = None
_local_image_readiness_probe_loop: asyncio.AbstractEventLoop | None = None


class _AsyncByteBudget:
    """Cancellation-safe process-local byte budget for resident request bodies."""

    def __init__(self, capacity: int):
        self.capacity = int(capacity)
        self._used = 0
        self._condition = asyncio.Condition()

    async def acquire(
        self,
        amount: int,
        *,
        timeout_seconds: float | None = None,
    ) -> int:
        amount = max(1, int(amount))
        if amount > self.capacity:
            raise ValueError("OCR image payload exceeds the per-worker memory budget")
        timeout = None if timeout_seconds is None else float(timeout_seconds)
        if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("OCR image payload budget timeout must be finite and positive")
        try:
            async with self._condition:
                if timeout is None:
                    await self._condition.wait_for(
                        lambda: self._used + amount <= self.capacity
                    )
                else:
                    async with asyncio.timeout(timeout):
                        await self._condition.wait_for(
                            lambda: self._used + amount <= self.capacity
                        )
                self._used += amount
        except TimeoutError as exc:
            raise LocalImagePipelineStalled(
                "OCR image payload memory budget remained blocked for "
                f"{timeout:g} seconds"
            ) from exc
        return amount

    async def release(self, amount: int) -> None:
        amount = max(1, int(amount))
        async with self._condition:
            if amount > self._used:
                raise RuntimeError("OCR image payload budget released too many bytes")
            self._used -= amount
            self._condition.notify_all()

    @asynccontextmanager
    async def reserve(self, amount: int):
        amount = await self.acquire(amount)
        try:
            yield
        finally:
            await self.release(amount)


def set_vision_teacher_id(teacher_id: str):
    """Set request-local teacher context for every low-level Vision call."""
    return _vision_teacher_id.set(str(teacher_id or "system"))


def reset_vision_teacher_id(token) -> None:
    _vision_teacher_id.reset(token)


def set_vision_route_ordinal(ordinal: int):
    return _vision_route_ordinal.set(int(ordinal))


def reset_vision_route_ordinal(token) -> None:
    _vision_route_ordinal.reset(token)


def set_vision_phase_key(phase_key: str, *, retry_scope_key: str | None = None):
    """Select deadline and retry-ledger scopes for one logical OCR phase."""
    normalized = str(phase_key or "").strip()
    if not normalized or len(normalized) > 240:
        raise ValueError("vision phase key must contain 1-240 characters")
    retry_scope = normalized if retry_scope_key is None else str(retry_scope_key).strip()
    if not retry_scope or len(retry_scope) > 240:
        raise ValueError("vision retry scope key must contain 1-240 characters")
    return (
        _vision_phase_key.set(normalized),
        _vision_retry_phase_key.set(retry_scope),
    )


def reset_vision_phase_key(token) -> None:
    phase_token, retry_token = token
    _vision_retry_phase_key.reset(retry_token)
    _vision_phase_key.reset(phase_token)


def set_vision_batch_paper_count(paper_count: int):
    """Attach one logical student-batch size to nested OCR requests."""
    if isinstance(paper_count, bool) or not isinstance(paper_count, int):
        raise ValueError("vision batch paper count must be a positive integer")
    if paper_count < 1:
        raise ValueError("vision batch paper count must be a positive integer")
    return _vision_batch_paper_count.set(paper_count)


def reset_vision_batch_paper_count(token) -> None:
    _vision_batch_paper_count.reset(token)


def set_vision_batch_deadline_s(deadline_s: float):
    """Override one workflow's scheduler deadline without changing pool limits."""
    normalized = float(deadline_s)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError("vision batch deadline must be finite and positive")
    return _vision_batch_deadline_s.set(normalized)


def reset_vision_batch_deadline_s(token) -> None:
    _vision_batch_deadline_s.reset(token)


def _get_local_ocr_request_semaphore() -> asyncio.Semaphore:
    """Bound per-worker Base64/request memory without limiting CPU preprocessing."""
    global _local_ocr_request_semaphore, _local_ocr_request_loop
    loop = asyncio.get_running_loop()
    if _local_ocr_request_semaphore is None or _local_ocr_request_loop is not loop:
        _local_ocr_request_semaphore = asyncio.Semaphore(OCR_LOCAL_MAX_CONCURRENT)
        _local_ocr_request_loop = loop
    return _local_ocr_request_semaphore


def _get_local_ocr_preprocess_semaphore() -> asyncio.Semaphore:
    """Bound CPU-heavy rotate/deskew/contrast work independently of HTTP slots."""
    global _local_ocr_preprocess_semaphore, _local_ocr_preprocess_loop
    loop = asyncio.get_running_loop()
    if (
        _local_ocr_preprocess_semaphore is None
        or _local_ocr_preprocess_loop is not loop
    ):
        _local_ocr_preprocess_semaphore = asyncio.Semaphore(
            OCR_PREPROCESS_LOCAL_MAX_CONCURRENT
        )
        _local_ocr_preprocess_loop = loop
    return _local_ocr_preprocess_semaphore


def _get_local_identity_preprocess_semaphore() -> asyncio.Semaphore:
    """Serialize full-resolution identity crops inside each worker."""
    global _local_identity_preprocess_semaphore, _local_identity_preprocess_loop
    loop = asyncio.get_running_loop()
    if (
        _local_identity_preprocess_semaphore is None
        or _local_identity_preprocess_loop is not loop
    ):
        _local_identity_preprocess_semaphore = asyncio.Semaphore(1)
        _local_identity_preprocess_loop = loop
    return _local_identity_preprocess_semaphore


def _get_local_ocr_payload_budget() -> _AsyncByteBudget:
    global _local_ocr_payload_budget, _local_ocr_payload_budget_loop
    loop = asyncio.get_running_loop()
    if (
        _local_ocr_payload_budget is None
        or _local_ocr_payload_budget_loop is not loop
        or _local_ocr_payload_budget.capacity
        != OCR_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES
    ):
        _local_ocr_payload_budget = _AsyncByteBudget(
            OCR_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES
        )
        _local_ocr_payload_budget_loop = loop
    return _local_ocr_payload_budget


async def _settle_local_image_thread(task: asyncio.Task) -> None:
    """Wait for native image work that Python cannot cancel once started."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    await asyncio.gather(task, return_exceptions=True)


async def _run_local_image_thread(function, /, *args, **kwargs):
    """Do not report cancellation until the underlying image thread settles."""
    thread_task = asyncio.create_task(
        asyncio.to_thread(function, *args, **kwargs)
    )
    task_key = id(thread_task)
    with _local_image_work_lock:
        _local_image_work_started[task_key] = time.monotonic()
    try:
        try:
            return await asyncio.shield(thread_task)
        except BaseException:
            await _settle_local_image_thread(thread_task)
            raise
    finally:
        with _local_image_work_lock:
            _local_image_work_started.pop(task_key, None)


def _fresh_shared_image_stall_markers() -> int:
    """Return fresh cross-worker stall markers and prune abandoned markers."""
    try:
        markers = tuple(_LOCAL_IMAGE_STALL_MARKER_DIR.glob("ocr-image-*.stall"))
    except OSError:
        return 0
    now = time.time()
    fresh = 0
    for marker in markers:
        try:
            age = max(0.0, now - marker.stat().st_mtime)
        except OSError:
            continue
        if age <= _LOCAL_IMAGE_STALL_MARKER_STALE_SECONDS:
            fresh += 1
            continue
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass
    return fresh


def _handoff_stalled_image_thread(
    thread_task: asyncio.Task,
    gate: asyncio.Semaphore,
    *,
    settle_callbacks=(),
) -> None:
    """Keep the gate and a shared fault marker until native work really exits."""
    marker = _LOCAL_IMAGE_STALL_MARKER_DIR / (
        f"ocr-image-{os.getpid()}-{uuid.uuid4().hex}.stall"
    )

    async def settle() -> None:
        marker_ready = False
        try:
            while not thread_task.done():
                try:
                    _LOCAL_IMAGE_STALL_MARKER_DIR.mkdir(parents=True, exist_ok=True)
                    marker.touch(exist_ok=True)
                    marker_ready = True
                except OSError:
                    if not marker_ready:
                        logger.exception("Failed to publish shared image-stall readiness")
                        marker_ready = True
                try:
                    await asyncio.wait_for(
                        asyncio.shield(thread_task),
                        timeout=_LOCAL_IMAGE_STALL_HEARTBEAT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    continue
                except BaseException:
                    break
            await asyncio.gather(thread_task, return_exceptions=True)
        finally:
            gate.release()
            for callback in settle_callbacks:
                try:
                    result = callback()
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    logger.exception("Failed to release stalled image resource")
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                logger.warning("Failed to clear shared image-stall readiness marker")

    settler = asyncio.create_task(settle())
    _stalled_image_settlers.add(settler)

    def settled(task: asyncio.Task) -> None:
        _stalled_image_settlers.discard(task)
        if not task.cancelled():
            task.exception()

    settler.add_done_callback(settled)


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

def get_local_image_readiness() -> dict:
    """Report native image work that has exceeded its bounded normal runtime."""
    now = time.monotonic()
    with _local_image_work_lock:
        started = tuple(_local_image_work_started.values())
    oldest_age = max((now - value for value in started), default=0.0)
    shared_stalls = _fresh_shared_image_stall_markers()
    ready = (
        oldest_age < _LOCAL_IMAGE_WORK_STALL_SECONDS
        and shared_stalls == 0
    )
    return {
        "ready": ready,
        "reason": "" if ready else "local_image_pipeline_stalled",
        "active_components": len(started),
        "oldest_component_age_s": max(0.0, oldest_age),
        "shared_stall_components": shared_stalls,
    }


async def _run_local_ocr_image_component(
    function,
    /,
    *args,
    semaphore: asyncio.Semaphore | None = None,
    on_handoff=None,
    settle_callbacks=(),
    **kwargs,
):
    """Bound business wait while retaining the gate until native work settles."""
    gate = semaphore or _get_local_ocr_preprocess_semaphore()
    try:
        await asyncio.wait_for(
            gate.acquire(),
            timeout=_LOCAL_IMAGE_COMPONENT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise LocalImagePipelineStalled(
            "local image pipeline remained blocked for "
            f"{_LOCAL_IMAGE_COMPONENT_TIMEOUT_SECONDS:g} seconds"
        ) from exc

    thread_task = asyncio.create_task(
        _run_local_image_thread(function, *args, **kwargs)
    )
    handed_off = False
    try:
        return await asyncio.wait_for(
            asyncio.shield(thread_task),
            timeout=_LOCAL_IMAGE_COMPONENT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        handed_off = True
        if on_handoff is not None:
            on_handoff()
        _handoff_stalled_image_thread(
            thread_task,
            gate,
            settle_callbacks=settle_callbacks,
        )
        raise LocalImagePipelineStalled(
            "local image pipeline exceeded the "
            f"{_LOCAL_IMAGE_COMPONENT_TIMEOUT_SECONDS:g} second limit"
        ) from exc
    except asyncio.CancelledError:
        handed_off = True
        if on_handoff is not None:
            on_handoff()
        _handoff_stalled_image_thread(
            thread_task,
            gate,
            settle_callbacks=settle_callbacks,
        )
        raise
    finally:
        if not handed_off:
            gate.release()


def _estimated_vision_payload_bytes(image_path: str) -> int:
    """Estimate raw, Base64 bytes/string, and serialized JSON residency."""
    raw_bytes = os.path.getsize(image_path)
    if raw_bytes <= 0:
        raise ValueError("OCR image payload is empty")
    encoded_bytes = 4 * ((raw_bytes + 2) // 3)
    return raw_bytes + 3 * encoded_bytes


def _estimated_vision_images_payload_bytes(image_paths: list[str]) -> int:
    return sum(_estimated_vision_payload_bytes(path) for path in image_paths)


MAX_RETRIES = 2  # one first attempt plus one automatic retry
RETRY_DELAY = 2.0  # base backoff for 429, doubles each retry

_NAME_PATTERN = re.compile(r"^[一-鿿]{2,4}$")
_STUDENT_NO_PATTERN = re.compile(r"^\d{6,12}$")
_ANSWER_STATES = {"blank", "visible", "unreadable"}
IDENTITY_ROSTER_AUTO_MATCH_CONFIDENCE = 0.90
IDENTITY_ROSTER_MAX_CANDIDATES = 3


def _sanitize_student_name(raw: str) -> str:
    """Keep plausible 2–4 Chinese names; reject OCR status/prose sentinels."""
    if not raw:
        return ""
    name = raw.strip().strip('"').strip("'").strip("：:").strip()
    name = re.sub(r"^姓名\s*[：:]\s*", "", name).strip()
    invalid_markers = (
        "无姓名", "没有", "学号", "无法识别", "未识别", "看不清",
        "不清楚", "姓名不详", "空字符串",
    )
    if any(marker in name for marker in invalid_markers):
        return ""
    if _NAME_PATTERN.match(name):
        return name
    match = re.search(r"[一-鿿]{2,4}", name)
    if match and _NAME_PATTERN.match(match.group(0)):
        return match.group(0)
    return ""

def _sanitize_student_no(raw: str) -> str:
    """Keep a 6–12 digit student number; extract digits from mixed text, else ''."""
    if not raw:
        return ""
    no = str(raw).strip().strip('"').strip("'").strip("：:").strip()
    if _STUDENT_NO_PATTERN.match(no):
        return no
    # Fall back to the first 6–12 digit run embedded in prose like "学号：123456"
    digits = re.findall(r"\d{6,12}", no)
    return digits[0] if digits else ""


def validate_visual_transcriptions(
    raw_questions: object,
    *,
    slot_contracts: dict | None = None,
    drawing_only_numbers: set[str] | None = None,
) -> list[dict]:
    """Require each OCR answer to declare evidence consistent with its text.

    This cannot prove that a generative model read the right strokes, but it
    prevents structurally self-contradictory output from entering grading and
    gives later audit/review stages an explicit visual-evidence state.
    """
    if not isinstance(raw_questions, list):
        raise ValueError("questions must be a list")

    questions: list[dict] = []
    for index, raw_question in enumerate(raw_questions, 1):
        if not isinstance(raw_question, dict):
            raise ValueError(f"question {index} must be an object")
        question_number = str(raw_question.get("question_number") or "").strip()
        if not question_number:
            raise ValueError(f"question {index} is missing question_number")

        if slot_contracts is not None and question_number in slot_contracts:
            from services.template_slots import bind_student_slots
            raw_question = bind_student_slots(raw_question, question_number, {
                "slot_ids": slot_contracts[question_number],
            })

        # A pure drawing question has no text slot.  The visual model may still
        # return its required question row with ``student_slots: []``; normalize
        # that structural blank without inventing a textual answer.
        if drawing_only_numbers and question_number in drawing_only_numbers:
            if raw_question.get("student_slots") not in (None, []):
                raise ValueError(f"question {index} pure drawing has unexpected text slots")
            raw_question = dict(raw_question)
            raw_question.setdefault("student_answer", "")
            raw_question.setdefault("answer_state", "blank")

        state = str(raw_question.get("answer_state") or "").strip().lower()
        if state not in _ANSWER_STATES:
            raise ValueError(f"question {index} has invalid answer_state")
        raw_answer = raw_question.get("student_answer")
        if not isinstance(raw_answer, str):
            raise ValueError(f"question {index} student_answer must be text")
        answer = raw_answer.strip()

        # A multi-slot question whose every box is blank keeps only its
        # positional "；" separators. That is an empty transcription, not a
        # contradiction that may reject the whole page.
        #
        # A state that contradicts its own text (blank with text, visible
        # without text) says nothing reliable about this one answer. Keep it
        # fail-closed for this question only, as contracted slots already do,
        # instead of discarding every other answer on the page.
        if (
            (state == "blank" and answer.replace("；", "").strip())
            or (state == "visible" and (not answer or answer == "[无法识别]"))
        ):
            state = "unreadable"
        if state == "unreadable":
            if answer.startswith("[") and answer.endswith("]") and "无法识别" in answer:
                # Providers occasionally qualify the fixed marker (for
                # example "[手写汉字，无法识别]").  The explicit unreadable
                # state is already fail-closed, so canonicalize that harmless
                # wording variation instead of rejecting the whole paper.
                answer = "[无法识别]"
            else:
                # A provider can emit an empty or speculative transcription
                # alongside the unreadable state.  Keep the conservative state
                # and discard that text so one malformed question cannot reject
                # an otherwise complete paper or leak a guessed answer into
                # grading.
                answer = "[无法识别]"

        normalized = dict(raw_question)
        normalized["question_number"] = question_number
        normalized["student_answer"] = answer
        normalized["answer_state"] = state
        questions.append(normalized)
    return questions


def _normalize_roster_candidate_names(raw_candidates: object, roster_names: list[str]) -> list[str]:
    """Keep only unique, exact names from the current class roster.

    The vision model may suggest candidates, but it must never expand the roster
    used for automatic assignment.  Teachers retain the separate manual-create
    path when a genuinely new student appears.
    """
    allowed = {str(name).strip() for name in roster_names if str(name).strip()}
    if not allowed:
        return []
    if isinstance(raw_candidates, str):
        values = [raw_candidates]
    elif isinstance(raw_candidates, list):
        values = raw_candidates
    else:
        values = []

    candidates: list[str] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, dict):
            value = value.get("name", "")
        name = _sanitize_student_name(str(value or ""))
        if name and name in allowed and name not in seen:
            candidates.append(name)
            seen.add(name)
        if len(candidates) >= IDENTITY_ROSTER_MAX_CANDIDATES:
            break
    return candidates


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


def _multi_pool_route_keys_for_phase(
    image_path: str,
    phase_key: str,
) -> tuple[str, str, str]:
    """Derive stable anonymous routing scope from one batch-local image path."""
    resolved = os.path.realpath(image_path)
    parts = Path(resolved).parts
    lower_parts = [part.lower() for part in parts]
    batch_part = next(
        (part for part in parts if part.lower().startswith("quick_batch_")),
        "",
    )
    answer_sheet_index = next(
        (
            index
            for index, part in enumerate(lower_parts)
            if part.startswith("answer_sheet_")
        ),
        -1,
    )
    student_upload_index = next(
        (
            index
            for index, part in enumerate(lower_parts)
            if part.startswith("student_upload_")
        ),
        -1,
    )
    evidence_set_index = next(
        (
            index
            for index, part in enumerate(lower_parts)
            if part.startswith("evidence-set-")
        ),
        -1,
    )
    answer_sheet_student_batch = (
        0 <= answer_sheet_index < student_upload_index < evidence_set_index
    )
    student_paper_scope = ""
    if batch_part:
        batch_key = batch_part.lower()
        priority = "batch"
    elif answer_sheet_student_batch:
        batch_key = (
            f"{lower_parts[answer_sheet_index]}:student-recognition"
        )
        student_paper_scope = (
            f"{lower_parts[student_upload_index]}:"
            f"{lower_parts[evidence_set_index]}"
        )
        priority = "batch"
    else:
        # Template/bank/single-image operations are user-facing and use the
        # interactive scheduler priority. The raw directory is hashed by the
        # scheduler before any coordinator key is written.
        batch_key = f"interactive:{os.path.dirname(resolved)}"
        priority = "interactive"

    phase_key = str(phase_key or "").strip()
    if phase_key:
        # The scheduler hashes this value before writing coordinator keys.  A
        # phase gets its own deadline while still sharing pool, teacher, RPM,
        # circuit-breaker, and explicitly selected retry-ledger controls.
        batch_key = f"{batch_key}:phase:{phase_key}"

    filename = os.path.basename(resolved)
    filename = re.sub(r"\.identity-[0-9a-f]+\.jpg$", "", filename, flags=re.I)
    filename = re.sub(
        r"_preprocessed-[0-9a-f]{32}\.jpg$",
        "",
        filename,
        flags=re.I,
    )
    filename = re.sub(r"_preprocessed\.jpg$", "", filename, flags=re.I)
    paper_key = (
        f"{batch_key}:{student_paper_scope}:{filename}"
        if student_paper_scope
        else f"{batch_key}:{filename}"
    )
    return batch_key, paper_key, priority


def _multi_pool_route_keys(image_path: str) -> tuple[str, str, str]:
    return _multi_pool_route_keys_for_phase(image_path, _vision_phase_key.get())


def _multi_pool_response_validator(prompt: str, response_validator=None):
    """Validate JSON and, for student transcription prompts, its evidence contract."""
    strict_transcription = "answer_state" in prompt and "student_answer" in prompt
    slot_contracts = {}
    drawing_only_numbers = set()
    # Read only the frozen contract, never infer IDs from model output.
    for encoded in re.findall(r"^逐槽转录合同=(.+)$", prompt, re.MULTILINE):
        from services.template_slots import normalize_slot_ids
        mapping = json.loads(encoded)
        if not isinstance(mapping, dict):
            raise ValueError("invalid slot transcription contract")
        for number, ids in mapping.items():
            if number in slot_contracts:
                raise ValueError("duplicate slot transcription parent")
            slot_contracts[number] = normalize_slot_ids(number, ids, len(ids))
    # The page contract names pure drawing questions even though they have no
    # textual slot IDs.  Keep this narrow: only an explicit drawing type grants
    # the empty-row normalization below.
    for encoded in re.findall(r"^作答类型=(.+)$", prompt, re.MULTILINE):
        try:
            types = json.loads(encoded)
        except json.JSONDecodeError:
            # Some legacy prompt builders leave a named placeholder before
            # appending the frozen page contract; it carries no type facts.
            continue
        if isinstance(types, dict):
            drawing_only_numbers.update(
                str(number).strip() for number, kind in types.items()
                if str(kind).strip().lower() == "drawing"
            )

    def validate(content: str) -> None:
        data = parse_llm_json(content)
        if strict_transcription:
            if not isinstance(data, dict):
                raise ValueError("student transcription must be a JSON object")
            questions = validate_visual_transcriptions(
                data.get("questions", []),
                slot_contracts=slot_contracts,
                drawing_only_numbers=drawing_only_numbers,
            )
            if not questions:
                raise ValueError("student transcription returned no questions")
            actual_qns = [
                str(item.get("question_number") or "").strip()
                for item in questions
            ]
            if len(actual_qns) != len(set(actual_qns)):
                raise ValueError(
                    "student transcription returned duplicate question numbers"
                )

            # Frozen-template prompts carry an explicit machine-readable order.
            # Missing, extra, or reordered questions must retry inside the same
            # scheduler request rather than becoming a downstream batch failure.
            match = re.search(r"question_numbers=([^\n]+)", prompt)
            if match:
                raw_expected = match.group(1).strip()
                try:
                    expected_value = json.loads(raw_expected)
                except json.JSONDecodeError:
                    expected_value = [
                        item.strip().strip("'\"")
                        for item in raw_expected.strip("[]").split(",")
                        if item.strip()
                    ]
                expected_qns = [str(item).strip() for item in expected_value]
                if actual_qns != expected_qns:
                    raise ValueError("student transcription question order mismatch")

        if response_validator is not None:
            return response_validator(content)
        return None

    return validate


def _get_template_ocr_scheduler_for_route(pool_route: str):
    """Resolve the qwen3.8-max template scheduler without breaking test getters.

    A few integrations still inject a zero-argument getter; signature
    inspection keeps those callers compatible without swallowing a real
    construction error from the production getter.
    """
    from services.ocr_pool_scheduler import get_template_ocr_pool_scheduler

    route = str(pool_route or "fallback").strip().lower()
    if route in {"qwen", "qwen3.8max"}:
        route = "fallback"
    try:
        parameters = inspect.signature(get_template_ocr_pool_scheduler).parameters
    except (TypeError, ValueError):
        parameters = None
    if parameters is not None and (
        "pool_route" not in parameters
        and not any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
    ):
        return get_template_ocr_pool_scheduler()
    return get_template_ocr_pool_scheduler(pool_route=route)


def _accepts_keyword(callable_obj, name: str) -> bool:
    """Check an optional keyword without breaking legacy injected schedulers."""
    try:
        parameters = inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        return True
    return name in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def _set_template_route_diagnostics(
    diagnostics: dict[str, object] | None,
    *,
    route: str,
    reasoning_effort: str,
) -> None:
    if not isinstance(diagnostics, dict):
        return
    diagnostics["actual_route"] = str(route)
    diagnostics["actual_reasoning_effort"] = str(reasoning_effort)


async def _call_multi_pool_vision_request(
    image_path: str | list[str],
    prompt: str,
    max_tokens: int = 6000,
    *,
    pool_route: str | None = None,
    response_validator=None,
    prompt_first: bool = False,
    reasoning_effort: str | None = None,
    scheduler=None,
    image_handoff_callback=None,
    image_settle_callbacks=(),
    cancel_on_waiter_cancel: bool = False,
    route_diagnostics: dict[str, object] | None = None,
    json_object: bool = False,
) -> str:
    """Send one template request through the qwen3.8-max Token Plan scheduler."""
    image_paths = [str(image_path)] if isinstance(image_path, str) else [
        str(path) for path in image_path
    ]
    try:
        output_token_limit = int(max_tokens)
        if output_token_limit < 1:
            raise ValueError("max_tokens must be positive")
    except (TypeError, ValueError) as exc:
        raise ValueError("max_tokens must be an integer") from exc
    if not isinstance(json_object, bool):
        raise ValueError("json_object must be a boolean")

    def encode_images():
        return [_encode_vision_image(path) for path in image_paths]

    encoded_images = await _run_local_ocr_image_component(
        encode_images,
        semaphore=_get_local_ocr_preprocess_semaphore(),
        on_handoff=image_handoff_callback,
        settle_callbacks=image_settle_callbacks,
    )
    image_content = [
        {
            "type": "input_image",
            "image_url": f"data:{mime};base64,{b64}",
            "detail": "high",
        }
        for b64, mime in encoded_images
    ]
    text_content = {"type": "input_text", "text": prompt}
    content = (
        [text_content, *image_content]
        if prompt_first
        else [*image_content, text_content]
    )
    from config import OCR_TEMPLATE_GPT_REASONING_EFFORT
    selected_reasoning_effort = str(
        reasoning_effort or OCR_TEMPLATE_GPT_REASONING_EFFORT
    ).strip().lower()
    if selected_reasoning_effort not in {
        "none", "minimal", "low", "medium", "high", "xhigh", "max"
    }:
        raise ValueError(
            "reasoning_effort must be none, minimal, low, medium, high, xhigh, or max"
        )

    payload = {
        "input": [{
            "role": "user",
            "content": content,
        }],
        "max_output_tokens": output_token_limit,
        "stream": True,
        "store": False,
    }
    # Internal scheduler metadata. It is consumed when constructing the
    # provider request and removed before the HTTP body is sent. Template GPT
    # calls always carry the canonical default explicitly, even when callers
    # omit ``reasoning_effort``.
    payload["model_reasoning_effort"] = selected_reasoning_effort
    if json_object:
        # Standard Responses text.format. The scheduler's Qwen chat adapter
        # converts it to the native ``response_format`` field of the provider
        # body, so the opt-in survives the internal Responses-shaped payload
        # instead of being dropped at the pool boundary.
        payload["text"] = {"format": {"type": "json_object"}}
    del encoded_images
    route_path = image_paths[0]
    _, paper_key, _ = _multi_pool_route_keys(route_path)
    ordinal = _vision_route_ordinal.get()
    if ordinal is not None:
        paper_key = f"{paper_key}-route-{ordinal}"
    # Only the qwen3.8-max Token Plan route remains; the GPT canonical and
    # YS/exemption routes are retired and fail closed in the scheduler.
    pool_route = str(pool_route if pool_route is not None else "fallback").strip().lower()
    if pool_route in {"qwen", "qwen3.8max"}:
        pool_route = "fallback"
    if pool_route != "fallback":
        raise ValueError(
            f"template pool_route {pool_route!r} is retired; only 'fallback' (qwen3.8-max) remains"
        )
    if not isinstance(route_diagnostics, dict):
        route_diagnostics = None
    if route_diagnostics is not None:
        route_diagnostics["requested_route"] = pool_route
        route_diagnostics["requested_reasoning_effort"] = selected_reasoning_effort
    scheduler = scheduler or _get_template_ocr_scheduler_for_route(pool_route)
    if route_diagnostics is not None:
        route_diagnostics["_scheduler"] = scheduler
    validator = _multi_pool_response_validator(
        prompt, response_validator=response_validator,
    )
    fallback_kwargs = {
        "request_key": paper_key,
        "validator": validator,
    }
    fallback_effort = "high"
    if route_diagnostics is not None and _accepts_keyword(
        scheduler.request_template_fallback_pool, "route_diagnostics"
    ):
        fallback_kwargs["route_diagnostics"] = route_diagnostics
    _set_template_route_diagnostics(
        route_diagnostics,
        route="qwen3.8max",
        reasoning_effort=fallback_effort,
    )
    return await scheduler.request_template_fallback_pool(
        payload,
        **fallback_kwargs,
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


async def call_gpt_vision(
    image_path: str | list[str],
    prompt: str,
    max_tokens: int = OCR_TEMPLATE_GPT_MAX_OUTPUT_TOKENS,
    *,
    pool_route: str | None = None,
    response_validator=None,
    prompt_first: bool = False,
    reasoning_effort: str | None = None,
    cancel_on_waiter_cancel: bool = False,
    route_diagnostics: dict[str, object] | None = None,
    json_object: bool = False,
) -> str:
    """Send teacher-template images to the qwen3.8-max Token Plan route."""
    paths = [image_path] if isinstance(image_path, str) else list(image_path)
    if not 1 <= len(paths) <= 12 or any(not isinstance(path, str) for path in paths) or len(set(paths)) != len(paths):
        raise ValueError("Template vision requires 1-12 unique local image paths")
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
            return await _call_multi_pool_vision_request(
                image_path,
                prompt,
                max_tokens=max_tokens,
                pool_route=pool_route,
                response_validator=response_validator,
                prompt_first=prompt_first,
                reasoning_effort=reasoning_effort,
                cancel_on_waiter_cancel=cancel_on_waiter_cancel,
                route_diagnostics=route_diagnostics,
                json_object=json_object,
                image_handoff_callback=mark_handed_off,
                image_settle_callbacks=(release_handed_off_budget,),
            )
        finally:
            if not handed_off:
                await payload_budget.release(reserved_bytes)


async def call_template_max_text(
    messages: list[dict],
    *,
    max_tokens: int = 32768,
    response_validator=None,
) -> str:
    """Send one text-only JSON request on the qwen3.8-max Token Plan route.

    The request shares the template Max in-flight gate with directory/fullsolve
    and always uses reasoning=high; it carries no image payload.
    """
    payload = {
        "input": [
            {"role": str(message["role"]), "content": str(message["content"])}
            for message in messages
        ],
        "max_output_tokens": int(max_tokens),
        "stream": True,
        "store": False,
        "model_reasoning_effort": "high",
        "text": {"format": {"type": "json_object"}},
    }
    request_key = "max-text-" + hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:32]
    scheduler = _get_template_ocr_scheduler_for_route("fallback")

    def validate(content: str) -> None:
        parse_llm_json(content)
        if response_validator is not None:
            response_validator(content)

    return await scheduler.request_template_fallback_pool(
        payload,
        request_key=request_key,
        validator=validate,
    )


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


async def _prepare_vision_image(
    image_path: str,
    *,
    max_edge: int | None = None,
) -> str:
    """Run CPU/image work in a bounded thread pool, never on Uvicorn's loop."""
    effective_max_edge = OCR_PREPROCESS_MAX_EDGE if max_edge is None else max_edge
    # The browser contract is fixed at 1920px and already includes EXIF
    # normalization, review-ink suppression and JPEG encoding. Validate only
    # its bounded headers, regardless of the optional server preprocess mode.
    if await asyncio.to_thread(
        is_ocr_ready_image,
        image_path,
        max_edge=1920,
    ):
        return image_path

    def _preprocess() -> str:
        # Keep small test/integration adapters that still expose the historical
        # one-argument helper working while production uses explicit policy.
        try:
            params = inspect.signature(preprocess_paper_image).parameters
        except (TypeError, ValueError):
            params = {}
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in params.values()
        )
        kwargs = {}
        if "mode" in params or accepts_kwargs:
            kwargs["mode"] = OCR_PREPROCESS_MODE
        if "max_edge" in params or accepts_kwargs:
            kwargs["max_edge"] = effective_max_edge
        if "suppress_review_ink" in params or accepts_kwargs:
            kwargs["suppress_review_ink"] = True
        return preprocess_paper_image(image_path, **kwargs)

    return await _run_local_ocr_image_component(_preprocess)


def _cleanup_prepared_image(image_path: str, processed_path: str) -> None:
    """Delete only the sibling generated by preprocess_paper_image."""
    source = Path(image_path).resolve()
    processed = Path(processed_path).resolve()
    expected_prefix = f"{source.stem}_preprocessed-"
    if (
        processed.parent != source.parent
        or not processed.name.startswith(expected_prefix)
        or not processed.name.endswith(".jpg")
        or len(processed.name) != len(expected_prefix) + 32 + len(".jpg")
    ):
        return
    try:
        processed.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Failed to remove temporary OCR image %s: %s", processed_path, exc)


async def _recognize_paper_limited(image_path: str, teacher_id: str) -> dict:
    """Separate CPU-heavy preprocessing from the wider request-memory window."""
    processed_path = await _prepare_vision_image(image_path)
    try:
        return await _recognize_paper_preprocessed(processed_path, teacher_id)
    finally:
        _cleanup_prepared_image(image_path, processed_path)


def classify_ocr_exception(error: BaseException) -> tuple[bool, str]:
    """Classify whether a later explicit batch recovery may try once more."""
    if isinstance(error, LocalImagePipelineStalled):
        return False, "local_image_pipeline_stalled"
    if isinstance(error, httpx.HTTPStatusError):
        status = int(error.response.status_code)
        if status in {401, 403}:
            return True, "credential_unavailable"
        if status == 408:
            return True, "provider_timeout"
        if status == 429:
            return True, "provider_rate_limited"
        if status >= 500:
            return True, "provider_unavailable"
        return False, "provider_rejected_request"
    if isinstance(error, httpx.TimeoutException):
        return True, "provider_timeout"
    if isinstance(error, httpx.TransportError):
        return True, "provider_transport_error"

    from services.ocr_pool_scheduler import OcrPoolError

    if isinstance(error, OcrPoolError):
        status = error.status_code
        if status in {401, 403}:
            return True, "credential_unavailable"
        if status == 408:
            return True, "provider_timeout"
        if status == 429:
            return True, "provider_rate_limited"
        if status is not None and status >= 500:
            return True, "provider_unavailable"
        code = str(error).lower()
        retryable = bool(
            error.retryable
            or error.transport_failure
            or any(
                marker in code
                for marker in (
                    "deadline",
                    "budget_exhausted",
                    "limit_reached",
                    "circuit",
                    "unavailable",
                    "busy",
                )
            )
        )
        return retryable, (
            "provider_transient_failure" if retryable else "provider_request_failed"
        )
    if isinstance(error, TimeoutError):
        # Rate-budget, shared-capacity and payload-budget waits all expire as
        # TimeoutError while the batch is saturated. That is a transient
        # admission state, not evidence that this page is unreadable.
        return True, "provider_capacity_timeout"
    if isinstance(error, (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError)):
        # The template evidence route sends one request without an inner
        # retry, so a malformed or contract-violating response must keep the
        # page's one durable recovery attempt (as the full-page route does).
        return True, "invalid_provider_response"
    if isinstance(error, RuntimeError):
        # Empty content, ignored thinking flags and an exhausted key pool are
        # raised as RuntimeError by the vision request loop.
        return True, "provider_transient_failure"
    return False, "local_processing_failed"


def ocr_failure_result(
    error: BaseException,
    *,
    message: str | None = None,
    retryable: bool | None = None,
    error_code: str | None = None,
) -> dict:
    classified_retryable, classified_code = classify_ocr_exception(error)
    info = report_failure(error, stage="transcription")
    return {
        "questions": [],
        "student_name": "",
        "student_no": "",
        "error": info["message"],
        "error_info": info,
        "_ocr_retryable": (
            classified_retryable if retryable is None else bool(retryable)
        ),
        "_ocr_error_code": str(error_code or classified_code)[:80],
    }


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


async def recognize_paper_with_cache(
    image_path: str,
    batch_id: str,
    cache,
    teacher_id: str = "",
    paper_id: str = "",
) -> dict:
    """Recognize one student page against the batch's frozen template.

    ``cache`` is the batch's ``PaperCache``: it routes the page to a confirmed
    template page and transcribes only the student's answers. The result has
    the shape of ``recognize_paper``.
    """
    token = set_vision_teacher_id(teacher_id)
    try:
        return await cache.recognize_page(
            image_path,
            teacher_id=teacher_id,
            batch_id=batch_id,
            paper_id=paper_id,
        )
    finally:
        reset_vision_teacher_id(token)


def _build_identity_crop(image_path: str) -> str:
    """Build the full-resolution identity composite outside the event loop."""
    from PIL import Image, ImageEnhance, ImageOps

    crop_path = f"{image_path}.identity-{uuid.uuid4().hex}.jpg"
    temp_path = f"{crop_path}.{uuid.uuid4().hex}.tmp"
    try:
        with Image.open(image_path) as source:
            _validate_source_dimensions(source)
            oriented = ImageOps.exif_transpose(source)
            try:
                image = oriented.convert("RGB")
            finally:
                if oriented is not source:
                    oriented.close()
        try:
            width, height = image.size
            top_height = min(height, max(1, int(height * IDENTITY_TOP_CROP_RATIO)))
            left_width = min(width, max(1, int(width * IDENTITY_LEFT_CROP_RATIO)))

            header = image.crop((0, 0, width, top_height))
            left = image.crop((0, 0, left_width, height)).transpose(
                Image.Transpose.ROTATE_270
            )
            try:
                composite = Image.new(
                    "RGB",
                    (max(header.width, left.width), header.height + left.height),
                    "white",
                )
                try:
                    composite.paste(header, (0, 0))
                    composite.paste(left, (0, header.height))
                    enhanced = ImageEnhance.Contrast(composite).enhance(1.25)
                    try:
                        enhanced.save(
                            temp_path,
                            "JPEG",
                            quality=95,
                            subsampling=0,
                        )
                    finally:
                        enhanced.close()
                finally:
                    composite.close()
            finally:
                header.close()
                left.close()
        finally:
            image.close()
        os.replace(temp_path, crop_path)
        return crop_path
    except Exception:
        for path in (temp_path, crop_path):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning("Failed to clean identity crop %s", path)
        raise


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
