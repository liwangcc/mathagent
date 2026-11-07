"""Local image work around a vision call: bounded threads, stall handoff, and memory budgets.

- Native image components (rotate, deskew, crops) run in threads Python cannot
  cancel.  ``_run_local_ocr_image_component`` bounds the business wait
  (``_LOCAL_IMAGE_COMPONENT_TIMEOUT_SECONDS``) and raises
  ``LocalImagePipelineStalled`` but keeps its gate until the thread really
  exits (``_run_local_image_thread``, ``_settle_local_image_thread``); a stalled
  thread hands off to a shared fault marker (``_handoff_stalled_image_thread``)
  that ``get_local_image_readiness`` reports (``_fresh_shared_image_stall_markers``).
- Per-worker gates, each created for the running event loop: request memory
  (``_get_local_ocr_request_semaphore``), CPU preprocessing
  (``_get_local_ocr_preprocess_semaphore``), full-resolution identity crops
  (``_get_local_identity_preprocess_semaphore``), and the resident request-body
  budget (``_get_local_ocr_payload_budget``, ``_AsyncByteBudget``), sized with
  ``_estimated_vision_payload_bytes``.

The gates and their event loops are module-level state, set only by the getter
functions here.

Moved unchanged from services/ocr_service.py, which still exposes these names.
"""

import asyncio
import inspect
import logging
import math
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from config import (
    OCR_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES,
    OCR_LOCAL_MAX_CONCURRENT,
    OCR_PREPROCESS_LOCAL_MAX_CONCURRENT,
    UPLOAD_DIR,
)

# services.ocr_service's logger, so these records keep their logger name.
logger = logging.getLogger("services.ocr_service")


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

_local_ocr_request_semaphore: asyncio.Semaphore | None = None
_local_ocr_preprocess_semaphore: asyncio.Semaphore | None = None
_local_identity_preprocess_semaphore: asyncio.Semaphore | None = None
_local_ocr_payload_budget = None
_local_ocr_request_loop: asyncio.AbstractEventLoop | None = None
_local_ocr_preprocess_loop: asyncio.AbstractEventLoop | None = None
_local_identity_preprocess_loop: asyncio.AbstractEventLoop | None = None
_local_ocr_payload_budget_loop: asyncio.AbstractEventLoop | None = None


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
