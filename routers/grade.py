"""批改接口 — 支持日常作业/考试两类导入 + 批量批改"""

import asyncio
import copy
import hashlib
import json as _json
import os
import re
import uuid
import logging
from contextlib import aclosing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from anyio import CancelScope
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db, SessionLocal
from services.processing_errors import current_correlation_id, report_failure
from services.teaching_context import (
    await_teaching,
    require_teacher_stage,
    stream_teaching,
    validate_teaching_context,
)
from services.grader import (
    GradingModelUnavailable,
    grade_paper_unified,
)
from services.ocr_service import (
    ocr_failure_result,
    recognize_paper_with_cache,
    recognize_student_info_with_roster,
    reset_vision_batch_paper_count,
    reset_vision_phase_key,
    set_vision_batch_paper_count,
    set_vision_phase_key,
)
from services.rate_limiter import RateLimiter
from services.bounded_dispatch import (
    bounded_completion_map,
)
from services.paper_cache import PaperCache
from services.security import (
    stream_to_disk_capped,
    validate_upload_path,
)
from services.system_readiness import (
    READINESS_IO_TIMEOUT_SECONDS,
    get_public_hardware_readiness,
    get_upload_storage_readiness_async,
)
from services.preprocess import get_display_image_path, ocr_ready_storage_suffix
from services.review_marks import align_template_review_details, detail_review_boxes, normalize_review_boxes, render_review_marks, review_cache_name
from services.paper_orientation import orientation_is_ready, sha256_file
from services.analyzer import (
    reset_grading_teacher_id,
    set_grading_teacher_id,
)
from services.quick_grader import (
    OCR_JOB_TERMINAL_STATES,
    OCR_MAX_JOB_ATTEMPTS,
    BatchOperationBusy,
    QuickBatch,
    batch_ocr_summary,
    batch_operation_lock,
    finalize_exhausted_ocr_jobs,
    is_accelerator_pause_result,
    mark_ocr_outcome,
    ocr_job_attempts,
    queued_recognition_uploads,
    requeue_interrupted_ocr_jobs,
    requeue_retryable_ocr_failures,
    sync_pending_recognition_uploads,
)
from services.template_session import TemplateSession, normalize_template_mode
from services.template_evidence import (
    TemplateEvidenceError,
    normalize_template_handwriting_regions,
    verify_template_artifacts,
)
from services.grading_persistence import image_content_hash
from services.batch_report import build_batch_report_payload, generate_batch_report
from services import template_knowledge
from services.flash_grading import FLASH_LOCAL_MAX_CONCURRENT
from services.grading_contract import (
    AnswerSpec,
    VALID_GRADING_CATEGORIES,
    finalize_answer_spec_for_category,
)
from services.collection_identity import parse_source_paths, resolve_collection_identity
from services.identity_inheritance import apply_identity_inheritance, inheritance_upload_order_key
from services.student_service import canonical_student_no
from models import Student
from models_user import User
from routers.auth import _require_user
from config import (
    MAX_UPLOAD_FILES,
    MAX_UPLOAD_FILE_SIZE,
    MAX_UPLOAD_TOTAL_SIZE,
    OCR_PREPROCESS_LOCAL_MAX_CONCURRENT,
    UPLOAD_DIR,
)
logger = logging.getLogger(__name__)
router = APIRouter()
_GENERIC_GRADE_ERROR = "批改处理失败，请稍后重试"
_RAW_PAPER_EXPIRED_MESSAGE = "原卷已按隐私保留策略清理，错题、得分和正确率记录仍保留"
_GRADE_HEARTBEAT_SECONDS = 15.0
# Reserve nine minutes for the text-only Flash grading stage after the
# six-minute OCR target; anything unfinished remains resumable rather than
# keeping the SSE request alive indefinitely.
_GRADE_BATCH_DEADLINE_S = 540.0
_batch_report_tasks: dict[str, asyncio.Task] = {}


_quick_batch_meta_io_semaphore: asyncio.Semaphore | None = None
_quick_batch_meta_io_loop: asyncio.AbstractEventLoop | None = None


# Resumable scheduling pauses, not failures: they must not mint an error id or
# be logged as processing.failed (each accelerator wait used to add one).
_SSE_PAUSE_CODES = {"ACCELERATOR_WAITING", "ACCELERATOR_UNAVAILABLE"}


def _sse(event: str, payload: dict) -> str:
    if (
        event == "error"
        and not payload.get("error_info")
        and str(payload.get("code") or "") not in _SSE_PAUSE_CODES
    ):
        error = RuntimeError(str(payload.get("message") or "processing failed"))
        error.code = str(payload.get("code") or "")
        stage = "template" if "TEMPLATE" in error.code else "grading"
        payload = {**payload, "error_info": report_failure(error, stage=stage)}
    if current_correlation_id():
        payload = {"correlation_id": current_correlation_id(), **payload}
    return f"event: {event}\ndata: {_json.dumps(payload, ensure_ascii=False)}\n\n"


async def _settle_template_component(
    task: asyncio.Task,
    *,
    cancel_running: bool,
) -> None:
    """Cancel when possible and do not return until the component is settled."""
    # StreamingResponse cancels an AnyIO scope: asyncio.shield alone cannot
    # protect the waiter from level cancellation (including the final gather).
    with CancelScope(shield=True):
        if cancel_running and not task.done():
            task.cancel()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # Repeated direct asyncio cancellation must not strand cleanup.
                continue
            except BaseException:
                break
        # The task is done: retrieve its exception synchronously so even a
        # second direct Task.cancel() cannot interrupt a final gather checkpoint.
        if not task.cancelled():
            task.exception()


def _get_quick_batch_meta_io_semaphore() -> asyncio.Semaphore:
    """Serialize large QuickBatch snapshots per worker to avoid disk thrash."""
    global _quick_batch_meta_io_loop, _quick_batch_meta_io_semaphore
    loop = asyncio.get_running_loop()
    if (
        _quick_batch_meta_io_semaphore is None
        or _quick_batch_meta_io_loop is not loop
    ):
        _quick_batch_meta_io_loop = loop
        _quick_batch_meta_io_semaphore = asyncio.Semaphore(1)
    return _quick_batch_meta_io_semaphore


async def _save_quick_batch_meta(batch: QuickBatch) -> None:
    """Write one atomic batch checkpoint without blocking SSE heartbeats."""
    semaphore = _get_quick_batch_meta_io_semaphore()
    async with semaphore:
        # Snapshot on the event-loop thread: pipeline tasks keep mutating the
        # live batch while the worker thread writes the file.
        snapshot = batch.serialize_meta()
        task = asyncio.create_task(asyncio.to_thread(batch.save_meta, snapshot=snapshot))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # The thread cannot be cancelled. Keep the batch operation lock until
            # its atomic replace has settled, then propagate the disconnect.
            await _settle_template_component(task, cancel_running=False)
            raise


async def _checkpoint_recognition_cleanup(batch: QuickBatch, pipeline) -> None:
    """Settle grading before the final snapshot, while the caller holds its lock.

    Keep cleanup in a joined task to survive repeated asyncio cancellation;
    _settle_template_component also shields the AnyIO disconnect scope. Context
    tokens and the DB/operation lock still belong to the calling stream task.
    """
    async def cleanup():
        if pipeline is not None:
            await pipeline.aclose()
        requeue_interrupted_ocr_jobs(batch)
        sync_pending_recognition_uploads(batch)
        await _save_quick_batch_meta(batch)

    task = asyncio.create_task(cleanup())
    await _settle_template_component(task, cancel_running=False)
    # The settle helper retrieves exceptions; this caller must not hide I/O or
    # business failures behind what looks like a successful checkpoint.
    task.result()


def _batch_report_response(batch: QuickBatch) -> dict:
    return {
        "status": str(batch.report_status or "not_started"),
        "text": str(batch.report_text or ""),
        "stats": batch.report_stats if isinstance(batch.report_stats, dict) else {},
        "error": str(batch.report_error or ""),
        "updated_at": str(batch.report_updated_at or ""),
    }


def _batch_knowledge_template(batch: QuickBatch, teacher_id: int):
    if not str(batch.template_id or "").strip():
        return None
    sess = _load_owned_template_session(batch.template_id, teacher_id)
    if (sess.teacher_id != teacher_id or sess.status not in {"confirmed", "frozen"}
            or sess.recognition_contract_error()
            or sess.teaching_context != batch.teaching_context
            or (batch.template_contract_hash and batch.template_contract_hash != sess.confirmed_contract_hash)):
        raise ValueError("模板知识点版本与本批次不一致")
    return sess


def _refresh_batch_knowledge(batch: QuickBatch, teacher_id: int) -> dict:
    """Only labels are mutable here; callers own the ordinary batch save lock."""
    try:
        sess = _batch_knowledge_template(batch, teacher_id)
        if sess is None:
            return {"status": "ready", "error": ""}
        template_knowledge.reconcile_batch_knowledge(batch, sess)
        return template_knowledge.knowledge_state(sess)
    except Exception as exc:
        logger.warning("批次知识点刷新暂缓 batch=%s: %s", batch.batch_id, type(exc).__name__)
        return {"status": "failed", "error": "知识点分类暂未完成，请稍后重试报告；已有成绩不受影响"}


def _reconcile_batch_report_sync(batch_id: str, teacher_id: int):
    with batch_operation_lock(batch_id, UPLOAD_DIR):
        batch = QuickBatch.load_meta(batch_id, UPLOAD_DIR)
        if batch.teacher_id != teacher_id:
            raise ValueError("batch report owner changed")
        state = _refresh_batch_knowledge(batch, teacher_id)
        payload = build_batch_report_payload(batch)
        batch.report_stats = payload["stats"]
        batch.report_input_hash = payload["input_hash"]
        batch.report_status = "generating" if state["status"] == "ready" else (
            "queued" if state["status"] == "pending" else "failed")
        batch.report_text = ""
        batch.report_error = state["error"]
        batch.report_updated_at = datetime.now(timezone.utc).isoformat()
        batch.save_meta()
        return payload, state


async def _prepare_batch_report_knowledge(batch_id: str, teacher_id: int):
    batch = await asyncio.to_thread(QuickBatch.load_meta, batch_id, UPLOAD_DIR)
    try:
        sess = _batch_knowledge_template(batch, teacher_id)
        if sess is not None:
            await template_knowledge.ensure_session_knowledge(
                sess, classifier=_template_knowledge_classifier,
            )
    except Exception as exc:
        logger.warning("批次知识点分类暂缓 batch=%s: %s", batch_id, type(exc).__name__)
    # Grading may still hold the batch lock while finishing its SSE. Do not
    # perform provider work again while waiting for that narrow commit window.
    for attempt in range(60):
        try:
            return await asyncio.to_thread(_reconcile_batch_report_sync, batch_id, teacher_id)
        except BatchOperationBusy:
            if attempt == 59:
                raise
            await asyncio.sleep(0.1)


def _finish_batch_report_sync(
    batch_id: str,
    expected_input_hash: str,
    text: str,
    provider_note: str,
) -> None:
    """Commit a report only if the review data did not change while generating."""
    with batch_operation_lock(batch_id, UPLOAD_DIR):
        batch = QuickBatch.load_meta(batch_id, UPLOAD_DIR)
        state = _refresh_batch_knowledge(batch, batch.teacher_id)
        current = build_batch_report_payload(batch)
        if state["status"] != "ready":
            batch.report_status = "queued" if state["status"] == "pending" else "failed"
            batch.report_text = ""
            batch.report_stats = current["stats"]
            batch.report_error = state["error"]
        elif current["input_hash"] != expected_input_hash:
            batch.report_status = "queued"
            batch.report_text = ""
            batch.report_stats = current["stats"]
            batch.report_error = "批改结果已更新，正在重新生成报告"
        else:
            batch.report_status = "ready"
            batch.report_text = str(text or "")
            batch.report_stats = current["stats"]
            batch.report_error = "；".join(note for note in (state["error"], str(provider_note or "")) if note)
        batch.report_input_hash = current["input_hash"]
        batch.report_updated_at = datetime.now(timezone.utc).isoformat()
        batch.save_meta()


def _fail_batch_report_sync(batch_id: str, expected_input_hash: str, error: str) -> None:
    with batch_operation_lock(batch_id, UPLOAD_DIR):
        batch = QuickBatch.load_meta(batch_id, UPLOAD_DIR)
        current = build_batch_report_payload(batch)
        if current["input_hash"] != expected_input_hash:
            batch.report_status = "queued"
            batch.report_error = "批改结果已更新，等待重新生成报告"
        else:
            batch.report_status = "failed"
            batch.report_error = str(error or "报告生成失败，请稍后重试")[:300]
        batch.report_input_hash = current["input_hash"]
        batch.report_stats = current["stats"]
        batch.report_updated_at = datetime.now(timezone.utc).isoformat()
        batch.save_meta()


async def _run_batch_report_generation(
    batch_id: str,
    teacher_id: int,
    expected_input_hash: str,
    stats: dict,
) -> None:
    try:
        payload, state = await _prepare_batch_report_knowledge(batch_id, teacher_id)
        if state["status"] != "ready":
            return
        expected_input_hash, stats = payload["input_hash"], payload["stats"]
        text, provider_note = await generate_batch_report(stats, teacher_id=teacher_id)
        await asyncio.to_thread(
            _finish_batch_report_sync,
            batch_id,
            expected_input_hash,
            text,
            provider_note,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("batch report generation failed batch=%s: %s", batch_id, exc, exc_info=True)
        try:
            await asyncio.to_thread(
                _fail_batch_report_sync,
                batch_id,
                expected_input_hash,
                "报告生成失败，请稍后重试",
            )
        except Exception:
            logger.exception("failed to persist batch report failure batch=%s", batch_id)


def _discard_batch_report_task(task: asyncio.Task, batch_id: str) -> None:
    if _batch_report_tasks.get(batch_id) is task:
        _batch_report_tasks.pop(batch_id, None)


async def _start_batch_report_generation(batch: QuickBatch, teacher_id: int) -> None:
    batch_id = str(batch.batch_id)
    existing = _batch_report_tasks.get(batch_id)
    if existing is not None and not existing.done():
        return
    payload = build_batch_report_payload(batch)
    batch.report_status = "generating"
    batch.report_text = ""
    batch.report_stats = payload["stats"]
    batch.report_error = ""
    batch.report_input_hash = payload["input_hash"]
    batch.report_updated_at = datetime.now(timezone.utc).isoformat()
    await _save_quick_batch_meta(batch)
    task = asyncio.create_task(
        _run_batch_report_generation(
            batch_id,
            int(teacher_id),
            payload["input_hash"],
            payload["stats"],
        ),
        name=f"batch-report-{batch_id}",
    )
    _batch_report_tasks[batch_id] = task
    task.add_done_callback(lambda done: _discard_batch_report_task(done, batch_id))


async def _ensure_batch_report_generation(batch: QuickBatch, teacher_id: int) -> QuickBatch:
    state = _refresh_batch_knowledge(batch, teacher_id)
    payload = build_batch_report_payload(batch)
    existing = _batch_report_tasks.get(str(batch.batch_id))
    report_is_current = (
        str(batch.report_input_hash or "") == payload["input_hash"]
        and str(batch.report_status or "") == "ready"
        and state["status"] == "ready"
    )
    if not report_is_current and (existing is None or existing.done()):
        # Reload under the review lock, never save a request's stale snapshot
        # over a teacher decision while refreshing derived report metadata.
        try:
            with batch_operation_lock(batch.batch_id, UPLOAD_DIR):
                batch = QuickBatch.load_meta(batch.batch_id, UPLOAD_DIR)
                _refresh_batch_knowledge(batch, teacher_id)
                await _start_batch_report_generation(batch, teacher_id)
        except BatchOperationBusy:
            pass
        batch = QuickBatch.load_meta(batch.batch_id, UPLOAD_DIR)
    return batch


def _jpeg_dimensions(prefix: bytes) -> tuple[int, int] | None:
    """Read JPEG dimensions from bounded headers without decoding pixels."""
    if not prefix.startswith(b"\xff\xd8\xff"):
        return None
    offset = 2
    size = len(prefix)
    while offset + 9 <= size:
        if prefix[offset] != 0xFF:
            offset += 1
            continue
        while offset < size and prefix[offset] == 0xFF:
            offset += 1
        if offset >= size:
            break
        marker = prefix[offset]
        offset += 1
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > size:
            break
        segment_length = int.from_bytes(prefix[offset:offset + 2], "big")
        if segment_length < 2 or offset + segment_length > size:
            break
        if marker in {
            0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
            0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
        }:
            if segment_length < 7:
                return None
            height = int.from_bytes(prefix[offset + 3:offset + 5], "big")
            width = int.from_bytes(prefix[offset + 5:offset + 7], "big")
            return width, height
        offset += segment_length
    return None


def _validate_client_ocr_ready_upload(filename: str, path: str) -> dict:
    """Validate the browser JPEG contract using headers and bounded reads only."""
    safe_name = os.path.basename(str(filename or ""))
    if not safe_name.lower().endswith(".ocr-ready-v1.jpg"):
        return {"safe": False, "reason": "缺少 .ocr-ready-v1.jpg 客户端规范化合同", "sanitized_filename": safe_name}
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as stream:
            prefix = stream.read(64 * 1024)
            stream.seek(max(0, size - 2))
            suffix = stream.read(2)
    except OSError:
        return {"safe": False, "reason": "文件内容为空", "sanitized_filename": safe_name}
    if size <= 0 or size > MAX_UPLOAD_FILE_SIZE:
        return {"safe": False, "reason": "文件超过大小限制", "sanitized_filename": safe_name}
    if not prefix.startswith(b"\xff\xd8\xff") or suffix != b"\xff\xd9":
        return {"safe": False, "reason": "客户端规范化文件不是完整 JPEG", "sanitized_filename": safe_name}
    dimensions = _jpeg_dimensions(prefix)
    if not dimensions or max(dimensions) > 1920 or dimensions[0] <= 0 or dimensions[1] <= 0:
        return {"safe": False, "reason": "客户端规范化 JPEG 尺寸合同无效", "sanitized_filename": safe_name}
    return {"safe": True, "reason": "", "sanitized_filename": safe_name}


def _client_ocr_ready_orientation(filename: str) -> dict:
    """Return the browser canonicalization checkpoint without touching pixels.

    ``.ocr-ready-v1.jpg`` is emitted only after the browser has applied EXIF
    orientation and bounded the canvas dimensions.  The server deliberately
    trusts only this explicit contract; it never guesses orientation locally.
    """
    if str(filename or "").lower().endswith(".ocr-ready-v1.jpg"):
        return {
            "orientation_status": "auto_passed",
            "orientation_reason": "client_ocr_ready_v1",
            "orientation_confidence": 1.0,
            "detected_tilt_degrees": 0.0,
            "exif_orientation": 1,
            "orientation_coordinate_space": "client_normalized_pixels",
        }
    return {
        "orientation_status": "needs_review",
        "orientation_reason": "client_canonicalization_required",
        "orientation_confidence": 0.0,
        "detected_tilt_degrees": None,
        "exif_orientation": None,
        "orientation_coordinate_space": "client_normalized_pixels",
    }


def _hash_saved_uploads(original_path: str, working_path: str) -> tuple[str, str]:
    """Hash source and working artifacts only; do not decode either image."""
    source_hash = sha256_file(original_path)
    working_hash = (
        source_hash
        if os.path.realpath(original_path) == os.path.realpath(working_path)
        else sha256_file(working_path)
    )
    return source_hash, working_hash


async def _ensure_batch_upload_inspected(batch: QuickBatch, upload: dict) -> dict:
    """Lazily inspect one upload after SSE has published its durable batch id."""
    orientation = _orientation_diagnostics(upload)
    if upload.get("source_sha256") and upload.get("working_sha256") and orientation:
        return orientation

    working_path = str(upload.get("image_path") or "")
    original_path = str(upload.get("original_image_path") or working_path)
    source_hash, working_hash = await asyncio.to_thread(
        _hash_saved_uploads,
        original_path,
        working_path,
    )
    orientation = _client_ocr_ready_orientation(
        str(upload.get("filename") or working_path)
    )
    upload.update({
        "source_sha256": source_hash,
        "working_sha256": working_hash,
        **orientation,
    })
    return orientation


async def _bounded_batch_upload_inspections(batch: QuickBatch, uploads: list[dict]):
    """Yield inspected uploads while keeping task creation and native work bounded."""
    pending = [
        upload for upload in uploads
        if not (
            upload.get("source_sha256")
            and upload.get("working_sha256")
            and _orientation_diagnostics(upload)
        )
    ]
    if not pending:
        return

    async def inspect_one(upload: dict) -> dict:
        await _ensure_batch_upload_inspected(batch, upload)
        return upload

    stream = bounded_completion_map(
        pending,
        inspect_one,
        max_concurrent=OCR_PREPROCESS_LOCAL_MAX_CONCURRENT,
    )
    next_result = asyncio.create_task(anext(stream))
    changed = False
    try:
        while True:
            done, _ = await asyncio.wait(
                {next_result}, timeout=_GRADE_HEARTBEAT_SECONDS,
            )
            if not done:
                yield None
                continue
            try:
                _position, upload = next_result.result()
            except StopAsyncIteration:
                break
            next_result = asyncio.create_task(anext(stream))
            changed = True
            yield upload
    finally:
        if not next_result.done():
            next_result.cancel()
            await asyncio.gather(next_result, return_exceptions=True)
        await stream.aclose()
        # Orientation/hash probes are local and recomputable. Coalesce their
        # metadata into one snapshot instead of rewriting growing JSON per file.
        if changed:
            await _save_quick_batch_meta(batch)


def _with_grading_hardware(
    availability: dict,
    hardware: dict,
    *,
    queued_label: str = "需排队",
    unavailable_label: str = "批改不可用",
    fault_label: str = "链路故障",
) -> dict:
    """Attach the anonymous hardware state and fail closed on bad contracts."""
    fallback_messages = {
        "ready": "教师客户端或机会式加速节点已就绪。",
        "waiting_accelerator": "等待教师客户端加速节点，已保存进度不会丢失。",
        "unavailable": "本地加速节点不可用，已停止新的图像处理任务；已保存进度仍可查看。",
        "fault": "本地加速节点故障，已停止新的图像处理任务；已保存进度仍可查看。",
    }
    hardware_status = str(hardware.get("status") or "fault")
    if hardware_status not in fallback_messages:
        hardware_status = "fault"
    public_hardware = {
        "status": hardware_status,
        "message": str(hardware.get("message") or fallback_messages[hardware_status]),
    }
    result = dict(availability)
    result["hardware"] = public_hardware
    result["service_status"] = str(result.get("status") or "fault")

    mapped_status = {
        "waiting_accelerator": "queued",
        "unavailable": "unavailable",
        "fault": "fault",
    }.get(hardware_status)
    ranks = {"available": 0, "queued": 1, "unavailable": 2, "fault": 3}
    current_status = str(result.get("status") or "fault")
    if mapped_status and ranks[mapped_status] > ranks.get(current_status, 3):
        result.update(
            status=mapped_status,
            label={
                "queued": queued_label,
                "unavailable": unavailable_label,
                "fault": fault_label,
            }[mapped_status],
            message=public_hardware["message"],
        )
    return result


async def _batch_grade_service_availability(user: User) -> dict:
    """Return model, storage and coordinator readiness."""
    from services.model_concurrency import RedisModelLimiter, get_model_limiter
    from services.ocr_service import get_ocr_readiness
    from services.ocr_teacher_fairness import public_teacher_availability

    async def model_readiness() -> tuple[dict, dict | None]:
        from services.analyzer import (
            get_grading_readiness,
            get_grading_readiness_async,
        )

        limiter = get_model_limiter()
        if isinstance(limiter, RedisModelLimiter):
            await limiter.ping()
        ocr = await get_ocr_readiness()
        grading = get_grading_readiness()
        if isinstance(limiter, RedisModelLimiter):
            grading = await get_grading_readiness_async(limiter)
        return ocr, grading

    try:
        async with asyncio.timeout(READINESS_IO_TIMEOUT_SECONDS):
            storage, models, ocr_capacity = await asyncio.gather(
                get_upload_storage_readiness_async(
                    UPLOAD_DIR,
                    MAX_UPLOAD_TOTAL_SIZE,
                    required_copies=2,
                ),
                model_readiness(),
                public_teacher_availability(str(user.id)),
            )
    except TimeoutError as exc:
        # asyncio.timeout's budget expiry raises TimeoutError FROM the
        # CancelledError it injected (see timeouts.__aexit__).  An inner
        # leg raising its own TimeoutError (e.g. a failed Redis ping) is a
        # real coordination fault and must stay fail-closed.
        if not isinstance(exc.__cause__, asyncio.CancelledError):
            logger.exception("batch grading readiness coordination failed")
            return {
                "status": "fault",
                "label": "链路故障",
                "message": "批改任务协调服务发生故障，请稍后重试或联系管理员。",
            }
        # Slow under load means the chain is busy, not broken.  "queued"
        # keeps the submit gate open (work queues behind in-flight
        # batches) instead of mislabeling OCR saturation as a fault.
        logger.warning("batch grading readiness coordination busy (under load)")
        return {
            "status": "queued",
            "label": "需排队",
            "message": "当前批改任务处理中，提交后会按顺序处理。",
        }
    except Exception:
        logger.exception("batch grading readiness coordination failed")
        return {
            "status": "fault",
            "label": "链路故障",
            "message": "批改任务协调服务发生故障，请稍后重试或联系管理员。",
        }

    if not storage.get("ready"):
        if storage.get("status") == "unavailable":
            return {
                "status": "unavailable",
                "label": "批改不可用",
                "message": "服务器存储空间不足，已暂停接收新批改任务，请联系管理员清理空间。",
            }
        return {
            "status": "fault",
            "label": "链路故障",
            "message": "服务器存储状态异常，已暂停接收新批改任务，请联系管理员。",
        }

    ocr, grading = models

    if not ocr.get("configured"):
        return {
            "status": "unavailable",
            "label": "批改不可用",
            "message": "视觉识别服务尚未就绪，请联系管理员。",
        }
    if not ocr.get("ready"):
        return {
            "status": "unavailable",
            "label": "批改不可用",
            "message": "视觉识别服务暂不可用，请稍后重试或联系管理员。",
        }
    if (
        not grading
        or not grading.get("configured")
        or not grading.get("ready")
    ):
        return {
            "status": "unavailable",
            "label": "批改不可用",
            "message": "数学批改服务尚未就绪，请联系管理员。",
        }

    if ocr_capacity.get("status") == "queued":
        return {
            "status": "queued",
            "label": "需排队",
            "message": "当前使用人数较多，提交后会按顺序处理。",
        }
    if ocr_capacity.get("status") != "available":
        return {
            "status": "unavailable",
            "label": "批改不可用",
            "message": "视觉识别服务暂不可用，请稍后重试或联系管理员。",
        }
    return {
        "status": "available",
        "label": "可用",
        "message": "视觉识别、数学批改和任务协调链路已就绪。",
    }


@router.get("/batch-grade/availability")
async def batch_grade_availability(user: User = Depends(_require_user)):
    """Return readiness for ordinary grading without requiring an accelerator."""
    return await _batch_grade_service_availability(user)


async def _require_batch_grade_chain(
    user: User,
    *,
    requires_accelerator: bool = False,
    accelerator_stage: str | None = None,
    accelerator_artifact_contract: str | None = None,
) -> dict:
    """Fail closed before a request creates or mutates grading work."""
    if requires_accelerator and (
        accelerator_stage is None or accelerator_artifact_contract is None
    ):
        raise ValueError("accelerator stage and artifact contract are required together")
    if not requires_accelerator and (
        accelerator_stage is not None or accelerator_artifact_contract is not None
    ):
        raise ValueError("accelerator contract requires requires_accelerator=True")
    try:
        availability = await batch_grade_availability(user)
        if requires_accelerator:
            hardware_kwargs = {}
            if accelerator_stage is not None:
                hardware_kwargs["stage"] = accelerator_stage
            if accelerator_artifact_contract is not None:
                hardware_kwargs["artifact_contract"] = accelerator_artifact_contract
            hardware = await get_public_hardware_readiness(user.id, **hardware_kwargs)
            availability = _with_grading_hardware(availability, hardware)
    except Exception:
        logger.exception("batch grading readiness check failed")
        availability = {
            "status": "fault",
            "message": "批改链路状态检查失败，请稍后重试或联系管理员。",
        }

    status = str(availability.get("status") or "fault")
    if status not in {"available", "queued"}:
        raise HTTPException(
            503,
            detail={
                "error": "GRADING_CHAIN_UNAVAILABLE",
                "status": status if status in {"unavailable", "fault"} else "fault",
                "message": str(
                    availability.get("message")
                    or "批改链路当前不可用，请等待恢复后再重试。"
                ),
            },
        )
    return availability


async def _batch_grade_scoring_availability(batch: QuickBatch) -> dict:
    """Return only the dependencies needed after OCR has been checkpointed."""
    # A completed OCR batch does not need capacity for another multi-GB upload.
    # The readiness service adds its global disk reserve to this one-byte write,
    # which is sufficient to gate the remaining atomic metadata checkpoints.
    storage = await get_upload_storage_readiness_async(
        batch.batch_dir,
        1,
        required_copies=1,
    )
    if not storage.get("ready"):
        if storage.get("status") == "unavailable":
            return {
                "status": "unavailable",
                "label": "批改不可用",
                "message": "服务器空间不足，当前批次无法保存新的判分进度，请联系管理员。",
            }
        return {
            "status": "fault",
            "label": "链路故障",
            "message": "当前批次的进度存储异常，请稍后重试或联系管理员。",
        }

    from services.analyzer import get_grading_readiness, get_grading_readiness_async
    from services.model_concurrency import RedisModelLimiter, get_model_limiter

    try:
        async with asyncio.timeout(READINESS_IO_TIMEOUT_SECONDS):
            limiter = get_model_limiter()
            if isinstance(limiter, RedisModelLimiter):
                await limiter.ping()
            grading = get_grading_readiness()
            if isinstance(limiter, RedisModelLimiter):
                grading = await get_grading_readiness_async(limiter)
    except Exception:
        logger.exception("batch scoring readiness coordination failed")
        return {
            "status": "fault",
            "label": "链路故障",
            "message": "数学批改任务协调服务发生故障，请稍后重试或联系管理员。",
        }

    if not grading.get("configured") or not grading.get("ready"):
        return {
            "status": "unavailable",
            "label": "批改不可用",
            "message": "数学批改服务尚未就绪，请联系管理员。",
        }
    return {
        "status": "available",
        "label": "可用",
        "message": "数学批改和进度保存链路已就绪。",
    }


async def _require_batch_scoring_chain(batch: QuickBatch) -> dict:
    """Fail closed on scoring dependencies without re-gating completed OCR."""
    try:
        availability = await _batch_grade_scoring_availability(batch)
    except Exception:
        logger.exception("batch scoring readiness check failed")
        availability = {
            "status": "fault",
            "message": "数学批改链路状态检查失败，请稍后重试或联系管理员。",
        }

    status = str(availability.get("status") or "fault")
    if status not in {"available", "queued"}:
        raise HTTPException(
            503,
            detail={
                "error": "GRADING_CHAIN_UNAVAILABLE",
                "status": status if status in {"unavailable", "fault"} else "fault",
                "message": str(
                    availability.get("message")
                    or "数学批改链路当前不可用，请等待恢复后再重试。"
                ),
            },
        )
    return availability


def _remove_batch_uploads(saved_uploads: list[tuple[str, str]]) -> None:
    for item in saved_uploads:
        path = item[1]
        try:
            os.remove(path)
        except OSError:
            pass


async def _stream_to_disk_capped_with_sha256(
    upload: UploadFile,
    destination: str,
    max_bytes: int,
) -> tuple[int, str]:
    """Persist one upload and compute its identity in the same bounded pass."""
    digest = hashlib.sha256()
    total = 0
    output = await asyncio.to_thread(open, destination, "wb")
    try:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                return -1, ""
            digest.update(chunk)
            await asyncio.to_thread(output.write, chunk)
    finally:
        await asyncio.to_thread(output.close)
    return total, digest.hexdigest()


async def _save_batch_uploads(
    files: list[UploadFile],
    destination_dir: str,
    *,
    total_byte_budget: int | None = None,
    require_client_ocr_ready: bool = False,
    capture_sha256: bool = False,
) -> list[tuple]:
    """Persist a grade/template batch under count, per-file, and total limits.

    The generic upload endpoint already enforces these limits.  Grade and
    template imports must use the same server-side gate rather than relying on
    nginx's request cap or client-side validation.  Any rejected batch removes
    all files already written for that request.
    """
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(
            413,
            detail=f"单次最多上传 {MAX_UPLOAD_FILES} 个文件",
        )

    if total_byte_budget is None:
        total_byte_budget = MAX_UPLOAD_TOTAL_SIZE
    if total_byte_budget <= 0 or total_byte_budget > MAX_UPLOAD_TOTAL_SIZE:
        raise HTTPException(413, detail="上传文件总大小超过限制")
    storage = await get_upload_storage_readiness_async(
        destination_dir,
        total_byte_budget,
        required_copies=1,
    )
    if not storage.get("ready"):
        raise HTTPException(
            503,
            detail={
                "error": "SERVER_STORAGE_UNAVAILABLE",
                "message": "服务器存储空间不足或不可写，当前无法接收新的批改任务。",
            },
        )
    saved_uploads: list[tuple] = []
    total_bytes = 0
    current_path = ""
    try:
        for upload in files:
            filename = upload.filename or "unknown"
            if os.path.splitext(filename)[1].lower() == ".pdf":
                raise HTTPException(
                    400,
                    detail="批改流程暂不支持 PDF，请先将每页转换为 JPG、PNG 或 WebP 图片",
                )
            if (
                require_client_ocr_ready
                and not filename.lower().endswith(".ocr-ready-v1.jpg")
            ):
                raise HTTPException(
                    400,
                    detail={
                        "error": "CLIENT_IMAGE_PREPROCESS_REQUIRED",
                        "message": (
                            f"{filename} 未完成浏览器图片处理；服务器不会回退解码，"
                            "请重新选择该图片后再提交"
                        ),
                    },
                )
            remaining_bytes = total_byte_budget - total_bytes
            if remaining_bytes <= 0:
                raise HTTPException(413, detail="上传文件总大小超过限制")

            ext = ocr_ready_storage_suffix(filename) or ".png"
            current_path = os.path.join(destination_dir, f"{uuid.uuid4().hex}{ext}")
            max_bytes = min(MAX_UPLOAD_FILE_SIZE, remaining_bytes)
            if capture_sha256:
                size, content_sha256 = await _stream_to_disk_capped_with_sha256(
                    upload,
                    current_path,
                    max_bytes,
                )
            else:
                size = await stream_to_disk_capped(upload, current_path, max_bytes)
                content_sha256 = ""
            if size < 0:
                try:
                    os.remove(current_path)
                except OSError:
                    pass
                current_path = ""
                if remaining_bytes < MAX_UPLOAD_FILE_SIZE:
                    raise HTTPException(413, detail="上传文件总大小超过限制")
                raise HTTPException(413, detail=f"{filename} 超过 20MB 限制")

            validation = await asyncio.to_thread(
                (
                    _validate_client_ocr_ready_upload
                    if require_client_ocr_ready
                    else validate_upload_path
                ),
                filename,
                current_path,
            )
            if not validation["safe"]:
                try:
                    os.remove(current_path)
                except OSError:
                    pass
                current_path = ""
                raise HTTPException(
                    400,
                    detail=f"文件安全检查失败: {validation['reason']}",
                )

            saved_uploads.append(
                (filename, current_path, content_sha256)
                if capture_sha256
                else (filename, current_path)
            )
            current_path = ""
            total_bytes += size
    except Exception:
        if current_path:
            try:
                os.remove(current_path)
            except OSError:
                pass
        _remove_batch_uploads(saved_uploads)
        raise

    return saved_uploads


def _new_template_session(
    template_id: str,
    teacher_id: int,
    teaching_context: dict,
) -> TemplateSession:
    """A new template session, under the upload root the owner checks read."""
    return TemplateSession(
        template_id, UPLOAD_DIR, teacher_id=teacher_id, teaching_context=teaching_context,
    )


def _load_owned_template_session(template_id: str, teacher_id: int) -> TemplateSession:
    """Load a template only when its persisted owner matches the caller."""
    try:
        sess = TemplateSession.load_meta(template_id, UPLOAD_DIR)
    except ValueError as exc:
        raise HTTPException(400, detail="非法的模板标识") from exc
    if sess.teacher_id is None or sess.teacher_id != teacher_id:
        # 所有会话（含 pending）都必须有归属：pending 阶段也已经在创建时写入
        # teacher_id，仅凭不可枚举的模板 ID 不足以授权。
        raise HTTPException(403, detail="无权访问该模板")
    return sess


@contextmanager
def _locked_owned_template_session(template_id: str, teacher_id: int):
    """Serialize template metadata changes and reload after taking the lock."""
    # Validate the identifier and ownership before creating a lock inode.  The
    # second load is the compare-and-save guard against a stale confirmed copy.
    _load_owned_template_session(template_id, teacher_id)
    lock_root = os.path.join(UPLOAD_DIR, ".template-locks")
    try:
        with batch_operation_lock(f"template-{template_id}", lock_root):
            yield _load_owned_template_session(template_id, teacher_id)
    except BatchOperationBusy as exc:
        raise HTTPException(409, detail="该模板正在执行另一项操作，请稍后重试") from exc


async def _template_knowledge_classifier(questions):
    """Label template questions one by one against the stage catalog."""
    from services.analyzer import classify_questions_knowledge
    return await classify_questions_knowledge(questions, per_question=True)


def _validate_template_for_recognition(
    sess: TemplateSession,
    category: str,
    exam_name: str,
    *,
    persist_reconfirmation: bool = False,
) -> None:
    if sess.status not in ("confirmed", "frozen") or not sess.pages:
        raise HTTPException(400, detail="模板未保存或不存在，请先保存模板")
    contract_error = sess.recognition_contract_error()
    if contract_error:
        if (
            persist_reconfirmation
            and (
                not sess.requires_reconfirmation
                or sess.reconfirmation_reason != contract_error
            )
        ):
            sess.requires_reconfirmation = True
            sess.reconfirmation_reason = contract_error
            sess.save_meta()
        raise HTTPException(409, detail=contract_error)
    if sess.category and sess.category != category:
        raise HTTPException(409, detail="模板批次类型与当前批次不一致，请重新导入模板")
    if category == "exam" and sess.exam_name and sess.exam_name != exam_name:
        raise HTTPException(409, detail="模板考试名称与当前批次不一致，请重新导入模板")


def _review_avg_score(rows: list[dict]) -> float | None:
    """Average only resolved provisional scores; pending papers have score=None."""
    scores = [float(row["score"]) for row in rows if row.get("score") is not None]
    return round(sum(scores) / len(scores), 4) if scores else None


def _resolve_safe_image_path(image_path: str) -> str:
    """校验用户传入的 image_path 必须落在 UPLOAD_DIR 内，防止路径遍历任意文件读取（H6）。

    返回规范化后的绝对路径；越界或文件不存在时抛 HTTPException。
    """
    if not image_path:
        raise HTTPException(400, detail="image_path 不能为空")
    base = os.path.realpath(UPLOAD_DIR)
    # 用户可能传相对路径（相对 uploads/）或绝对路径，统一拼到 base 下再规范化
    candidate = image_path if os.path.isabs(image_path) else os.path.join(base, image_path)
    full = os.path.realpath(candidate)
    # 必须严格在 base 目录内（用 os.sep 防止 /uploads-evil 这类前缀绕过）
    if full != base and not full.startswith(base + os.sep):
        raise HTTPException(403, detail="非法的图片路径")
    if not os.path.isfile(full):
        raise HTTPException(404, detail="图片文件不存在")
    return full


# ════════════════════════════════════════════════════════════════════
#  统一批阅流程：recognize（按冻结模板识别学生答案）→ confirm（按模式批改）
#  precise 模式出详细解析，simple/auto 模式只判对错（详略不同，
#  流程一致：选择题机械比对，其余题走 LLM）。
# ════════════════════════════════════════════════════════════════════


def _check_limits_or_raise(
    db: Session,
    user_id: int,
    requested_images: int = 0,
) -> "RateLimiter":
    """Check the natural-day original student-image allowance."""
    limiter = RateLimiter(db)
    daily = limiter.check_daily_image_limit(user_id, requested_images)
    if not daily["allowed"]:
        raise HTTPException(403, detail={
            "error": "DAILY_IMAGE_LIMIT_EXCEEDED",
            "message": (
                f"今日学生试卷图片额度不足（已用 {daily['current']} / "
                f"{daily['limit']} 张，本次 {daily['requested']} 张）"
            ),
            "current": daily["current"],
            "limit": daily["limit"],
            "requested": daily["requested"],
            "remaining": daily["remaining"],
            "reset_time": daily["reset_time"],
        })
    return limiter


def _template_handwriting_regions_for_mode(
    template_mode: object,
    raw_regions: object,
    expected_numbers: list[str] | None = None,
    *,
    teacher_confirmed: bool = False,
) -> list[dict]:
    """Normalize model/teacher boxes for either legacy template mode.

    ``template_mode`` is retained as descriptive metadata for old clients. Both
    modes now use the same visual contract: every expected question has a
    ``regions`` list, which may be empty when the model found no handwriting.
    """
    normalize_template_mode(template_mode, default="marked")
    expected = [str(number).strip() for number in (expected_numbers or [])]
    if raw_regions is None:
        normalized = [
            {"question_number": number, "regions": []}
            for number in expected
        ]
    else:
        normalized = normalize_template_handwriting_regions(raw_regions)
    if expected_numbers is not None:
        by_number = {item["question_number"]: item for item in normalized}
        if set(by_number) != set(expected) or len(normalized) != len(expected):
            raise ValueError("模板已有笔迹框必须与本页题目逐一对应")
        normalized = [by_number[number] for number in expected]
    if teacher_confirmed:
        # Geometry submitted through the teacher override endpoint is the
        # authority for this revision. Ignore any client-supplied provenance
        # and stamp an auditable server-owned teacher marker.
        normalized = [
            {
                **item,
                "confidence": 1.0,
                "human_confirmed": True,
                "confidence_source": "teacher",
            }
            for item in normalized
        ]
    return normalized


def _build_template_paper_cache(template_session: TemplateSession) -> PaperCache:
    """Adapt a frozen teacher template to the shared answers-only OCR path."""
    try:
        template_mode = normalize_template_mode(
            template_session.template_mode,
            default="marked",
        )
        if not template_session.page_artifacts:
            if template_mode == "blank":
                raise ValueError("空白模板缺少视觉基准，请重新导入模板")
            if template_session.has_answers:
                # Compatibility for templates confirmed before per-template ROI
                # artifacts were introduced. New imports always create artifacts.
                return PaperCache.from_confirmed_template_pages(template_session.pages)
            raise ValueError("blank template has no visual evidence artifacts")
        artifacts = verify_template_artifacts(
            template_session.template_dir,
            template_session.page_artifacts,
        )
        for artifact in artifacts.values():
            _template_handwriting_regions_for_mode(
                template_mode,
                artifact.get("template_handwriting_regions"),
            )
        return PaperCache.from_confirmed_template_pages(
            template_session.pages,
            template_artifacts=artifacts,
            template_dir=template_session.template_dir,
            require_local_evidence=True,
            template_id=template_session.template_id,
        )
    except (TemplateEvidenceError, ValueError) as exc:
        raise HTTPException(400, detail=f"模板题干无效：{exc}") from exc


def _single_page_template_failure_questions(
    template_session: TemplateSession,
) -> list[dict]:
    """Build fail-closed review placeholders only when page identity is certain."""
    if len(template_session.pages) != 1:
        return []
    from services.template_slots import ocr_failed_placeholder

    answer_map = template_session.build_answer_map()
    questions: list[dict] = []
    for question in template_session.pages[0].get("questions", []):
        question_number = str(question.get("question_no") or "").strip()
        entry = answer_map.get(question_number, {})
        if not question_number or not entry:
            continue
        questions.append(ocr_failed_placeholder(
            question_number, entry.get("question_type"), entry.get("blank_count"),
        ))
    return questions


def _require_complete_batch_ocr(batch: QuickBatch) -> None:
    """Reject partial OCR batches before any grading or database mutation."""
    if batch.status == "failed":
        raise HTTPException(
            409,
            detail="本批次识别流程已失败，不能开始批阅；请重新识别",
        )
    if not batch.uploads:
        if batch.status not in {
            "awaiting_confirmation", "ocr_done", "grading", "grading_interrupted",
            "review_ready", "voided", "archived",
        }:
            raise HTTPException(
                409,
                detail=f"本批次当前状态 {batch.status or 'unknown'} 不允许开始批阅或回放；请完成识别或重新识别",
            )
        return
    summary = batch_ocr_summary(batch)
    if not summary["can_grade"]:
        missing = summary["failed_papers"] + summary["unresolved_papers"]
        raise HTTPException(
            409,
            detail=(
                f"本批次有 {missing} 张试卷尚未完成 OCR，不能开始部分批阅；"
                "请重新识别失败试卷"
            ),
        )
    if batch.status not in {
        "awaiting_confirmation", "ocr_done", "grading", "grading_interrupted",
        "review_ready", "voided", "archived",
    }:
        raise HTTPException(
            409,
            detail=f"本批次当前状态 {batch.status or 'unknown'} 不允许开始批阅或回放；请完成识别或重新识别",
        )


def _require_orientation_ready(batch: QuickBatch) -> None:
    """Never grade a paper whose page direction was not explicitly cleared."""
    blocked = [
        str(paper.get("filename") or paper.get("paper_id") or "unknown")
        for paper in batch.papers
        if not orientation_is_ready(paper)
    ]
    if blocked:
        raise HTTPException(
            409,
            detail={
                "error": "ORIENTATION_INVALID",
                "message": "存在方向异常或未确认方向的试卷，不能开始批阅；请旋转后重新上传或人工确认。",
                "files": blocked,
            },
        )


_ORIENTATION_DIAGNOSTIC_FIELDS = (
    "orientation_status",
    "orientation_reason",
    "orientation_confidence",
    "detected_tilt_degrees",
    "exif_orientation",
    "orientation_coordinate_space",
)


def _orientation_diagnostics(record: dict) -> dict:
    return {
        field: record.get(field)
        for field in _ORIENTATION_DIAGNOSTIC_FIELDS
        if field in record
    }


def _paper_source_metadata(upload: dict) -> dict:
    """Copy auditable source fields into internal paper state, never SSE paths."""
    fields = (
        "source_relative_path",
        "original_image_path",
        "source_sha256",
        "working_sha256",
        "manual_quarter_turns",
        "orientation_confirmed_by",
        "orientation_confirmed_at",
        "orientation_confirmation_id",
        "orientation_history",
    )
    return {field: upload.get(field) for field in fields if field in upload}


def _stored_paper_orientation(upload: dict, paper_data: dict) -> dict:
    """Return checkpointed orientation data without doing image I/O inline."""
    supplied = paper_data.get("orientation")
    if isinstance(supplied, dict):
        orientation = _orientation_diagnostics(supplied)
        if orientation.get("orientation_status"):
            return orientation
    orientation = _orientation_diagnostics(upload)
    if orientation.get("orientation_status"):
        return orientation
    return {
        "orientation_status": "needs_review",
        "orientation_reason": "orientation_metadata_missing",
        "orientation_confidence": 0.0,
        "detected_tilt_degrees": None,
        "exif_orientation": None,
        "orientation_coordinate_space": "exif_display_pixels",
    }


def _mark_unverified_answer_alignment(paper: dict) -> dict:
    """Flag an answers-only page whose unreadable answers cannot be re-aligned.

    Another full-page Vision pass is no evidence of alignment: a model can
    solve the same question consistently and still replace a blank or wrong
    student answer. The original transcription is therefore kept and only
    marked; the ``ocr_alignment_retry`` key is part of stored OCR results.
    """
    if not paper.get("ocr_answers_only") or paper.get("ocr_alignment_verified"):
        return paper

    has_unreadable = any(
        str(question.get("answer_state") or "").strip().lower() == "unreadable"
        or str(question.get("student_answer") or "").strip() == "[无法识别]"
        for question in paper.get("questions", [])
    )
    if not has_unreadable:
        return paper

    unresolved = dict(paper)
    unresolved["ocr_alignment_retry"] = "skipped_no_spatial_evidence"
    return unresolved


def _reject_answer_key_matching_local_recoveries(
    paper: dict,
    answer_map: dict,
) -> dict:
    """Do not auto-grade a local OCR recovery that matches the answer key."""
    changed = False
    questions = []
    normalized_answer_map = {
        str(key).strip(): value
        for key, value in (answer_map or {}).items()
    }
    for question in paper.get("questions", []):
        current = dict(question)
        question_no = str(current.get("question_no") or "").strip()
        if current.pop("_ocr_local_recovery", False):
            from services.grading_contract import normalize_choice_answer

            recovered = normalize_choice_answer(current.get("student_answer"))
            standard = normalize_choice_answer(
                (normalized_answer_map.get(question_no) or {}).get("standard_answer")
            )
            if recovered and standard and recovered == standard:
                current["student_answer"] = "[无法识别]"
                current["answer_state"] = "unreadable"
                changed = True
        questions.append(current)
    if not changed and questions == paper.get("questions", []):
        return paper
    result = dict(paper)
    result["questions"] = questions
    if changed:
        result["ocr_local_recovery"] = "answer_key_match_rejected"
    return result


def _anonymous_page_identity(
    batch: QuickBatch,
    image_path: str,
) -> tuple[str, dict]:
    """Return a stable display label without persisting OCR identity data."""
    page_number = next(
        (
            index + 1
            for index, item in enumerate(batch.uploads)
            if os.path.realpath(str(item.get("image_path") or ""))
            == os.path.realpath(image_path)
        ),
        len(batch.papers) + 1,
    )
    return f"匿名页 {page_number}", {
        "status": "anonymous",
        "student_id": None,
        "reason": "anonymous_batch",
    }


def _resolve_identity_with_filename_clues(
    db: Session,
    teacher_id: int,
    class_id: int | None,
    *,
    ocr_name: str | None,
    ocr_student_no: str | None,
    filename: str,
    source_relative_path: str = "",
    roster_candidate_names: list[str] | None = None,
    roster_match_status: str = "",
) -> tuple[str, str, dict]:
    """姓名先于学号；目录/文件名只在本批次班级内提供身份线索。"""
    students = db.query(Student).filter(
        Student.teacher_id == teacher_id,
        Student.class_id == class_id,
    ).order_by(Student.id.asc()).all() if class_id is not None else []
    identity = resolve_collection_identity(
        [{"student_id": student.id, "name": student.name,
          "student_no": student.student_no or "", "class_id": student.class_id}
         for student in students],
        ocr_name=ocr_name or "", ocr_student_no=ocr_student_no or "",
        filename=filename, source_relative_path=source_relative_path,
        roster_candidate_names=roster_candidate_names,
        roster_match_status=roster_match_status,
    )
    name = identity.get("resolved_name") or ocr_name or ""
    number = identity.get("resolved_student_no") or ""
    identity["_clue_source"] = identity["clue_source"]
    return name, number, identity


def _ingest_recognized_paper(
    batch: QuickBatch,
    db: Session,
    teacher_id: int,
    filename: str,
    image_path: str,
    paper_data: dict,
    failure_questions: list[dict] | None = None,
) -> dict | None:
    """Persist one OCR card into batch state without creating Student rows."""
    upload = next(
        (
            item for item in batch.uploads
            if os.path.realpath(str(item.get("image_path") or "")) == os.path.realpath(image_path)
        ),
        {},
    )
    paper_id = str(upload.get("paper_id") or uuid.uuid4().hex[:12])
    source_metadata = _paper_source_metadata(upload)
    anonymous_label, anonymous_identity = _anonymous_page_identity(
        batch, image_path,
    )
    is_anonymous = batch.identity_mode == "anonymous"
    # A page routed to a question_only/non_gradable template page has no
    # questions by contract; it is reported as not graded, not as a failure.
    if paper_data.get("error") or (
        not paper_data.get("questions") and not paper_data.get("_not_graded_page")
    ):
        error = str(paper_data.get("error") or "试卷内容无法识别")
        error_info = paper_data.get("error_info") or {}
        orientation = _stored_paper_orientation(upload, paper_data)
        # A routed failure (e.g. a zero-readout transcription) knows its page
        # type, so its own review placeholders also cover multi-page templates.
        routed_placeholders = paper_data.get("_fail_closed_questions")
        if failure_questions is not None and routed_placeholders:
            failure_questions = routed_placeholders
        # The QuickBatch owns the original image and persists this error below.
        # Copying it again into the legacy failed-import folder doubles disk use
        # during provider-wide outages without adding recoverability.
        if failure_questions:
            if is_anonymous:
                display_name, display_no, identity = anonymous_label, "", anonymous_identity
            else:
                display_name, display_no, identity = _resolve_identity_with_filename_clues(
                    db, teacher_id, batch.class_id, ocr_name="", ocr_student_no="",
                    filename=filename, source_relative_path=str(upload.get("source_relative_path") or ""),
                )
            batch.papers.append({
                "paper_id": paper_id,
                "filename": filename,
                "student_name": display_name,
                "student_no": display_no,
                "identity": identity,
                "identity_ocr_source": "disabled" if is_anonymous else "ocr_failure",
                "identity_ocr_confidence": 0.0,
                "identity_roster_candidates": [],
                "identity_roster_match_status": "",
                "image_path": image_path,
                **source_metadata,
                **orientation,
                "unexpected_page_type": False,
                "page_type_error": "",
                "ocr_failure": True,
                "ocr_error": error,
                "error_info": error_info,
                **({"ocr_receipt": paper_data["_ocr_receipt"]} if paper_data.get("_ocr_receipt") else {}),
                "requires_manual_review": True,
                "orientation_status": orientation.get("orientation_status"),
                "orientation_reason": orientation.get("orientation_reason"),
                "questions": [dict(question) for question in failure_questions],
            })
            _refresh_identity_inheritance(batch)
            return {
                "batch_id": batch.batch_id,
                "paper_id": paper_id,
                "filename": filename,
                "student_name": display_name,
                "student_no": display_no,
                "identity": identity,
                "questions_count": len(failure_questions),
                "matched_count": 0,
                "error": error,
                "error_info": error_info,
                "requires_manual_review": True,
                **_orientation_diagnostics(orientation),
            }
        return {
            "batch_id": batch.batch_id, "paper_id": paper_id,
            "filename": filename,
            "student_name": anonymous_label if is_anonymous else "",
            "student_no": "",
            "questions_count": 0, "matched_count": 0,
            "error": error,
            "error_info": error_info,
            "requires_manual_review": not orientation_is_ready(orientation),
            **_orientation_diagnostics(orientation),
        }

    ocr_questions = paper_data.get("questions", [])
    if is_anonymous:
        student_name = anonymous_label
        student_no = ""
        identity = anonymous_identity
    else:
        student_name, student_no, identity = _resolve_identity_with_filename_clues(
            db,
            teacher_id,
            batch.class_id,
            ocr_name=paper_data.get("student_name"),
            ocr_student_no=paper_data.get("student_no"),
            filename=filename,
            source_relative_path=str(upload.get("source_relative_path") or ""),
            roster_candidate_names=paper_data.get("identity_roster_candidates") or [],
            roster_match_status=str(paper_data.get("identity_roster_match_status") or ""),
        )
        identity_clue_source = str(identity.pop("_clue_source", ""))
    orientation = _stored_paper_orientation(upload, paper_data)
    batch.papers.append({
        "paper_id": paper_id,
        "filename": filename,
        "student_name": student_name,
        "student_no": student_no,
        "identity": identity,
        "identity_ocr_source": (
            "disabled"
            if is_anonymous
            else str(paper_data.get("identity_ocr_source") or "full_paper")
        ),
        "identity_ocr_confidence": (
            0.0
            if is_anonymous
            else float(paper_data.get("identity_ocr_confidence") or 0.0)
        ),
        "identity_roster_candidates": (
            []
            if is_anonymous
            else list(paper_data.get("identity_roster_candidates") or [])
        ),
        "identity_roster_match_status": (
            ""
            if is_anonymous
            else str(paper_data.get("identity_roster_match_status") or "")
        ),
        "identity_name_written": (
            None if is_anonymous else paper_data.get("identity_name_written")
        ),
        # 身份线索来源（ocr / filename / ocr+filename / conflict），供确认弹窗
        # 向老师说明"这个姓名学号是从哪来的"。
        "identity_clue_source": "" if is_anonymous else identity_clue_source,
        "image_path": image_path,
        **source_metadata,
        **orientation,
        "unexpected_page_type": bool(paper_data.get("_unexpected_page_type")),
        "page_type_error": str(paper_data.get("page_type_error") or ""),
        **({"page_not_graded": True, "page_role": str(paper_data.get("_page_role") or "")}
           if paper_data.get("_not_graded_page") else {}),
        "ocr_answers_only": bool(paper_data.get("_answers_only")),
        "ocr_page_type": str(paper_data.get("_cache_type_label") or ""),
        "ocr_local_template_evidence": bool(
            paper_data.get("_local_template_evidence")
        ),
        "ocr_full_page_transcription": bool(
            paper_data.get("_full_page_transcription")
        ),
        # Persist the OCR/score boundary with the paper, not merely in global
        # config, so stored results keep describing how they were produced.
        "ocr_transcription_only": bool(paper_data.get("_transcription_only")),
        "ocr_requires_separate_grading": bool(
            paper_data.get("_requires_separate_grading")
        ),
        **({"drawing_assessments": paper_data["_drawing_assessments"]}
           if paper_data.get("_drawing_assessments") else {}),
        # Hashes of the exact images sent and the response shape received.
        **({"ocr_receipt": paper_data["_ocr_receipt"]} if paper_data.get("_ocr_receipt") else {}),
        "questions": [
            {
                "question_no": str(q.get("question_number", "")),
                "student_answer": q.get("student_answer", "") or "",
                "answer_state": str(q.get("answer_state") or ""),
                "question_type": q.get("question_type") or "solve",
                "_ocr_local_recovery": bool(q.get("_ocr_local_recovery")),
                # Slot identity evidence must survive ingestion: grading
                # re-binds student_slots to the frozen slot_ids and
                # fail-closes contracted questions without it.
                **{key: q[key] for key in ("student_slots", "slot_identity_error") if key in q},
                **({"answer_bboxes": [list(box) for box in detail_review_boxes(q)]} if detail_review_boxes(q) else {}),
            }
            for q in ocr_questions
        ],
    })

    seen = {q["question_no"] for q in batch.questions}
    for question in ocr_questions:
        question_no = str(question.get("question_number", ""))
        if question_no and question_no not in seen:
            batch.questions.append({
                "question_no": question_no,
                "question_text": question.get("question_text", "") or "",
                "question_type": question.get("question_type", "solve") or "solve",
            })
            seen.add(question_no)
    _refresh_identity_inheritance(batch)
    return None


def _refresh_identity_inheritance(batch: QuickBatch) -> None:
    """Re-derive opt-in inherited identities after any change to the page set."""
    if batch.identity_mode == "anonymous" or not batch.identity_inheritance:
        return
    apply_identity_inheritance(
        batch.uploads,
        batch.papers,
        enabled=True,
        excluded_paper_ids=(
            str(job.get("paper_id") or "")
            for job in batch.ocr_jobs
            if job.get("status") == "excluded"
        ),
    )


async def _recognize_batch_upload(
    batch: QuickBatch,
    image_path: str,
    paper_cache: PaperCache,
    *,
    teacher_id: str,
    paper_id: str,
    roster_names: list[str] | None = None,
) -> dict:
    """Recognize identity first, then run the frozen-template answer OCR.

    Identity is read from the dedicated top/left crop before any page-cache
    matching or column reuse.  The subsequent OCR request never receives the
    identity area, so splitting a page into one, two, or three columns cannot
    change which student owns the answers.
    """
    identity_data: dict = {}
    if batch.identity_mode != "anonymous":
        try:
            identity_data = await recognize_student_info_with_roster(
                image_path,
                list(roster_names or []),
                teacher_id=teacher_id,
            )
        except Exception as exc:
            # Identity uncertainty must remain visible to the teacher, but it
            # must not discard otherwise usable answer OCR.
            logger.warning("identity preflight failed for %s: %s", image_path, exc)
            identity_data = {
                "name": "",
                "student_no": "",
                "confidence": 0.0,
                "candidate_names": [],
                "match_status": "failed",
            }

    # Identity comes only from the dedicated crop above; the template OCR
    # prompt never asks for it, in identified and anonymous modes alike.
    result = await await_teaching(batch.teaching_context, recognize_paper_with_cache(
        image_path,
        batch.batch_id,
        paper_cache,
        teacher_id=teacher_id,
        paper_id=paper_id,
    ))
    if batch.identity_mode != "anonymous":
        result.update({
            "student_name": str(identity_data.get("name") or "").strip(),
            "student_no": str(identity_data.get("student_no") or "").strip(),
            "identity_ocr_source": "top_left_crop",
            "identity_ocr_confidence": float(identity_data.get("confidence") or 0.0),
            "identity_roster_candidates": list(
                identity_data.get("candidate_names") or []
            ),
            "identity_roster_match_status": str(
                identity_data.get("match_status") or ""
            ),
            # None (unknown, e.g. a failed identity OCR) never counts as blank.
            "identity_name_written": identity_data.get("name_written"),
        })
    return result


# ── Student recognition steps shared by recognize and resume ─────────────────


def _class_roster_names(db: Session, batch: QuickBatch, teacher_id: int) -> list[str]:
    """Names on the batch's frozen class roster; anonymous batches have none."""
    if batch.identity_mode == "anonymous":
        return []
    students = db.query(Student).filter(
        Student.teacher_id == teacher_id,
        Student.class_id == batch.class_id,
    ).order_by(Student.id.asc()).all()
    return [student.name for student in students if student.name]


def _apply_ocr_outcome(
    batch: QuickBatch,
    db: Session,
    teacher_id: int,
    upload: dict,
    paper_data: dict,
    failure_questions: list[dict],
) -> dict | None:
    """Record one OCR outcome in the job ledger and the batch's papers.

    Returns the error payload of a page without a usable transcription, or
    None. A retryable failure stays without review placeholders so the next
    attempt can still succeed. The caller checkpoints the batch afterwards.
    """
    image_path = str(upload.get("image_path") or "")
    _failed, can_retry = mark_ocr_outcome(batch, image_path, paper_data)
    if is_accelerator_pause_result(paper_data):
        return None
    error_payload = _ingest_recognized_paper(
        batch,
        db,
        teacher_id,
        str(upload.get("filename") or "unknown"),
        image_path,
        paper_data,
        failure_questions=(None if can_retry else failure_questions),
    )
    if (
        error_payload
        and error_payload.get("requires_manual_review")
        and orientation_is_ready(error_payload)
    ):
        batch.mark_ocr_job(
            image_path,
            "needs_review",
            str(error_payload.get("error") or ""),
        )
    return error_payload


def _discard_unstarted_batch(batch: QuickBatch, saved_uploads: list[tuple]) -> None:
    """Remove a rejected batch's uploads and metadata before recognition starts."""
    _remove_batch_uploads(saved_uploads)
    try:
        os.remove(batch.meta_path)
    except OSError:
        pass


def _initialize_batch_answer_map(
    batch: QuickBatch,
    teacher_id: int,
    template_session: TemplateSession,
) -> None:
    """Derive the batch's answer map and question list from its frozen template."""
    batch.answer_map = template_session.build_answer_map()
    try:
        if (
            template_session.teacher_id == teacher_id
            and template_session.status in {"confirmed", "frozen"}
            and not template_session.recognition_contract_error()
        ):
            latest = _load_owned_template_session(
                template_session.template_id, teacher_id
            )
            if (
                latest.teacher_id == teacher_id
                and latest.status in {"confirmed", "frozen"}
                and latest.confirmed_contract_hash
                    == template_session.confirmed_contract_hash
                and not latest.recognition_contract_error()
            ):
                # OCR may outlive the background classification. Refresh only
                # derived metadata; keep this batch's frozen answers/pages.
                enriched = copy.copy(template_session)
                enriched.knowledge_classifications = copy.deepcopy(
                    latest.knowledge_classifications
                )
                batch.answer_map = enriched.build_answer_map()
    except Exception as exc:
        # An unavailable/newer template must not change the frozen authority.
        logger.warning(
            "模板派生知识点刷新暂缓 template=%s: %s",
            template_session.template_id,
            exc,
        )
    # The frozen template keeps answer geometry in page artifacts rather
    # than in the answer-text contract. Carry it into the final grading
    # map after any derived metadata refresh above.
    for artifact in (template_session.page_artifacts or {}).values():
        if not isinstance(artifact, dict):
            continue
        regions = artifact.get("template_answer_regions") or artifact.get("template_handwriting_regions") or []
        if not isinstance(regions, list):
            continue
        for region in regions:
            if not isinstance(region, dict):
                continue
            number = str(region.get("question_number") or region.get("question_no") or "").strip()
            boxes = normalize_review_boxes(region.get("regions"))
            if number and boxes and number in batch.answer_map:
                batch.answer_map[number]["answer_bboxes"] = [list(box) for box in boxes]
                batch.answer_map[number]["answer_box_source"] = "template"
    batch.questions = [
        {
            "question_no": qn,
            "question_text": entry.get("question_text", ""),
            "question_type": entry.get("question_type", "solve"),
        }
        for qn, entry in batch.answer_map.items()
    ]


def _confirmation_questions(batch: QuickBatch) -> list[dict]:
    """Return the frozen template questions in the unified AnswerSpec payload."""
    questions_out = []
    for question in batch.questions:
        question_no = question["question_no"]
        entry = batch.answer_map.get(question_no, {}) or {}
        questions_out.append({
            **AnswerSpec.model_validate({**entry, "question_no": question_no}).directory_metadata(),
            **({"drawing_reference": copy.deepcopy(entry["drawing_reference"])} if entry.get("drawing_reference") else {}),
            "question_no": question_no,
            "content": question["question_text"],
            "question_type": entry.get("question_type") or question.get("question_type") or "solve",
            "answer": entry.get("standard_answer") or "",
            "answer_variants": entry.get("answer_variants", []) or [],
            "solution_steps": entry.get("solution_steps", []) or [],
            "max_score": entry.get("max_score"),
            "blank_count": entry.get("blank_count", 1),
            "answer_slots": entry.get("answer_slots", []) or [],
            "fill_scoring_mode": entry.get("fill_scoring_mode", "equal") or "equal",
            "blank_scores": entry.get("blank_scores", []) or [],
            "subquestion_count": entry.get("subquestion_count", 1),
            "subquestion_scores": entry.get("subquestion_scores", []) or [],
            "grading_method": entry.get("grading_method", "") or "",
            "chapter": entry.get("chapter") or "",
            "topic": entry.get("topic") or "",
            "matched": bool(entry.get("matched")),
            "source": entry.get("source") or "bank",
        })
    return questions_out


# ── Student recognition streams ──────────────────────────────────────────────
#
# Both recognition endpoints validate the request and the template evidence
# first, then return an SSE stream.  Inside the stream each concern has its
# own owner and the event generators only sequence them:
#
#   _RecognitionLease    DB session, batch operation lock and request-local
#                        vision/grading context, released in reverse
#   _RecognitionRun      resumable-run state over the durable OCR job ledger
#   _RecognitionFlavor   where the first run and a resume deliberately differ
#   *_events             what the client sees, in order


_RECOGNITION_DISCONNECTED = "识别连接中断，等待恢复"


class _RecognitionLease:
    """What one recognition stream holds while it runs.

    Taken inside the SSE generator, so a busy batch still answers with an SSE
    error instead of an HTTP status.  ``release`` undoes it in reverse: the
    grading pipeline, the batch lock, the context tokens, the DB session.
    """

    def __init__(self, batch_id: str, teacher_id: int) -> None:
        self.batch_id = batch_id
        self.db = SessionLocal()
        self._batch_lock = None
        self._grading_token = set_grading_teacher_id(str(teacher_id))
        self._vision_phase_token = None
        self._vision_batch_size_token = None

    def set_vision_phase(self, phase_key: str, *, retry_scope_key: str | None = None) -> None:
        self._vision_phase_token = set_vision_phase_key(phase_key, retry_scope_key=retry_scope_key)

    def set_vision_batch_size(self, paper_count: int) -> None:
        self._vision_batch_size_token = set_vision_batch_paper_count(paper_count)

    def acquire_batch_lock(self) -> bool:
        """Take the cross-worker batch lock; False while another stream holds it."""
        try:
            self._batch_lock = batch_operation_lock(self.batch_id, UPLOAD_DIR)
            self._batch_lock.__enter__()
        except BatchOperationBusy:
            self._batch_lock = None
            return False
        return True

    async def release(self, pipeline: "_PerPaperGradingPipeline | None") -> None:
        try:
            if pipeline is not None:
                await pipeline.aclose()
        finally:
            try:
                if self._batch_lock is not None:
                    self._batch_lock.__exit__(None, None, None)
            finally:
                try:
                    if self._vision_batch_size_token is not None:
                        reset_vision_batch_paper_count(self._vision_batch_size_token)
                    if self._vision_phase_token is not None:
                        reset_vision_phase_key(self._vision_phase_token)
                    reset_grading_teacher_id(self._grading_token)
                finally:
                    self.db.close()


@dataclass(frozen=True)
class _RecognitionFlavor:
    """Where the first recognition run and a resumed one differ on purpose.

    Both streams share one implementation; these fields keep the events and
    checkpoints of each exactly as they were before the two were merged.
    """

    start_message: str
    busy_message: str
    # Formatted with ``inspected`` and ``total``.
    inspection_message: str
    inspection_wait_message: str
    ocr_heartbeat_message: str
    ocr_failure_log: str
    dispatch_suffix: str
    # A resume tags every OCR and grading progress event with its batch id.
    tags_progress: bool
    # A resume keeps ``pending_uploads`` in step with every ledger change.
    syncs_pending_uploads: bool
    # The first run reports manual-review placeholders only with the final
    # paper list; a resume reports every failed page at once and the client
    # merges rows by paper_id.
    streams_review_placeholders: bool

    def streams_failed_page(self, error_payload: dict | None, accelerator_wait: bool) -> bool:
        """Whether a page without a usable transcription is reported right away."""
        if not error_payload or accelerator_wait:
            return False
        return self.streams_review_placeholders or (
            not error_payload.get("requires_manual_review")
            or not orientation_is_ready(error_payload)
        )


_INITIAL_RECOGNITION = _RecognitionFlavor(
    start_message="开始识别",
    busy_message="该批次正在执行识别，请等待当前任务完成，不要重复提交",
    inspection_message="已完成 {inspected}/{total} 张图片安全方向检查",
    inspection_wait_message="正在进行图片安全与方向检查，请保持页面打开",
    ocr_heartbeat_message="正在进行本地配准与答案区域预处理，请稍候",
    ocr_failure_log="并发 OCR 异常 %s: %s",
    dispatch_suffix="",
    tags_progress=False,
    syncs_pending_uploads=False,
    streams_review_placeholders=False,
)
_RESUMED_RECOGNITION = _RecognitionFlavor(
    start_message="正在恢复未完成的识别任务",
    busy_message="该批次仍在识别，请等待当前任务完成，不要重复提交",
    inspection_message="恢复时已检查 {inspected}/{total} 张图片",
    inspection_wait_message="正在恢复图片安全与方向检查，请保持页面打开",
    ocr_heartbeat_message="正在恢复未完成的试卷识别，请保持页面打开",
    ocr_failure_log="恢复 OCR 异常 %s: %s",
    dispatch_suffix=":resume",
    tags_progress=True,
    syncs_pending_uploads=True,
    streams_review_placeholders=True,
)


@dataclass
class _RecognitionRun:
    """One recognition stream's progress over the batch's durable OCR ledger.

    A resume replaces ``batch`` with the checkpoint it reloads under the batch
    lock; an interruption or failure always checkpoints the current object.
    """

    batch: QuickBatch
    teacher_id: int
    flavor: _RecognitionFlavor
    db: Session
    total: int = 0
    processed: int = 0
    pipeline: "_PerPaperGradingPipeline | None" = None
    # Set once a resume has persisted its outcome: a disconnect after that
    # point must not reopen the batch as interrupted.
    completed: bool = False

    def busy_payload(self) -> dict:
        return {"code": "BATCH_RECOGNITION_BUSY", "message": self.flavor.busy_message}

    def started_payload(self) -> dict:
        return {
            "batch_id": self.batch.batch_id,
            "message": self.flavor.start_message,
            "current": self.processed,
            "total": self.total,
        }

    def inspection_payload(self, inspected: int, *, waiting: bool) -> dict:
        return {
            "batch_id": self.batch.batch_id,
            "message": (
                self.flavor.inspection_wait_message
                if waiting
                else self.flavor.inspection_message.format(inspected=inspected, total=self.total)
            ),
            "current": self.processed,
            "total": self.total,
            "heartbeat": waiting,
        }

    def progress_payload(self, message: str, *, heartbeat: bool = False) -> dict:
        """Progress of the OCR and grading phases."""
        payload = {"batch_id": self.batch.batch_id} if self.flavor.tags_progress else {}
        payload.update(message=message, current=self.processed, total=self.total)
        if heartbeat:
            payload["heartbeat"] = True
        return payload

    async def record_ocr_outcome(
        self,
        upload: dict,
        paper_data: dict,
        failure_questions: list[dict],
    ) -> dict | None:
        """Ledger, checkpoint, then grade one finished upload, in that order."""
        error_payload = _apply_ocr_outcome(
            self.batch, self.db, self.teacher_id, upload, paper_data, failure_questions,
        )
        if self.flavor.syncs_pending_uploads:
            sync_pending_recognition_uploads(self.batch)
        await _save_quick_batch_meta(self.batch)
        self.pipeline.dispatch_upload(upload)
        self.processed += 1
        return error_payload

    async def checkpoint_stop(self, status: str, error: str, *, log_message: str) -> None:
        """Persist a run that ended early; its finished OCR and grades stay saved."""
        self.batch.status = status
        self.batch.error = error
        try:
            await _checkpoint_recognition_cleanup(self.batch, self.pipeline)
        except Exception:
            logger.exception(log_message, self.batch.batch_id)


def _inspected_upload_count(batch: QuickBatch) -> int:
    """Uploads already hashed and orientation-checked."""
    return sum(
        bool(
            upload.get("source_sha256")
            and upload.get("working_sha256")
            and _orientation_diagnostics(upload)
        )
        for upload in batch.uploads
    )


async def _grading_drain_events(run: _RecognitionRun):
    """SSE until every paper dispatched during OCR is graded.

    收尾对账：批阅已逐卷提前跑，尾部在途卷在这里补齐再出 done。
    """
    async for kind, graded in run.pipeline.drain():
        if kind == "heartbeat":
            yield _sse("progress", run.progress_payload(
                "正在批阅已识别的试卷，请保持页面打开", heartbeat=True,
            ))
            continue
        yield _pipeline_result_sse(run.pipeline, graded)


def _complete_recognition(batch: QuickBatch, *, clear_error: bool) -> tuple[list[dict], dict]:
    """Freeze the confirmation questions and settle the batch's OCR status."""
    # 组装冻结模板的题目合同。
    questions_out = _confirmation_questions(batch)
    batch.confirmation_questions = questions_out
    ocr_summary = batch_ocr_summary(batch)
    batch.status = "ocr_done" if ocr_summary["can_grade"] else "ocr_partial_failed"
    batch.pending_uploads = []
    if clear_error:
        batch.error = None
    return questions_out, ocr_summary


def _sse_response(teaching_context: dict | None, events) -> StreamingResponse:
    return StreamingResponse(
        stream_teaching(teaching_context, events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Initial recognition ──────────────────────────────────────────────────────


@router.post("/batch-grade/recognize")
async def batch_grade_recognize(
    files: list[UploadFile] = File(...),
    category: str = Form("daily"),
    exam_name: str = Form(""),
    template_id: str = Form(""),
    mode: str = Form("precise"),
    anonymous: bool = Form(False),
    source_paths: str = Form(""),
    inherit_identity: bool = Form(False),
    user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    """阶段一：复用已确认模板并识别学生答案（SSE）。

    新批次必须提供 ``template_id``。模板冻结的题干、题型和标准答案是
    本批次依据，学生卷只按题号识别答案。

    SSE events:
      - progress: {message, current, total}
      - paper:    {filename, student_name, questions_count, matched_count, [error]}
      - done:     {batch_id, mode, total_papers, questions:[...]}
      - error:    {message}
    """
    relative_paths = _parse_recognition_request(
        files,
        category=category,
        exam_name=exam_name,
        mode=mode,
        source_paths=source_paths,
        anonymous=anonymous,
        template_id=template_id,
        user=user,
    )

    # Student template recognition has a verified server-side evidence matcher
    # fallback.  Accelerator readiness is advisory here; an unavailable
    # opportunity node must not turn an otherwise healthy grading request into
    # a pre-upload 503.
    await _require_batch_grade_chain(user)

    template_session = _template_for_new_batch(template_id, user.id, category, exam_name)
    limiter = _check_requested_image_allowance(db, user.id, len(files))
    batch = _new_template_batch(
        template_session,
        user,
        template_id=template_id,
        category=category,
        exam_name=exam_name,
        mode=mode,
        anonymous=anonymous,
        inherit_identity=inherit_identity,
    )

    # ── 保存上传文件（流式落盘 + 安全校验 + 批量硬限制） ──
    saved_uploads = await _save_new_batch_uploads(batch, files)
    saved_uploads = _register_batch_uploads(batch, saved_uploads, relative_paths)
    await _save_quick_batch_meta(batch)

    # All request validation and upload checks have succeeded.  Reload while
    # holding the cross-worker lock so a concurrent confirm cannot overwrite a
    # frozen status with an older confirmed snapshot.
    try:
        template_session, paper_cache = _freeze_template_for_batch(
            batch, template_id, user.id, category, exam_name,
        )
    except Exception:
        _discard_unstarted_batch(batch, saved_uploads)
        raise
    _reserve_batch_images(limiter, user.id, batch, saved_uploads)
    failure_questions = _single_page_template_failure_questions(template_session)

    return _sse_response(batch.teaching_context, _initial_recognition_events(
        batch, template_session, paper_cache, failure_questions, user.id, mode,
    ))


def _parse_recognition_request(
    files: list[UploadFile],
    *,
    category: str,
    exam_name: str,
    mode: str,
    source_paths: str,
    anonymous: bool,
    template_id: str,
    user: User,
) -> list[str]:
    """Reject a malformed request before any I/O; return the uploads' source paths."""
    if category not in VALID_GRADING_CATEGORIES:
        raise HTTPException(400, detail="category 必须是 daily 或 exam")
    if category == "exam" and not exam_name:
        raise HTTPException(400, detail="exam 类型需要 exam_name")
    if mode not in ("precise", "auto", "simple"):
        raise HTTPException(400, detail="mode 必须是 precise/auto/simple")
    if not files:
        raise HTTPException(400, detail="未上传文件")
    try:
        relative_paths = parse_source_paths(source_paths if isinstance(source_paths, str) else "", len(files))
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    # 产品约束：批改必须在某个已选班级下进行（学号=班级前缀+班内座号，靠班级消歧）。
    # 未选班级直接拦下，避免跨班误配 / 产生无班级归属的孤儿学生。
    if not anonymous and user.current_class_id is None:
        raise HTTPException(400, detail="请先选择班级后再批改")

    # Every batch is graded against a confirmed template.
    if not str(template_id or "").strip():
        raise HTTPException(400, detail="请先导入并确认模板后再开始批改")
    return relative_paths


def _template_for_new_batch(
    template_id: str,
    teacher_id: int,
    category: str,
    exam_name: str,
) -> TemplateSession:
    """The caller's confirmed template; contract drift is persisted for reconfirmation."""
    template_session = _load_owned_template_session(template_id, teacher_id)
    try:
        _validate_template_for_recognition(template_session, category, exam_name)
    except HTTPException as exc:
        if exc.status_code == 409 and template_session.recognition_contract_error():
            with _locked_owned_template_session(template_id, teacher_id) as latest_template:
                _validate_template_for_recognition(
                    latest_template,
                    category,
                    exam_name,
                    persist_reconfirmation=True,
                )
        raise
    return template_session


def _check_requested_image_allowance(db: Session, teacher_id: int, requested: int):
    """Today's student-image allowance must cover this upload; returns the limiter."""
    # Keep the gate callable with the historical two-argument contract used by
    # integrations and test doubles, then apply the requested-image check on
    # the returned limiter. The second check is still before any OCR work.
    limiter = _check_limits_or_raise(db, teacher_id)
    if limiter is not None:
        requested_daily = limiter.check_daily_image_limit(teacher_id, requested)
        if not requested_daily["allowed"]:
            raise HTTPException(403, detail={
                "error": "DAILY_IMAGE_LIMIT_EXCEEDED",
                "message": (
                    f"今日学生试卷图片额度不足（已用 {requested_daily['current']} / "
                    f"{requested_daily['limit']} 张，本次 {requested_daily['requested']} 张）"
                ),
                **requested_daily,
            })
    return limiter


def _new_template_batch(
    template_session: TemplateSession,
    user: User,
    *,
    template_id: str,
    category: str,
    exam_name: str,
    mode: str,
    anonymous: bool,
    inherit_identity: bool,
) -> QuickBatch:
    """A new batch frozen to its class, identity mode, category and template."""
    batch_id = uuid.uuid4().hex[:12]
    require_teacher_stage(user)
    batch = QuickBatch(
        batch_id, UPLOAD_DIR, teaching_context=template_session.teaching_context,
    )
    batch.teacher_id = user.id
    batch.class_id = None if anonymous else user.current_class_id
    batch.identity_mode = "anonymous" if anonymous else "identified"
    batch.identity_inheritance = bool(inherit_identity) and not anonymous
    batch.source_mode = "template"
    batch.template_id = str(template_id)
    batch.template_mode = str(template_session.template_mode or "marked")
    batch.category = category
    batch.exam_name = exam_name if category == "exam" else ""
    batch.grade_mode = mode
    os.makedirs(batch.batch_dir, exist_ok=True)
    return batch


async def _save_new_batch_uploads(batch: QuickBatch, files: list[UploadFile]) -> list[tuple]:
    """Stream the uploads to disk; a rejected upload leaves no batch directory."""
    try:
        return await _save_batch_uploads(
            files,
            batch.batch_dir,
            require_client_ocr_ready=True,
            capture_sha256=True,
        )
    except Exception:
        try:
            os.rmdir(batch.batch_dir)
        except OSError:
            pass
        raise


def _register_batch_uploads(
    batch: QuickBatch,
    saved_uploads: list[tuple],
    relative_paths: list[str],
) -> list[tuple]:
    """Record the saved uploads and their queued OCR jobs, in recognition order."""
    if batch.identity_inheritance:
        # "The previous page" must not depend on the browser's file-picker
        # order (Windows lists the focused file first): use natural path order.
        order = sorted(
            range(len(saved_uploads)),
            key=lambda index: inheritance_upload_order_key(
                relative_paths[index], saved_uploads[index][0],
            ),
        )
        saved_uploads = [saved_uploads[index] for index in order]
        relative_paths = [relative_paths[index] for index in order]

    batch.uploads = []
    for index, (filename, path, content_sha256) in enumerate(saved_uploads):
        batch.uploads.append({
            "paper_id": uuid.uuid4().hex[:12],
            "filename": filename,
            "source_relative_path": relative_paths[index],
            "image_path": path,
            "original_image_path": path,
            "source_sha256": content_sha256,
            "working_sha256": content_sha256,
            "manual_quarter_turns": 0,
            "orientation_history": [],
            **_client_ocr_ready_orientation(filename),
        })
    batch.ensure_ocr_jobs(batch.uploads)
    batch.status = "recognizing_remaining"
    return saved_uploads


def _freeze_template_for_batch(
    batch: QuickBatch,
    template_id: str,
    teacher_id: int,
    category: str,
    exam_name: str,
) -> tuple[TemplateSession, PaperCache]:
    """Under the template lock: revalidate, build the visual evidence, freeze.

    Invalid visual evidence marks the template for reconfirmation (409).
    """
    with _locked_owned_template_session(template_id, teacher_id) as latest_template:
        _validate_template_for_recognition(
            latest_template,
            category,
            exam_name,
            persist_reconfirmation=True,
        )
        try:
            paper_cache = _build_template_paper_cache(latest_template)
        except HTTPException as exc:
            latest_template.requires_reconfirmation = True
            latest_template.reconfirmation_reason = (
                "模板视觉基准无效，请老师重新确认模板"
            )
            latest_template.save_meta()
            raise HTTPException(
                409,
                detail=latest_template.reconfirmation_reason,
            ) from exc
        if latest_template.status == "confirmed":
            latest_template.status = "frozen"
            latest_template.save_meta()
        batch.template_mode = str(
            latest_template.template_mode or batch.template_mode or "marked"
        )
        return latest_template, paper_cache


def _reserve_batch_images(limiter, teacher_id: int, batch: QuickBatch, saved_uploads: list[tuple]) -> None:
    """Reserve today's allowance for the saved pages; a refusal discards the batch."""
    reservation = (
        limiter.reserve_student_images(
            teacher_id,
            len(saved_uploads),
            batch_id=f"student-upload-{batch.batch_id}",
        )
        if limiter is not None
        else {"allowed": True}
    )
    if not reservation["allowed"]:
        _discard_unstarted_batch(batch, saved_uploads)
        raise HTTPException(403, detail={
            "error": "DAILY_IMAGE_LIMIT_EXCEEDED",
            "message": "今日学生试卷图片额度不足，请减少本次上传张数",
            **reservation,
        })


def _fail_unrecognized_batch(batch: QuickBatch) -> dict:
    """No page of a new batch was transcribed: fail it, keep its files listed."""
    batch.status = "failed"
    batch.error = "所有试卷识别失败"
    return {
        "message": "本批次所有试卷均未识别成功。请检查视觉模型服务、网络或降低并发后重试；失败文件已保留在列表中。",
    }


def _initial_recognition_paper_payload(batch: QuickBatch, paper: dict) -> dict:
    """Final paper row of a first run.

    Unlike ``_recognition_paper_payload`` (used by a resume) it passes stored
    values through uncoerced; kept separate so that neither stream changes.
    """
    matched = 0 if paper.get("ocr_failure") else sum(
        1 for q in paper["questions"]
        if batch.answer_map.get(q["question_no"], {}).get("matched")
    )
    paper_payload = {
        "batch_id": batch.batch_id,
        "filename": paper["filename"],
        "student_name": paper["student_name"],
        "student_no": paper.get("student_no", ""),
        "paper_id": paper.get("paper_id", ""),
        "identity": paper.get("identity", {}),
        "questions_count": len(paper["questions"]),
        "matched_count": matched,
        "orientation_status": paper.get("orientation_status", "needs_review"),
        "orientation_reason": paper.get("orientation_reason", ""),
        "detected_tilt_degrees": paper.get("detected_tilt_degrees"),
    }
    if paper.get("ocr_failure"):
        paper_payload.update(_ocr_failure_payload(paper))
    return paper_payload


async def _initial_recognition_events(
    batch: QuickBatch,
    template_session: TemplateSession,
    paper_cache: PaperCache,
    failure_questions: list[dict],
    teacher_id: int,
    mode: str,
):
    """SSE of a new batch: inspect, recognize and grade every upload, then finalize.

    A disconnect leaves the batch ``recognition_interrupted`` for a resume; any
    other failure leaves it ``failed``.  Finished OCR and grades stay saved.
    """
    lease = _RecognitionLease(batch.batch_id, teacher_id)
    lease.set_vision_phase("initial-recognition")
    lease.set_vision_batch_size(len(batch.uploads))
    run = _RecognitionRun(
        batch, teacher_id, _INITIAL_RECOGNITION, lease.db, total=len(batch.uploads),
    )
    try:
        if not lease.acquire_batch_lock():
            yield _sse("error", run.busy_payload())
            return
        yield _sse("progress", run.started_payload())
        inspected = _inspected_upload_count(batch)
        async for inspected_upload in _bounded_batch_upload_inspections(batch, batch.uploads):
            if inspected_upload is not None:
                inspected += 1
            yield _sse("progress", run.inspection_payload(
                inspected, waiting=inspected_upload is None,
            ))

        roster_names = _class_roster_names(lease.db, batch, teacher_id)
        run.pipeline = _start_per_paper_grading(batch, lease.db, teacher_id, template_session)
        remaining = list(batch.uploads)
        if remaining:
            yield _sse("progress", run.progress_payload(f"排队识别剩余 {len(remaining)} 张"))
            async with aclosing(_ocr_phase_events(
                run, remaining, paper_cache, failure_questions, roster_names,
            )) as events:
                async for event in events:
                    yield event
        async with aclosing(_grading_drain_events(run)) as events:
            async for event in events:
                yield event

        accelerator_pause = _pause_batch_for_accelerator(batch)
        if accelerator_pause is not None:
            await _save_quick_batch_meta(batch)
            yield _sse("error", accelerator_pause)
            return

        # ── 建 answer_map ──
        _initialize_batch_answer_map(batch, teacher_id, template_session)
        if not any(paper.get("questions") for paper in batch.papers):
            failure = _fail_unrecognized_batch(batch)
            await _save_quick_batch_meta(batch)
            yield _sse("error", failure)
            return
        for paper in batch.papers:
            yield _sse("paper", _initial_recognition_paper_payload(batch, paper))
        questions_out, ocr_summary = _complete_recognition(batch, clear_error=False)
        await _save_quick_batch_meta(batch)

        yield _sse("done", {
            "batch_id": batch.batch_id,
            "status": batch.status,
            "teaching_context": copy.deepcopy(batch.teaching_context),
            "mode": mode,
            "total_papers": len(batch.papers),
            "questions": questions_out,
            **ocr_summary,
        })
    except (asyncio.CancelledError, GeneratorExit):
        if batch.status == "recognizing_remaining":
            await run.checkpoint_stop(
                "recognition_interrupted", _RECOGNITION_DISCONNECTED,
                log_message="failed to checkpoint interrupted recognition %s",
            )
        raise
    except Exception as e:
        info = report_failure(e, stage="transcription", task_id=batch.batch_id)
        await run.checkpoint_stop(
            "failed", info["message"],
            log_message="failed to checkpoint recognition failure %s",
        )
        yield _sse("error", {"message": info["message"], "error_info": info})
    finally:
        await lease.release(run.pipeline)


_OCR_RESUMABLE_BATCH_STATES = {
    "recognizing_remaining",
    "recognition_interrupted",
    "ocr_partial_failed",
    "failed",
}


def _ocr_failure_payload(paper: dict) -> dict:
    """Failed-page row fields; keep the persisted diagnostic id visible."""
    error = str(paper.get("ocr_error") or "OCR 识别失败")
    info = paper.get("error_info") if isinstance(paper.get("error_info"), dict) else {}
    correlation_id = str(info.get("correlation_id") or "")
    if re.fullmatch(r"[a-f0-9]{32}", correlation_id) and correlation_id not in error:
        error = f"{error}（诊断编号 {correlation_id}）"
    payload = {"error": error, "requires_manual_review": True}
    if info:
        payload["error_info"] = dict(info)
    return payload


def _recognition_paper_payload(batch: QuickBatch, paper: dict) -> dict:
    matched = 0 if paper.get("ocr_failure") else sum(
        1
        for question in paper.get("questions") or []
        if batch.answer_map.get(str(question.get("question_no") or ""), {}).get("matched")
    )
    payload = {
        "batch_id": batch.batch_id,
        "filename": str(paper.get("filename") or ""),
        "student_name": str(paper.get("student_name") or ""),
        "student_no": str(paper.get("student_no") or ""),
        "paper_id": str(paper.get("paper_id") or ""),
        "identity": dict(paper.get("identity") or {}),
        "questions_count": len(paper.get("questions") or []),
        "matched_count": matched,
        "orientation_status": paper.get("orientation_status", "needs_review"),
        "orientation_reason": str(paper.get("orientation_reason") or ""),
        "detected_tilt_degrees": paper.get("detected_tilt_degrees"),
    }
    if paper.get("ocr_failure"):
        payload.update(_ocr_failure_payload(paper))
    return payload


def _pause_batch_for_accelerator(batch: QuickBatch) -> dict | None:
    """Persist offloaded image work as resumable instead of an OCR failure."""
    jobs = [
        job
        for job in batch.ocr_jobs
        if str(job.get("status") or "") == "queued"
        and str(job.get("_accelerator_state") or "") in {"pending", "unavailable"}
    ]
    if not jobs:
        return None

    unavailable = any(
        str(job.get("_accelerator_state") or "") == "unavailable"
        for job in jobs
    )
    code = "ACCELERATOR_UNAVAILABLE" if unavailable else "ACCELERATOR_WAITING"
    message = (
        "加速节点不可用，请使用客户端"
        if unavailable
        else "加速节点正在处理，进度已保存；请稍后继续，或使用客户端。"
    )
    sync_pending_recognition_uploads(batch)
    batch.status = "recognition_interrupted"
    batch.error = message
    return {
        "batch_id": batch.batch_id,
        "code": code,
        "message": message,
        "pending": len(batch.pending_uploads),
        **batch_ocr_summary(batch),
    }


# ── Recognition resume ───────────────────────────────────────────────────────


@router.post("/batch-grade/{batch_id}/recognize/resume")
async def batch_grade_resume_recognition(
    batch_id: str,
    user: User = Depends(_require_user),
):
    """Resume unfinished OCR jobs from the durable per-batch ledger."""
    batch = QuickBatch.load_meta(batch_id, UPLOAD_DIR)
    _require_owned_batch(batch, user)
    _require_resumable_recognition(batch)

    # Template evidence can be produced by the server matcher when no
    # opportunity node is available, so recovery checks only the core grading
    # services.  The per-page accelerator handoff remains atomic in PaperCache.
    await _require_batch_grade_chain(user)

    return _sse_response(
        batch.teaching_context, _resumed_recognition_events(batch_id, batch, user),
    )


def _require_resumable_recognition(batch: QuickBatch) -> None:
    """HTTP preflight: only an unfinished template recognition can resume."""
    if str(batch.source_mode or "").strip().lower() != "template":
        raise HTTPException(
            409,
            detail="该批次缺少有效的模板流程标识，请重新上传试卷",
        )
    if not str(batch.template_id or "").strip():
        raise HTTPException(409, detail="批次缺少冻结模板，不能恢复识别")
    # The per-paper pipeline checkpoints results while recognition is still
    # running, so having results does not mean the batch reached grading. This
    # status gate alone excludes grading/review batches; rubric version stamps
    # make replaying already graded papers idempotent.
    if batch.status not in _OCR_RESUMABLE_BATCH_STATES:
        raise HTTPException(409, detail="该批次当前不需要恢复识别")


def _resume_refusal(batch: QuickBatch) -> dict | None:
    """The preflight again, on the checkpoint reloaded under the batch lock."""
    if str(batch.source_mode or "").strip().lower() != "template":
        return {
            "code": "BATCH_SOURCE_MODE_INVALID",
            "message": "该批次缺少有效的模板流程标识，请重新上传试卷",
        }
    if not str(batch.template_id or "").strip():
        return {
            "code": "BATCH_TEMPLATE_REQUIRED",
            "message": "批次缺少冻结模板，不能恢复识别",
        }
    if batch.status not in _OCR_RESUMABLE_BATCH_STATES:
        return {
            "code": "BATCH_NOT_RESUMABLE",
            "message": "该批次状态已变化，请刷新页面查看最新结果",
        }
    return None


def _ocr_attempts_exhausted(batch: QuickBatch) -> bool:
    """True when every failed OCR job has used up its automatic attempts."""
    failed_jobs = [
        job for job in batch.ocr_jobs
        if str(job.get("status") or "") == "failed"
    ]
    return bool(
        failed_jobs
        and all(
            ocr_job_attempts(job) >= OCR_MAX_JOB_ATTEMPTS
            for job in failed_jobs
        )
    )


def _stop_resume_without_work(batch: QuickBatch, summary: dict) -> dict:
    """Nothing is queued and the ledger cannot be graded: settle it and say why."""
    exhausted = _ocr_attempts_exhausted(batch)
    batch.status = (
        "ocr_partial_failed"
        if summary["succeeded_papers"] or summary["manual_review_papers"]
        else "failed"
    )
    batch.pending_uploads = []
    batch.error = "没有可自动恢复的识别任务"
    return {
        "code": (
            "OCR_RETRY_EXHAUSTED"
            if exhausted
            else "OCR_NO_RETRYABLE_FAILURES"
        ),
        "message": (
            "可恢复识别次数已用尽；请检查原图，若均正常请联系管理员检查模型服务。"
            if exhausted
            else "当前失败项不能自动恢复，请检查原图或联系管理员。"
        ),
        **summary,
    }


def _restart_recognition(run: _RecognitionRun) -> None:
    """Reopen the ledger for this resume and count the jobs already finished."""
    batch = run.batch
    run.total = len(batch.uploads)
    run.processed = sum(
        str(job.get("status") or "") in OCR_JOB_TERMINAL_STATES
        for job in batch.ocr_jobs
    )
    batch.status = "recognizing_remaining"
    batch.error = None
    sync_pending_recognition_uploads(batch)


def _redispatch_checkpointed_papers(pipeline: "_PerPaperGradingPipeline", batch: QuickBatch) -> None:
    """Grade papers whose OCR was saved before their grading task was cancelled.

    Resume only terminal pages; dispatch's rubric/identity gates preserve
    valid results and leave uncertain identity for review.
    """
    terminal_paths = {
        os.path.realpath(str(job.get("image_path") or ""))
        for job in batch.ocr_jobs
        if job.get("status") in {"succeeded", "needs_review"}
        and job.get("image_path")
    }
    for paper in batch.papers:
        if os.path.realpath(str(paper.get("image_path") or "")) in terminal_paths:
            pipeline.dispatch(paper)


def _fail_unrecognized_resume(batch: QuickBatch) -> dict:
    """The resumed OCR still left every page without a transcription."""
    ocr_summary = batch_ocr_summary(batch)
    exhausted = _ocr_attempts_exhausted(batch)
    batch.status = "failed"
    batch.error = (
        "可恢复识别次数已用尽"
        if exhausted
        else "所有试卷识别失败"
    )
    return {
        "code": "OCR_RETRY_EXHAUSTED" if exhausted else "VISION_UNAVAILABLE",
        "message": (
            "可恢复识别次数已用尽；请检查原图，若均正常请联系管理员检查模型服务。"
            if exhausted
            else "本批次所有试卷均未识别成功，请检查视觉识别服务后重试。"
        ),
        **ocr_summary,
    }


async def _resumed_recognition_events(batch_id: str, batch: QuickBatch, user: User):
    """SSE of a resume: rerun the queued OCR jobs, grade, then finalize.

    ``batch`` is the preflight snapshot; the stream reloads the checkpoint once
    it holds the batch lock.  A disconnect or failure before the outcome is
    persisted leaves the batch ``recognition_interrupted`` for the next resume.
    """
    teacher_id = user.id
    lease = _RecognitionLease(batch_id, teacher_id)
    run = _RecognitionRun(batch, teacher_id, _RESUMED_RECOGNITION, lease.db)
    try:
        if not lease.acquire_batch_lock():
            yield _sse("error", run.busy_payload())
            return

        batch = run.batch = QuickBatch.load_meta(batch_id, UPLOAD_DIR)
        _require_owned_batch(batch, user)
        batch.recover_interrupted_ocr_jobs(max_attempts=OCR_MAX_JOB_ATTEMPTS)
        refusal = _resume_refusal(batch)
        if refusal is not None:
            yield _sse("error", refusal)
            return

        finalize_exhausted_ocr_jobs(batch)
        requeue_retryable_ocr_failures(batch)
        summary = batch_ocr_summary(batch)
        # A disconnect can happen after the final OCR checkpoint but before
        # confirmation/done. Reuse that complete ledger and run normal
        # finalization; no queued OCR is not itself a recognition failure.
        if not queued_recognition_uploads(batch) and not summary["can_grade"]:
            stopped = _stop_resume_without_work(batch, summary)
            await _save_quick_batch_meta(batch)
            yield _sse("error", stopped)
            run.completed = True
            return
        batch.recognition_resume_generation += 1
        await _save_quick_batch_meta(batch)
        lease.set_vision_phase(
            f"recognition-resume:{batch.recognition_resume_generation}",
            retry_scope_key="recognition-resume",
        )

        template_session = _load_owned_template_session(batch.template_id, teacher_id)
        paper_cache = _build_template_paper_cache(template_session)
        failure_questions = _single_page_template_failure_questions(template_session)
        pending = queued_recognition_uploads(batch)
        if pending:
            lease.set_vision_batch_size(len(pending))
        _restart_recognition(run)
        await _save_quick_batch_meta(batch)
        yield _sse("progress", run.started_payload())

        inspected = _inspected_upload_count(batch)
        async for inspected_upload in _bounded_batch_upload_inspections(batch, pending):
            if inspected_upload is not None:
                inspected += 1
            yield _sse("progress", run.inspection_payload(
                inspected, waiting=inspected_upload is None,
            ))

        roster_names = _class_roster_names(lease.db, batch, teacher_id)
        # 逐卷流水批阅：同 initial recognize——识别定稿一张就批一张。
        run.pipeline = _start_per_paper_grading(batch, lease.db, teacher_id, template_session)
        _redispatch_checkpointed_papers(run.pipeline, batch)
        if pending:
            # Same in-flight window as initial recognition. Each slot spends
            # most of its life waiting on the accelerator node or the OCR
            # provider; the native template matcher keeps its own preprocess
            # gate in PaperCache and request memory stays on the byte budget.
            async with aclosing(_ocr_phase_events(
                run, pending, paper_cache, failure_questions, roster_names,
            )) as events:
                async for event in events:
                    yield event
        async with aclosing(_grading_drain_events(run)) as events:
            async for event in events:
                yield event

        accelerator_pause = _pause_batch_for_accelerator(batch)
        if accelerator_pause is not None:
            await _save_quick_batch_meta(batch)
            yield _sse("error", accelerator_pause)
            run.completed = True
            return

        _initialize_batch_answer_map(batch, teacher_id, template_session)
        if not any(paper.get("questions") for paper in batch.papers):
            failure = _fail_unrecognized_resume(batch)
            await _save_quick_batch_meta(batch)
            yield _sse("error", failure)
            run.completed = True
            return
        for paper in batch.papers:
            yield _sse("paper", _recognition_paper_payload(batch, paper))
        questions_out, ocr_summary = _complete_recognition(batch, clear_error=True)
        await _save_quick_batch_meta(batch)
        run.completed = True
        yield _sse("done", {
            "batch_id": batch.batch_id,
            "status": batch.status,
            "mode": batch.grade_mode,
            "total_papers": len(batch.papers),
            "questions": questions_out,
            **ocr_summary,
        })
    except (asyncio.CancelledError, GeneratorExit):
        if not run.completed and run.batch.status in _OCR_RESUMABLE_BATCH_STATES:
            await run.checkpoint_stop(
                "recognition_interrupted", _RECOGNITION_DISCONNECTED,
                log_message="failed to checkpoint interrupted recognition %s",
            )
        raise
    except Exception as exc:
        info = report_failure(exc, stage="transcription", task_id=run.batch.batch_id)
        if run.batch.status in _OCR_RESUMABLE_BATCH_STATES:
            await run.checkpoint_stop(
                "recognition_interrupted", str(exc),
                log_message="failed to checkpoint recognition failure %s",
            )
        yield _sse("error", {
            "code": "GRADING_CHAIN_FAILED",
            "message": "识别恢复链路中断，已完成结果已保存，请稍后重试。",
            "error_info": info,
        })
    finally:
        await lease.release(run.pipeline)


@router.get("/batch-grade/{batch_id}/identity-options")
def batch_grade_identity_options(
    batch_id: str,
    user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    """Return the live roster for the class frozen into an owned batch."""
    batch = QuickBatch.load_meta(batch_id, UPLOAD_DIR)
    _require_owned_batch(batch, user)
    if batch.identity_mode == "anonymous":
        raise HTTPException(409, detail="无身份批阅不使用学生名单")
    students = db.query(Student).filter(
        Student.teacher_id == user.id,
        Student.class_id == batch.class_id,
    ).order_by(Student.id.asc()).all()
    return {
        "batch_id": batch.batch_id,
        "class_id": batch.class_id,
        "students": [
            {
                "student_id": student.id,
                "name": student.name,
                "student_no": student.student_no or "",
                "class_id": student.class_id,
            }
            for student in students
        ],
    }


def _orientation_state_payload(batch: QuickBatch) -> dict:
    papers_by_id = {
        str(item.get("paper_id") or ""): item for item in batch.papers
    }
    jobs_by_id = {
        str(item.get("paper_id") or ""): item for item in batch.ocr_jobs
        if item.get("paper_id")
    }
    frozen_identity_plan = (
        batch.grading_contract.get("identity_plan") or {}
        if isinstance(batch.grading_contract, dict)
        else {}
    )
    rows = []
    for upload in batch.uploads:
        paper_id = str(upload.get("paper_id") or "")
        paper = papers_by_id.get(paper_id, {})
        job = jobs_by_id.get(paper_id)
        if job is None:
            active_path = str(upload.get("image_path") or "")
            job = next(
                (
                    item for item in batch.ocr_jobs
                    if str(item.get("image_path") or "") == active_path
                ),
                {},
            )
        merged = {**paper, **upload}
        assignment = frozen_identity_plan.get(paper_id) or {}
        questions = paper.get("questions") or []
        matched = sum(
            1 for question in questions
            if batch.answer_map.get(str(question.get("question_no") or ""), {}).get("matched")
        )
        row = {
            "batch_id": batch.batch_id,
            "paper_id": paper_id,
            "filename": str(upload.get("filename") or paper.get("filename") or "unknown"),
            "student_name": str(paper.get("student_name") or ""),
            "student_no": str(paper.get("student_no") or ""),
            "identity": paper.get("identity") or {},
            # 让老师看得见身份线索是"纸上 OCR"还是"文件名"，便于核对贴错图的情况。
            "identity_clue_source": str(paper.get("identity_clue_source") or ""),
            "identity_resolution": (
                {
                    "paper_id": paper_id,
                    "action": str(assignment.get("action") or ""),
                    "student_id": assignment.get("student_id"),
                    "confirmed_name": (
                        str(assignment.get("name") or "")
                        if assignment.get("action") == "create" else ""
                    ),
                    "confirmed_student_no": (
                        str(assignment.get("student_no") or "")
                        if assignment.get("action") == "create" else ""
                    ),
                }
                if assignment else None
            ),
            "questions_count": len(questions),
            "matched_count": matched,
            "ocr_status": str(job.get("status") or "queued"),
            "error": (
                _EXCLUDED_PAPER_MESSAGE
                if job.get("status") == "excluded"
                else str(paper.get("ocr_error") or job.get("last_error") or "")
            ),
            "requires_manual_review": (
                not orientation_is_ready(merged)
                or str(job.get("status") or "") == "needs_review"
            ),
            "manual_quarter_turns": int(upload.get("manual_quarter_turns") or 0),
            "orientation_confirmed_at": str(upload.get("orientation_confirmed_at") or ""),
            "original_preserved": bool(
                upload.get("original_image_path") and upload.get("source_sha256")
            ),
            **_orientation_diagnostics(merged),
        }
        rows.append(row)

    summary = batch_ocr_summary(batch)
    orientation_ready = bool(rows) and all(orientation_is_ready(row) for row in rows)
    summary["can_grade"] = bool(summary["can_grade"] and orientation_ready)
    return {
        "batch_id": batch.batch_id,
        "teaching_context": copy.deepcopy(batch.teaching_context),
        "status": batch.status,
        "identity_mode": batch.identity_mode,
        "identity_inheritance": batch.identity_inheritance,
        "category": batch.category,
        "exam_name": batch.exam_name,
        "mode": batch.grade_mode,
        "source_mode": batch.source_mode,
        "template_id": batch.template_id,
        "template_mode": batch.template_mode,
        "papers": rows,
        "questions": batch.confirmation_questions if summary["can_grade"] else [],
        **summary,
    }


@router.get("/batch-grade/{batch_id}/orientation")
def batch_grade_orientation_state(
    batch_id: str,
    user: User = Depends(_require_user),
):
    """Hydrate an owned orientation review after refresh or navigation."""
    batch = QuickBatch.load_meta(batch_id, UPLOAD_DIR)
    _require_owned_batch(batch, user)
    return _orientation_state_payload(batch)


_EXCLUDED_PAPER_MESSAGE = "已由教师排除，不参与本批批阅"


@router.post("/batch-grade/{batch_id}/paper/{paper_id}/exclude")
async def batch_grade_exclude_failed_paper(
    batch_id: str,
    paper_id: str,
    user: User = Depends(_require_user),
):
    """Let the teacher remove one unrecoverable OCR failure from this batch.

    Without this, a single page whose OCR failed for good (for example a photo
    that matches no page of a multi-page template) blocks grading for every
    other paper. Only an explicit teacher action resolves it: the page is never
    graded, never written to records, and stays listed as excluded.
    """
    try:
        with batch_operation_lock(batch_id, UPLOAD_DIR):
            batch = QuickBatch.load_meta(batch_id, UPLOAD_DIR)
            _require_owned_batch(batch, user)
            if str(batch.source_mode or "").strip().lower() != "template" or not batch.template_id:
                raise HTTPException(409, detail="只有模板批次可以排除识别失败的试卷")
            job = next(
                (
                    item for item in batch.ocr_jobs
                    if str(item.get("paper_id") or "") == paper_id
                ),
                None,
            )
            if job is None:
                raise HTTPException(404, detail="试卷不存在或已被清理")
            if job.get("status") == "excluded":
                # A repeated click (or a lost response) returns the durable state.
                return _orientation_state_payload(batch)
            if batch.status not in {"ocr_partial_failed", "failed"}:
                raise HTTPException(409, detail="只有识别未完成的批次可以排除失败试卷")
            if job.get("status") != "failed":
                raise HTTPException(409, detail="只能排除识别失败的试卷")
            if bool(job.get("retryable")) and ocr_job_attempts(job) < OCR_MAX_JOB_ATTEMPTS:
                raise HTTPException(409, detail="该试卷仍可自动重试，请先继续处理未完成试卷")
            if any(str(paper.get("paper_id") or "") == paper_id for paper in batch.papers):
                raise HTTPException(409, detail="该试卷已有识别结果，不能排除")

            job.update({
                "status": "excluded",
                "retryable": False,
                "excluded_by": user.id,
                "excluded_at": datetime.now(timezone.utc).isoformat(),
            })
            # An excluded page no longer breaks the inheritance chain.
            _refresh_identity_inheritance(batch)
            summary = batch_ocr_summary(batch)
            if summary["can_grade"] and all(
                orientation_is_ready(paper) for paper in batch.papers
            ):
                template_session = _load_owned_template_session(batch.template_id, user.id)
                _initialize_batch_answer_map(batch, user.id, template_session)
                batch.confirmation_questions = _confirmation_questions(batch)
                batch.status = "ocr_done"
                batch.error = None
            batch.pending_uploads = []
            await _save_quick_batch_meta(batch)
            logger.info(
                "教师排除失败试卷 batch=%s paper=%s excluded=%d can_grade=%s",
                batch.batch_id, paper_id, summary["excluded_papers"], summary["can_grade"],
            )
            return _orientation_state_payload(batch)
    except BatchOperationBusy as exc:
        raise HTTPException(409, detail="该批次正在执行另一项操作，请稍后重试") from exc


async def _save_client_orientation_upload(
    upload: UploadFile,
    *,
    batch_dir: str,
    paper_id: str,
    action_id: str,
    source_sha256: str,
    quarter_turns_clockwise: int,
) -> tuple[str, str]:
    """Persist a browser-rendered rotation without decoding image pixels."""
    filename = os.path.basename(str(upload.filename or ""))
    if not filename.lower().endswith(".ocr-ready-v1.jpg"):
        raise HTTPException(
            400,
            detail={
                "error": "CLIENT_IMAGE_PREPROCESS_REQUIRED",
                "message": "方向确认必须提交浏览器生成的 .ocr-ready-v1.jpg 文件",
            },
        )

    root = Path(batch_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    paper_token = hashlib.sha256(paper_id.encode("utf-8")).hexdigest()[:12]
    action_token = hashlib.sha256(action_id.encode("utf-8")).hexdigest()[:12]
    destination = root / (
        f"teacher-oriented-{paper_token}-{source_sha256[:16]}-"
        f"q{quarter_turns_clockwise}-{action_token}.ocr-ready-v1.jpg"
    )
    temporary = root / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    try:
        size, content_sha256 = await _stream_to_disk_capped_with_sha256(
            upload,
            str(temporary),
            MAX_UPLOAD_FILE_SIZE,
        )
        if size < 0:
            raise HTTPException(413, detail="方向确认文件超过大小限制")
        if size == 0:
            raise HTTPException(400, detail="方向确认文件为空")
        validation = await asyncio.to_thread(
            _validate_client_ocr_ready_upload,
            filename,
            str(temporary),
        )
        if not validation.get("safe"):
            raise HTTPException(
                400,
                detail={
                    "error": "CLIENT_IMAGE_PREPROCESS_REQUIRED",
                    "message": validation.get("reason") or "客户端方向文件校验失败",
                },
            )
        if destination.exists():
            existing_hash = await asyncio.to_thread(sha256_file, str(destination))
            if existing_hash != content_sha256:
                raise HTTPException(409, detail="同一方向确认请求对应的客户端文件已变化")
            temporary.unlink(missing_ok=True)
        else:
            os.replace(str(temporary), str(destination))
        return str(destination), content_sha256
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(503, detail="服务器暂时无法保存方向确认文件") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


@router.post("/batch-grade/{batch_id}/paper/{paper_id}/orientation-confirm")
async def batch_grade_confirm_orientation(
    batch_id: str,
    paper_id: str,
    action_id: str = Form(...),
    quarter_turns_clockwise: int = Form(...),
    file: UploadFile | None = File(None),
    user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    """Persist a client-rendered teacher rotation, then retry one paper."""
    action_id = _orientation_action_id(action_id, quarter_turns_clockwise)
    try:
        with batch_operation_lock(batch_id, UPLOAD_DIR):
            return await _confirm_orientation_locked(
                batch_id,
                paper_id,
                action_id,
                quarter_turns_clockwise,
                file,
                user,
                db,
            )
    except BatchOperationBusy as exc:
        raise HTTPException(409, detail="该批次正在执行另一项操作，请稍后重试") from exc


def _orientation_action_id(action_id: str, quarter_turns_clockwise: int) -> str:
    """Validate the request before taking the batch lock; returns the action id."""
    action_id = str(action_id or "").strip()
    if not (8 <= len(action_id) <= 80) or not action_id.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(400, detail="action_id 格式无效")
    if quarter_turns_clockwise not in {0, 1, 2, 3}:
        raise HTTPException(400, detail="旋转角度必须是 0/90/180/270 度")
    return action_id


async def _confirm_orientation_locked(
    batch_id: str,
    paper_id: str,
    action_id: str,
    quarter_turns_clockwise: int,
    file: UploadFile | None,
    user: User,
    db: Session,
) -> dict:
    """The confirmation itself; the caller holds the batch operation lock."""
    batch = QuickBatch.load_meta(batch_id, UPLOAD_DIR)
    _require_owned_batch(batch, user)
    _require_orientation_editable(batch)
    upload = _batch_upload_or_404(batch, paper_id)

    history, existing = _orientation_action(upload, action_id, quarter_turns_clockwise)
    if existing is not None and existing.get("status") == "complete":
        return _orientation_state_payload(batch)
    if file is None:
        raise HTTPException(
            400,
            detail={
                "error": "CLIENT_ORIENTATION_UPLOAD_REQUIRED",
                "message": "请在浏览器完成旋转后重新提交方向文件",
            },
        )
    _require_orientation_change(batch, upload, paper_id)

    template_session, paper_cache = _orientation_template_evidence(batch, user.id)
    current_source_hash = await _verified_original_source_hash(batch, upload)
    _require_rotatable_page(upload)
    working_path, working_sha256 = await _save_client_orientation_upload(
        file,
        batch_dir=batch.batch_dir,
        paper_id=paper_id,
        action_id=action_id,
        source_sha256=current_source_hash,
        quarter_turns_clockwise=quarter_turns_clockwise,
    )

    event, confirmed_orientation, history = _begin_orientation_retry(
        batch,
        upload,
        history,
        existing,
        paper_id=paper_id,
        action_id=action_id,
        quarter_turns_clockwise=quarter_turns_clockwise,
        teacher_id=user.id,
        source_sha256=current_source_hash,
        working_path=working_path,
        working_sha256=working_sha256,
    )
    await _save_quick_batch_meta(batch)

    roster_names = _class_roster_names(db, batch, user.id)
    result = await _recognize_reoriented_upload(
        batch,
        upload,
        paper_cache,
        confirmed_orientation,
        teacher_id=user.id,
        paper_id=paper_id,
        action_id=action_id,
        roster_names=roster_names,
    )
    _finish_orientation_retry(
        batch, upload, event, history, result, template_session, db,
        teacher_id=user.id, paper_id=paper_id,
    )
    await _save_quick_batch_meta(batch)

    return _orientation_state_payload(batch)


def _require_orientation_editable(batch: QuickBatch) -> None:
    """Only a batch between recognition and grading may change a page direction."""
    if batch.review_results or batch.status in {"grading", "review_ready", "done"}:
        raise HTTPException(409, detail="本批次已进入批阅或审阅阶段，不能再修改试卷方向")
    if batch.status not in {
        "failed",
        "ocr_partial_failed",
        "ocr_done",
        "orientation_retrying",
    }:
        raise HTTPException(409, detail="请先等待当前识别或页型基准确认流程结束")


def _batch_upload_or_404(batch: QuickBatch, paper_id: str) -> dict:
    upload = next(
        (
            item for item in batch.uploads
            if str(item.get("paper_id") or "") == paper_id
        ),
        None,
    )
    if upload is None:
        raise HTTPException(404, detail="试卷不存在或已被清理")
    return upload


def _orientation_action(
    upload: dict,
    action_id: str,
    quarter_turns_clockwise: int,
) -> tuple[list[dict], dict | None]:
    """The page's direction history and this action's earlier event, if any.

    Replaying an action id with other turns is refused.
    """
    history = list(upload.get("orientation_history") or [])
    existing = next(
        (item for item in history if str(item.get("action_id") or "") == action_id),
        None,
    )
    if existing is not None:
        if int(existing.get("quarter_turns_clockwise") or 0) != quarter_turns_clockwise:
            raise HTTPException(409, detail="同一方向确认请求不能改用其他旋转角度")
    return history, existing


def _require_orientation_change(batch: QuickBatch, upload: dict, paper_id: str) -> None:
    """A cleared direction may be rotated again only while its OCR retry is unfinished."""
    job = next(
        (
            item for item in batch.ocr_jobs
            if str(item.get("paper_id") or "") == paper_id
        ),
        {},
    )
    if orientation_is_ready(upload) and not (
        upload.get("orientation_status") == "teacher_confirmed"
        and str(job.get("status") or "") in {"queued", "running", "failed"}
    ):
        raise HTTPException(409, detail="该试卷方向已经确认，无需重复旋转")


def _orientation_template_evidence(
    batch: QuickBatch,
    teacher_id: int,
) -> tuple[TemplateSession, PaperCache]:
    """The batch's frozen template and its visual evidence cache."""
    if batch.source_mode != "template" or not batch.template_id:
        raise HTTPException(409, detail="旧批次缺少模板快照标识，请重新上传本批试卷")
    template_session = _load_owned_template_session(batch.template_id, teacher_id)
    paper_cache = _build_template_paper_cache(template_session)
    return template_session, paper_cache


async def _verified_original_source_hash(batch: QuickBatch, upload: dict) -> str:
    """Hash of the preserved original; it must still be inside the batch and unchanged."""
    batch_root = os.path.realpath(batch.batch_dir)
    original_path = os.path.realpath(str(
        upload.get("original_image_path") or upload.get("image_path") or ""
    ))
    if (
        not original_path.startswith(batch_root + os.sep)
        or not os.path.isfile(original_path)
    ):
        raise HTTPException(404, detail="原始试卷已被清理，无法旋转")
    current_source_hash = await asyncio.to_thread(sha256_file, original_path)
    stored_source_hash = str(upload.get("source_sha256") or "")
    if stored_source_hash and stored_source_hash != current_source_hash:
        raise HTTPException(409, detail="原始试卷内容已变化，请重新上传")
    return current_source_hash


def _require_rotatable_page(upload: dict) -> None:
    if upload.get("orientation_reason") == "page_tilt_exceeds_limit":
        raise HTTPException(
            409,
            detail={
                "error": "ORIENTATION_STILL_INVALID",
                "message": "该页面存在连续倾斜，90 度旋转无法修正，请重新拍摄。",
            },
        )


def _begin_orientation_retry(
    batch: QuickBatch,
    upload: dict,
    history: list[dict],
    existing: dict | None,
    *,
    paper_id: str,
    action_id: str,
    quarter_turns_clockwise: int,
    teacher_id: int,
    source_sha256: str,
    working_path: str,
    working_sha256: str,
) -> tuple[dict, dict, list[dict]]:
    """Switch the page to the teacher-rotated file and reopen its OCR job.

    Returns ``(event, confirmed_orientation, history)``; the event stays
    ``ocr_running`` until the retry has finished.
    """
    working = {
        "image_path": working_path,
        "working_sha256": working_sha256,
        "manual_quarter_turns": quarter_turns_clockwise,
    }
    diagnostic = {
        **_client_ocr_ready_orientation(os.path.basename(working_path)),
        "orientation_coordinate_space": "client_teacher_rotated_pixels",
    }

    confirmed_at = datetime.now(timezone.utc).isoformat()
    event = existing or {
        "action_id": action_id,
        "quarter_turns_clockwise": quarter_turns_clockwise,
        "teacher_id": teacher_id,
        "created_at": confirmed_at,
    }
    event.update({
        "status": "ocr_running",
        "source_sha256": source_sha256,
        "working_sha256": working["working_sha256"],
        "detector": diagnostic,
    })
    history = [
        item for item in history
        if str(item.get("action_id") or "") != action_id
    ]
    history.append(event)

    old_path = str(upload.get("image_path") or "")
    confirmed_orientation = {
        **diagnostic,
        "orientation_status": "teacher_confirmed",
        "orientation_reason": "teacher_confirmed",
    }
    upload.update({
        **working,
        **confirmed_orientation,
        "orientation_confirmed_by": teacher_id,
        "orientation_confirmed_at": confirmed_at,
        "orientation_confirmation_id": action_id,
        "orientation_history": history[-20:],
    })
    batch.replace_ocr_job_path(old_path, upload)
    batch.mark_ocr_job(upload["image_path"], "running")
    batch.papers = [
        item for item in batch.papers
        if str(item.get("paper_id") or "") != paper_id
    ]
    # Followers must not keep pointing at the page being re-recognized.
    _refresh_identity_inheritance(batch)
    batch.confirmation_questions = []
    batch.status = "orientation_retrying"
    batch.error = None
    return event, confirmed_orientation, history


async def _recognize_reoriented_upload(
    batch: QuickBatch,
    upload: dict,
    paper_cache: PaperCache,
    confirmed_orientation: dict,
    *,
    teacher_id: int,
    paper_id: str,
    action_id: str,
    roster_names: list[str],
) -> dict:
    """OCR the rotated page under its own vision phase; a failure becomes a result."""
    vision_phase_token = set_vision_phase_key(
        f"orientation-retry:{paper_id}:{action_id}"
    )
    try:
        try:
            result = await _recognize_batch_upload(
                batch,
                upload["image_path"],
                paper_cache,
                teacher_id=str(teacher_id),
                paper_id=paper_id,
                roster_names=roster_names,
            )
            result["orientation"] = confirmed_orientation
        except Exception as exc:
            logger.error("方向确认后单卷 OCR 异常 %s: %s", upload.get("filename"), exc)
            result = ocr_failure_result(
                exc,
                message="方向已确认，但试卷识别失败",
            )
            result["orientation"] = confirmed_orientation
    finally:
        reset_vision_phase_key(vision_phase_token)
    return result


def _finish_orientation_retry(
    batch: QuickBatch,
    upload: dict,
    event: dict,
    history: list[dict],
    result: dict,
    template_session: TemplateSession,
    db: Session,
    *,
    teacher_id: int,
    paper_id: str,
) -> None:
    """Record the retried OCR in the ledger, settle the batch, complete the event.

    Unlike ``_apply_ocr_outcome`` an accelerator pause is ingested like any
    other result here; kept as it was.
    """
    _failed, can_retry = mark_ocr_outcome(
        batch, upload["image_path"], result,
    )
    failure_questions = _single_page_template_failure_questions(template_session)
    error_payload = _ingest_recognized_paper(
        batch,
        db,
        teacher_id,
        str(upload.get("filename") or "unknown"),
        upload["image_path"],
        result,
        failure_questions=(None if can_retry else failure_questions),
    )
    if (
        error_payload
        and error_payload.get("requires_manual_review")
        and orientation_is_ready(error_payload)
    ):
        batch.mark_ocr_job(
            upload["image_path"],
            "needs_review",
            str(error_payload.get("error") or ""),
        )

    upload_order = {
        str(item.get("paper_id") or ""): index
        for index, item in enumerate(batch.uploads)
    }
    batch.papers.sort(
        key=lambda item: upload_order.get(str(item.get("paper_id") or ""), len(upload_order))
    )
    _initialize_batch_answer_map(batch, teacher_id, template_session)
    summary = batch_ocr_summary(batch)
    all_orientations_ready = all(
        orientation_is_ready(item) for item in batch.uploads
    )
    if summary["can_grade"] and all_orientations_ready:
        batch.confirmation_questions = _confirmation_questions(batch)
        batch.status = "ocr_done"
    else:
        batch.status = "ocr_partial_failed"

    event["status"] = "complete"
    event["ocr_status"] = next(
        (
            item.get("status") for item in batch.ocr_jobs
            if str(item.get("paper_id") or "") == paper_id
        ),
        "failed",
    )
    event["completed_at"] = datetime.now(timezone.utc).isoformat()
    upload["orientation_history"] = history[-20:]


@router.get("/batch-grade/{batch_id}/paper/{paper_id}/image")
def batch_grade_paper_image(
    batch_id: str,
    paper_id: str,
    variant: Literal["active", "original", "review"] = Query("active"),
    user: User = Depends(_require_user),
):
    """Serve an owned uploaded paper during identity confirmation or result review."""
    batch = QuickBatch.load_meta(batch_id, UPLOAD_DIR)
    _require_owned_batch(batch, user)
    if variant == "review":
        output_path = _batch_review_image_path(batch, paper_id, user)
        return FileResponse(str(output_path), media_type="image/png", headers={"Cache-Control": "private, no-store"})
    _paper, display_path = _batch_paper_display_path(batch, paper_id, variant)
    return FileResponse(display_path, headers={"Cache-Control": "private, no-store"})


def _batch_paper_display_path(batch: QuickBatch, paper_id: str, variant: str = "active"):
    """Resolve a paper only inside the already authorized batch."""
    if batch.asset_lifecycle.get("status") == "expired":
        raise HTTPException(410, detail=_RAW_PAPER_EXPIRED_MESSAGE)
    paper = next(
        (item for item in batch.papers if str(item.get("paper_id") or "") == paper_id),
        None,
    )
    if paper is None:
        paper = next(
            (item for item in batch.uploads if str(item.get("paper_id") or "") == paper_id),
            None,
        )
    if paper is None:
        raise HTTPException(404, detail="试卷不存在或已被清理")
    image_key = "original_image_path" if variant == "original" else "image_path"
    image_path = os.path.realpath(str(paper.get(image_key) or paper.get("image_path") or ""))
    batch_root = os.path.realpath(batch.batch_dir)
    if not image_path.startswith(batch_root + os.sep) or not os.path.isfile(image_path):
        raise HTTPException(404, detail="试卷不存在或已被清理")
    display_path = os.path.realpath(get_display_image_path(image_path))
    if not display_path.startswith(batch_root + os.sep) or not os.path.isfile(display_path):
        display_path = image_path
    return paper, display_path


def _batch_review_image_path(batch: QuickBatch, paper_id: str, user: User, decisions=None) -> Path:
    """Use one renderer for preview, single download and batch export."""
    paper, display_path = _batch_paper_display_path(batch, paper_id)
    review = _find_review_result(batch, paper_id)
    if review.get("disposition") == "voided":
        raise HTTPException(409, detail="该试卷已作废，不能导出批阅痕迹")
    details = copy.deepcopy([
        item for item in (review.get("result") or {}).get("details") or []
        if isinstance(item, dict)
    ])
    number_of = lambda item: str(item.get("question_no") or item.get("question_number") or "")
    by_number = {number_of(item): item for item in details}
    for decision in decisions or []:
        if decision.question_no not in by_number:
            raise HTTPException(400, detail="预览判定包含不属于本页的题号")
        detail = by_number[decision.question_no]
        previous_result = str(detail.get("result") or "needs_review")
        detail["result"] = decision.result
        # Folded fill details retain one verdict per physical answer box.  A
        # teacher decision targets the parent question, so a changed parent
        # verdict must update every nested mark used by the renderer as well.
        if previous_result != decision.result and isinstance(detail.get("grading_units"), list):
            for unit in detail["grading_units"]:
                if isinstance(unit, dict):
                    unit["result"] = decision.result

    # Older saved results can lack boxes even though OCR/template geometry exists.
    ocr_by_number = {number_of(item): item for item in paper.get("questions") or []}
    for detail in details:
        if detail_review_boxes(detail):
            continue
        number = number_of(detail)
        for source in (ocr_by_number.get(number) or {}, batch.answer_map.get(number) or {}):
            boxes = detail_review_boxes(source)
            if boxes:
                detail["answer_bboxes"] = boxes
                detail["answer_box_source"] = source.get("answer_box_source") or "student"
                break

    if batch.template_id and any(not detail_review_boxes(item) or item.get("answer_box_source") == "template" for item in details):
        template = _load_owned_template_session(batch.template_id, user.id)
        if template.teacher_id != user.id:
            raise HTTPException(403, detail="无权访问该模板")
        artifacts = template.page_artifacts or {}
        artifact = artifacts.get(str(paper.get("ocr_page_type") or ""))
        if not artifact:
            numbers = set(by_number)
            matches = [candidate for candidate in artifacts.values() if isinstance(candidate, dict)
                       and numbers.intersection(str(item.get("question_number") or item.get("question_no") or "")
                                                for item in candidate.get("question_schema") or [])]
            # Never use the first template page for every student's page.
            artifact = matches[0] if len(matches) == 1 else (next(iter(artifacts.values())) if len(artifacts) == 1 else None)
        if isinstance(artifact, dict):
            regions = {}
            for entry in artifact.get("template_answer_regions") or artifact.get("template_handwriting_regions") or []:
                if isinstance(entry, dict):
                    regions[number_of(entry)] = normalize_review_boxes(entry.get("regions"))
            for entry in artifact.get("question_schema") or []:
                if entry.get("drawing_regions"):
                    regions[number_of(entry)] = normalize_review_boxes(entry["drawing_regions"])
            for detail in details:
                if not detail_review_boxes(detail) and regions.get(number_of(detail)):
                    detail["answer_bboxes"] = regions[number_of(detail)]
                    detail["answer_box_source"] = "template"
            if any(item.get("answer_box_source") == "template" for item in details):
                template_root = os.path.realpath(template.template_dir)
                template_path = os.path.realpath(os.path.join(template_root, str(artifact.get("template_image") or "")))
                if not template_path.startswith(template_root + os.sep) or not os.path.isfile(template_path):
                    raise HTTPException(409, detail="模板定位底图已不可用，无法生成批阅痕迹")
                details = align_template_review_details(details, template_path, display_path)
        elif any(item.get("answer_box_source") == "template" for item in details):
            raise HTTPException(409, detail="无法确定本页对应的模板，无法生成批阅痕迹")

    output_dir = Path(batch.batch_dir) / "review_evidence"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / review_cache_name(display_path, details)
    if not output_path.is_file():
        temporary = output_dir / f".{uuid.uuid4().hex}.png"
        try:
            render_review_marks(display_path, details, temporary)
            os.replace(temporary, output_path)
        finally:
            temporary.unlink(missing_ok=True)
    return output_path


class _IdentityResolution(BaseModel):
    paper_id: str
    action: Literal["existing", "create"]
    student_id: int | None = None
    confirmed_name: str = ""
    confirmed_student_no: str = ""


class ConfirmReq(BaseModel):
    # Answers come from the batch's frozen template. Clients may still send
    # ``confirmed_answers``; like any unknown field it is ignored.
    batch_id: str
    mode: str = "precise"
    category: str = "daily"
    exam_name: str = ""
    identity_resolutions: list[_IdentityResolution] = []


def _authoritative_answers_for_confirmation(
    batch: QuickBatch,
    category: str,
) -> list[AnswerSpec]:
    """Build the grading answers from the batch's frozen template snapshot."""
    if batch.source_mode != "template":
        raise HTTPException(409, detail="该批次没有冻结模板，不能批阅；请重新上传试卷")
    answer_map = batch.answer_map if isinstance(batch.answer_map, dict) else {}
    answers: list[AnswerSpec] = []
    seen: set[str] = set()
    try:
        for question in batch.questions:
            question_no = str(question.get("question_no") or "").strip()
            entry = answer_map.get(question_no)
            if not question_no or not isinstance(entry, dict):
                raise ValueError("冻结模板中存在空题号或缺失题目")
            if question_no in seen:
                raise ValueError("冻结模板中存在重复题号")
            seen.add(question_no)
            answers.append(finalize_answer_spec_for_category({
                **entry,
                "question_no": question_no,
            }, category))
    except ValueError as exc:
        raise HTTPException(409, detail=f"冻结模板答案合同无效：{exc}") from exc
    if not answers:
        raise HTTPException(409, detail="冻结模板没有可批阅题目")
    return answers


def _require_owned_batch(batch: QuickBatch, user: User) -> None:
    """Reject legacy/unowned batches instead of guessing ownership or class."""
    if batch.teacher_id is None or batch.teacher_id != user.id:
        raise HTTPException(404, detail="批次不存在或不属于当前老师")
    if batch.identity_mode != "anonymous" and batch.class_id is None:
        raise HTTPException(400, detail="批次未冻结班级，请重新上传")


def _prepare_anonymous_identity_plan(batch: QuickBatch) -> dict[str, dict]:
    """Keep every uploaded page independent and outside student records."""
    plan: dict[str, dict] = {}
    upload_order = {
        str(item.get("paper_id") or ""): index + 1
        for index, item in enumerate(batch.uploads)
        if item.get("paper_id")
    }
    for index, paper in enumerate(batch.papers):
        paper_id = str(paper.get("paper_id") or f"paper-{index + 1}")
        paper["paper_id"] = paper_id
        label = f"匿名页 {upload_order.get(paper_id, index + 1)}"
        paper["student_name"] = label
        paper["student_no"] = ""
        paper["identity"] = {
            "status": "anonymous",
            "student_id": None,
            "reason": "anonymous_batch",
        }
        plan[paper_id] = {
            "action": "anonymous",
            "student_id": None,
            "name": label,
            "student_no": "",
        }
    return plan


def _prepare_identity_plan(
    db: Session,
    batch: QuickBatch,
    resolutions: list[_IdentityResolution],
    teacher_id: int,
    frozen_fallback: dict[str, dict] | None = None,
) -> dict[str, dict]:
    """Validate every page identity before any grading call or database write."""
    by_paper = {item.paper_id: item for item in resolutions}
    if len(by_paper) != len(resolutions):
        raise HTTPException(400, detail="同一张试卷不能提交多条学生身份确认")
    _refresh_identity_inheritance(batch)
    plan: dict[str, dict] = {}
    planned_creates: dict[str, str] = {}
    valid_paper_ids: set[str] = set()

    for index, paper in enumerate(batch.papers):
        paper_id = str(paper.get("paper_id") or f"paper-{index + 1}")
        paper["paper_id"] = paper_id
        valid_paper_ids.add(paper_id)
        identity = paper.get("identity") or {}
        if identity.get("status") == "matched" and identity.get("student_id"):
            student = db.query(Student).filter(
                Student.id == int(identity["student_id"]),
                Student.teacher_id == teacher_id,
                Student.class_id == batch.class_id,
            ).first()
            if student is None:
                raise HTTPException(400, detail=f"{paper.get('filename', paper_id)} 的自动匹配学生已失效，请重新确认身份")
            plan[paper_id] = {
                "action": "existing",
                "student_id": student.id,
                "name": student.name,
                "student_no": student.student_no or "",
            }
            continue

        resolution = by_paper.get(paper_id)
        frozen = (frozen_fallback or {}).get(paper_id) or {}
        if resolution is None and frozen:
            action = str(frozen.get("action") or "")
            if action == "existing" and int(frozen.get("student_id") or 0) > 0:
                plan[paper_id] = {
                    "action": "existing",
                    "student_id": int(frozen["student_id"]),
                    "name": str(frozen.get("name") or ""),
                    "student_no": str(frozen.get("student_no") or ""),
                }
                continue
            if action == "create":
                frozen_name = str(frozen.get("name") or "").strip()
                frozen_no = canonical_student_no(frozen.get("student_no"))
                if frozen_name:
                    plan[paper_id] = {
                        "action": "create",
                        "student_id": None,
                        "name": frozen_name,
                        "student_no": frozen_no,
                    }
                    if frozen_no:
                        planned_creates[frozen_no] = frozen_name
                    continue
        if resolution is None:
            raise HTTPException(400, detail=f"{paper.get('filename', paper_id)} 的学生身份尚未确认")

        if resolution.action == "existing":
            if not resolution.student_id:
                raise HTTPException(400, detail=f"{paper.get('filename', paper_id)} 必须选择已有学生")
            student = db.query(Student).filter(
                Student.id == resolution.student_id,
                Student.teacher_id == teacher_id,
                Student.class_id == batch.class_id,
            ).first()
            if student is None:
                raise HTTPException(400, detail=f"{paper.get('filename', paper_id)} 选择的学生不在本批次班级")
            plan[paper_id] = {
                "action": "existing",
                "student_id": student.id,
                "name": student.name,
                "student_no": student.student_no or "",
            }
            continue

        name = resolution.confirmed_name.strip()
        student_no = canonical_student_no(resolution.confirmed_student_no)
        if not name:
            raise HTTPException(400, detail=f"{paper.get('filename', paper_id)} 新建学生必须确认姓名")
        if student_no:
            duplicate = db.query(Student).filter(
                Student.teacher_id == teacher_id,
                Student.student_no == student_no,
            ).first()
            if duplicate is not None:
                raise HTTPException(
                    400,
                    detail=f"学号 {student_no} 已属于 {duplicate.name}，请选择已有学生而不是新建",
                )
            prior_name = planned_creates.get(student_no)
            if prior_name is not None and prior_name != name:
                raise HTTPException(400, detail=f"同一学号 {student_no} 被确认成了两个不同姓名")
            planned_creates[student_no] = name
        plan[paper_id] = {
            "action": "create",
            "student_id": None,
            "name": name,
            "student_no": student_no,
        }

    unknown_papers = set(by_paper) - valid_paper_ids
    if unknown_papers:
        raise HTTPException(400, detail="学生身份确认中包含不属于本批次的试卷")
    return plan


def _stable_review_image_hash(batch_id: str, paper_id: str, image_path: str) -> str:
    """Return a non-empty idempotency key even if the uploaded image was cleaned up.

    The real image SHA-256 is preferred.  A deterministic batch+paper fallback
    prevents a committed archive from duplicating formal rows when meta.json
    was not saved before a process crash.
    """
    actual = image_content_hash(image_path)
    if actual:
        return actual
    material = f"quick-batch-review:{batch_id}:{paper_id}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


_GRADING_CONTRACT_VERSION = 1


def _grading_question_sort_key(value: str) -> tuple:
    return tuple(
        (0, int(token)) if token.isdigit() else (1, token.casefold())
        for token in re.split(r"(\d+)", str(value or ""))
        if token
    )


def _frozen_answer_payload(item: AnswerSpec) -> dict:
    """Keep only normalized teacher inputs that affect grading or scoring."""
    return {
        **item.directory_metadata(),
        **({"drawing_reference": copy.deepcopy(item.drawing_reference)} if item.drawing_reference is not None else {}),
        "question_no": item.question_no,
        "question_text": item.question_text,
        "question_type": item.question_type,
        "choice_mode": item.choice_mode,
        "standard_answer": item.standard_answer,
        "answer_variants": sorted(item.answer_variants),
        "solution_steps": list(item.solution_steps),
        "max_score": item.max_score,
        "blank_count": item.blank_count,
        "answer_slots": list(item.answer_slots),
        "fill_scoring_mode": item.fill_scoring_mode,
        "blank_scores": list(item.blank_scores),
        "subquestion_count": item.subquestion_count,
        "subquestion_scores": list(item.subquestion_scores),
        "grading_method": item.grading_method,
        "chapter": item.chapter,
        "topic": item.topic,
    }


def _build_grading_contract(
    *,
    mode: str,
    category: str,
    exam_name: str,
    confirmed: list[AnswerSpec],
    identity_plan: dict[str, dict],
    teaching_context: dict | None = None,
) -> dict:
    confirmed_payload = sorted(
        (_frozen_answer_payload(item) for item in confirmed),
        key=lambda item: _grading_question_sort_key(item["question_no"]),
    )
    frozen_identities = {
        paper_id: {
            "action": str(identity_plan[paper_id].get("action") or ""),
            "student_id": identity_plan[paper_id].get("student_id"),
            "name": str(identity_plan[paper_id].get("name") or ""),
            "student_no": str(identity_plan[paper_id].get("student_no") or ""),
        }
        for paper_id in sorted(identity_plan)
    }
    snapshot = validate_teaching_context(teaching_context)
    return {
        **({"teaching_context": snapshot} if snapshot else {}),
        "version": _GRADING_CONTRACT_VERSION,
        "mode": mode,
        "category": category,
        "exam_name": str(exam_name or "").strip(),
        "confirmed": confirmed_payload,
        "identity_plan": frozen_identities,
    }


def _grading_contract_hash(contract: dict) -> str:
    """Hash semantic grading inputs while ignoring mutable roster labels."""
    identities = []
    for paper_id in sorted(contract.get("identity_plan") or {}):
        assignment = contract["identity_plan"][paper_id]
        identity = {
            "paper_id": paper_id,
            "action": str(assignment.get("action") or ""),
        }
        if identity["action"] == "existing":
            identity["student_id"] = int(assignment.get("student_id") or 0)
        else:
            identity["name"] = str(assignment.get("name") or "").strip()
            identity["student_no"] = str(assignment.get("student_no") or "").strip()
        identities.append(identity)
    payload = {
        "version": int(contract.get("version") or _GRADING_CONTRACT_VERSION),
        "mode": str(contract.get("mode") or ""),
        "category": str(contract.get("category") or ""),
        "exam_name": str(contract.get("exam_name") or "").strip(),
        "confirmed": sorted(
            list(contract.get("confirmed") or []),
            key=lambda item: _grading_question_sort_key(item.get("question_no", "")),
        ),
        "identities": identities,
    }
    if contract.get("teaching_context"):
        payload["teaching_context"] = validate_teaching_context(contract["teaching_context"])
    encoded = _json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# ── 版本戳与逐卷流水批阅（2026-09-27）────────────────────────────────
# 幂等身份 = paper_id + rubric_hash：同一输入只一份有效结果；旧模板/旧
# rubric 的任务不能覆盖新结果。批阅只有一条流程（_grade_paper_core，与
# /batch-grade/confirm 共用），流水线只决定"何时"让每张卷进入该流程——
# 识别定稿一张就地批一张，不再等全量识别。

_RUBRIC_METADATA_KEYS = {
    "chapter", "topic", "question_bank_id", "difficulty", "matched", "source",
    # Derived from chapter/topic by the background knowledge classifier. It
    # arrives asynchronously, so a resume must not treat it as a new rubric
    # and regrade every paper the pipeline already finished.
    "knowledge_point_id",
}


def _batch_rubric_hash(batch: QuickBatch) -> str:
    """Scoring-input identity: only grading-relevant fields change the hash.

    Roster/metadata keys (chapter/topic/difficulty/source/...) are excluded so
    a background knowledge enrichment cannot masquerade as a rubric change.
    """
    questions = []
    for question_no in sorted(batch.answer_map or {}):
        entry = (batch.answer_map or {}).get(question_no) or {}
        questions.append((
            str(question_no),
            {
                str(key): value
                for key, value in entry.items()
                if key not in _RUBRIC_METADATA_KEYS
            },
        ))
    material = _json.dumps({
        "mode": str(batch.grade_mode or ""),
        "category": str(batch.category or ""),
        "exam_name": str(batch.exam_name or ""),
        "questions": questions,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _pin_batch_rubric(
    batch: QuickBatch,
    template_session: Optional["TemplateSession"] = None,
) -> dict:
    """Pin the rubric/template version stamps for one recognition run.

    Re-deriving the pins on every (re)recognition run is intentional: when the
    rubric actually changed (e.g. the teacher fixed the template and re-ran the
    batch), previously checkpointed rows keep their old stamps and are refused
    at write-back — stale work never overwrites reworked results.
    """
    batch.grading_rubric_hash = _batch_rubric_hash(batch)
    batch.template_contract_hash = str(
        getattr(template_session, "confirmed_contract_hash", "") or ""
    )
    return {
        "grading_rubric_hash": batch.grading_rubric_hash,
        "template_contract_hash": batch.template_contract_hash,
    }


def _row_matches_rubric_pin(batch: QuickBatch, row: dict) -> bool:
    """A result is current iff it carries no stamp (legacy) or matches the pin."""
    if not isinstance(row, dict):
        return False
    for field in ("grading_rubric_hash", "template_contract_hash"):
        stamp = str(row.get(field) or "")
        if stamp and stamp != str(getattr(batch, field, "") or ""):
            return False
    return True


def _upsert_batch_paper_result(batch: QuickBatch, review: dict, row: dict) -> None:
    """Checkpoint one paper's result set; one effective result per paper_id."""
    keys = {
        str(row.get("paper_id") or row.get("review_id") or ""),
        str(review.get("paper_id") or review.get("review_id") or ""),
    }
    keys.discard("")
    reviews = [
        item for item in batch.review_results
        if str(item.get("paper_id") or item.get("review_id") or "") not in keys
    ]
    rows = [
        item for item in batch.results
        if str(item.get("paper_id") or item.get("review_id") or "") not in keys
    ]
    reviews.append(review)
    rows.append(row)
    order = {
        str(paper.get("paper_id") or ""): index
        for index, paper in enumerate(batch.papers)
    }

    def _sort_key(item: dict) -> tuple:
        paper_id = str(item.get("paper_id") or item.get("review_id") or "")
        return (order.get(paper_id, len(order)), paper_id)

    batch.review_results = sorted(reviews, key=_sort_key)
    batch.results = sorted(rows, key=_sort_key)


def _build_template_confirmed_map(
    batch: QuickBatch,
    confirmed_for_grading: list[AnswerSpec],
) -> dict:
    """Rebuild the frozen grading map exactly as /batch-grade/confirm does.

    Geometry lives in the template artifact rather than in AnswerSpec; keep it
    when rebuilding so scoring does not lose answer boxes at confirmation.
    """
    old_map = batch.answer_map or {}
    confirmed_map: dict = {}
    for answer in confirmed_for_grading:
        old_entry = old_map.get(answer.question_no, {}) or {}
        confirmed_entry = answer.model_copy(update={
            "matched": True,
            "question_bank_id": old_entry.get("question_bank_id"),
            "difficulty": old_entry.get("difficulty") or 3,
            "source": "template_confirmed",
        }).to_answer_map_entry(source="template_confirmed")
        for key in ("answer_bboxes", "answer_box_source"):
            if key in old_entry:
                confirmed_entry[key] = copy.deepcopy(old_entry[key])
        confirmed_map[answer.question_no] = confirmed_entry
    return confirmed_map


def _pipeline_grading_answer_map(batch: QuickBatch) -> dict:
    """The confirmed-map snapshot every pipeline-graded paper is scored against."""
    confirmed = _authoritative_answers_for_confirmation(batch, batch.category or "daily")
    contract = _build_grading_contract(
        mode=str(batch.grade_mode or "precise"),
        category=str(batch.category or "daily"),
        exam_name=str(batch.exam_name or ""),
        confirmed=confirmed,
        identity_plan={},
        teaching_context=batch.teaching_context,
    )
    return _build_template_confirmed_map(batch, _contract_answer_specs(contract))


def _pipeline_identity_assignment(
    db: Session,
    batch: QuickBatch,
    paper: dict,
    teacher_id: int,
) -> dict | None:
    """Per-paper identity for pipeline grading; None = wait for the teacher.

    Mirrors _prepare_identity_plan's per-paper branches.  Anything not
    verifiable here stays unassigned: uncertain OCR identity is never silently
    attributed to a student (fail-closed).
    """
    if batch.identity_mode == "anonymous":
        label, _identity = _anonymous_page_identity(
            batch, str(paper.get("image_path") or ""),
        )
        return {"action": "anonymous", "student_id": None, "name": label, "student_no": ""}
    paper_id = str(paper.get("paper_id") or "")
    identity = paper.get("identity") or {}
    if identity.get("status") == "matched" and identity.get("student_id"):
        student = db.query(Student).filter(
            Student.id == int(identity["student_id"]),
            Student.teacher_id == teacher_id,
            Student.class_id == batch.class_id,
        ).first()
        if student is None:
            return None
        return {
            "action": "existing",
            "student_id": student.id,
            "name": student.name,
            "student_no": student.student_no or "",
        }
    frozen = ((batch.grading_contract or {}).get("identity_plan") or {}).get(paper_id) or {}
    action = str(frozen.get("action") or "")
    if action == "existing" and int(frozen.get("student_id") or 0) > 0:
        return {
            "action": "existing",
            "student_id": int(frozen["student_id"]),
            "name": str(frozen.get("name") or ""),
            "student_no": str(frozen.get("student_no") or ""),
        }
    frozen_name = str(frozen.get("name") or "").strip()
    if action == "create" and frozen_name:
        return {
            "action": "create",
            "student_id": None,
            "name": frozen_name,
            "student_no": canonical_student_no(frozen.get("student_no")),
        }
    return None


async def _grade_paper_core(
    batch: QuickBatch,
    paper: dict,
    paper_id: str,
    assignment: dict,
    teacher_id: int,
    *,
    detail_level: str,
    grading_category: str,
    grading_exam_name: str,
    grading_mode: str,
    answer_map: dict | None = None,
    stamp: dict | None = None,
) -> tuple[str, dict | None, dict]:
    """Grade one paper under the batch's frozen rubric — the single grading path.

    Used by both /batch-grade/confirm and the per-paper recognition pipeline;
    there is only one grading flow, the pipeline merely decides when each paper
    enters it.
    """
    sname = assignment["name"]
    sno = assignment["student_no"]
    grading_map = batch.answer_map if answer_map is None else answer_map
    paper_for_grading = _mark_unverified_answer_alignment(paper)
    paper_for_grading = _reject_answer_key_matching_local_recoveries(
        paper_for_grading,
        grading_map,
    )
    try:
        result = await await_teaching(batch.teaching_context, grade_paper_unified(
            paper=paper_for_grading, answer_map=grading_map,
            student_name=sname, teacher_id=teacher_id,
            detail_level=detail_level, category=grading_category,
        ))
    except GradingModelUnavailable as exc:
        # The grading queue already made one complete retry. Do not
        # cancel unrelated papers or turn every question into a
        # teacher review item. Omitting this paper from the durable
        # result set makes the existing resume action pick it up.
        info = report_failure(exc, stage="grading", task_id=batch.batch_id, operation_id=paper_id)
        paper["grading_error_info"] = info
        return sname, None, {
            "paper_id": paper_id,
            "_grading_pending": True,
            "error_info": info,
        }

    # 身份已冻结；此阶段只保存暂存 assignment，正式档案由 archive-all 创建。
    paper.pop("grading_error_info", None)
    knowledge_items = []
    for d in result["details"]:
        if batch.teaching_context:
            d["teaching_context"] = copy.deepcopy(batch.teaching_context)
        source_question_no = str(
            d.get("parent_question_no") or d.get("question_no", d.get("question_number", ""))
        )
        entry = grading_map.get(source_question_no, {}) or {}
        knowledge_items.append({
            "question_no": source_question_no,
            "detail_question_no": str(d.get("question_no") or d.get("question_number") or ""),
            "question_bank_id": entry.get("question_bank_id"),
            "chapter": entry.get("chapter") or "未知",
            "topic": entry.get("topic") or entry.get("chapter") or "未知",
            "difficulty": entry.get("difficulty") or 3,
        })

    review = {
        "teaching_context": copy.deepcopy(batch.teaching_context),
        "review_id": paper_id,
        "paper_id": paper_id,
        "filename": paper.get("filename", ""),
        "student_name": sname,
        "student_no": sno,
        "identity_assignment": assignment,
        "image_path": paper.get("image_path", ""),
        "image_content_hash": _stable_review_image_hash(
            batch.batch_id, paper_id, str(paper.get("image_path") or "")
        ),
        "ocr_raw_name": (paper.get("student_name") or "").strip(),
        "ocr_raw_json": paper_for_grading,
        "category": grading_category,
        "exam_name": grading_exam_name,
        "grading_mode": grading_mode,
        "result": result,
        "knowledge_items": knowledge_items,
        "disposition": "active",
        "manual_corrections": [],
        "voided_at": "",
        "void_reason": "",
        "archived_grading_result_id": None,
        "archived_profile_id": None,
    }
    row = {
        "teaching_context": copy.deepcopy(batch.teaching_context),
        "review_id": paper_id,
        "student_name": sname,
        "grading_result_id": None,
        "profile_id": None,
        "score": result["score"],
        "max_score": result.get("max_score"),
        "correct_rate": result.get("correct_rate"),
        "category": grading_category,
        "exam_name": grading_exam_name,
        "correct": result["correct_count"],
        "partial": result["partial_count"],
        "wrong": result["wrong_count"],
        "needs_review": result["needs_review_count"],
        "total": result["total"],
        "details": result["details"],
        "grading_mode": grading_mode,
        "idempotent": False,
        "needs_name_confirm": False,
        "paper_id": paper_id,
        "disposition": "active",
    }
    if stamp:
        review.update(stamp)
        row.update(stamp)
    return sname, review, row


class _PerPaperGradingPipeline:
    """Grade each paper as soon as its OCR outcome is final.

    Runs inside the recognition stream: dispatch is per ingested paper (single
    paper ordering — recognition finalizes first, then that paper is graded),
    cross-paper grading runs in parallel.  Every result is stamped with the
    batch's rubric/template pins and refused at write-back when the stamp no
    longer matches, so stale tasks can never overwrite reworked results.
    """

    def __init__(
        self,
        batch: QuickBatch,
        db_gen: Session,
        teacher_id: int,
        *,
        answer_map: dict,
    ) -> None:
        self.batch = batch
        self.db_gen = db_gen
        self.teacher_id = teacher_id
        self.answer_map = answer_map
        self.stamp = {
            "grading_rubric_hash": batch.grading_rubric_hash,
            "template_contract_hash": batch.template_contract_hash,
        }
        self.queue: asyncio.Queue = asyncio.Queue()
        self.tasks: set[asyncio.Task] = set()
        self.dispatched: set[str] = set()
        self.done_count = 0
        self.detail_level = (
            "simple" if str(batch.grade_mode or "") in ("auto", "simple") else "detailed"
        )

    @property
    def total_count(self) -> int:
        return len(self.batch.papers)

    def _already_graded(self, paper_id: str) -> bool:
        return any(
            str(row.get("paper_id") or row.get("review_id") or "") == paper_id
            and _row_matches_rubric_pin(self.batch, row)
            for row in self.batch.results
        )

    def dispatch(self, paper: dict) -> bool:
        batch = self.batch
        paper_id = str(paper.get("paper_id") or "")
        if not paper_id or paper_id in self.dispatched or self._already_graded(paper_id):
            return False
        if not orientation_is_ready(paper):
            return False
        assignment = _pipeline_identity_assignment(
            self.db_gen, batch, paper, self.teacher_id,
        )
        if assignment is None:
            return False
        self.dispatched.add(paper_id)
        _mark_grading_started(batch)
        task = asyncio.create_task(
            self._grade_and_checkpoint(paper, paper_id, assignment)
        )
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return True

    def dispatch_inherited(self) -> None:
        """Pages that inherit a matched identity become ready with their anchor."""
        for paper in self.batch.papers:
            if (paper.get("identity") or {}).get("inherited_from"):
                self.dispatch(paper)

    def dispatch_upload(self, upload: dict) -> None:
        """Grade the paper just ingested from ``upload``, then its inheritors."""
        image_path = os.path.realpath(str(upload.get("image_path") or ""))
        ingested = next(
            (
                paper for paper in self.batch.papers
                if os.path.realpath(str(paper.get("image_path") or "")) == image_path
            ),
            None,
        )
        if ingested is not None:
            self.dispatch(ingested)
            self.dispatch_inherited()

    async def drain(self):
        """Yield ``("graded", item)`` until every dispatched paper is graded.

        Yields ``("heartbeat", None)`` when no result arrived within one
        heartbeat interval. Nothing is pending between yields.
        """
        while True:
            self.tasks = {task for task in self.tasks if not task.done()}
            if not self.tasks and self.queue.empty():
                return
            get_task = asyncio.create_task(self.queue.get())
            try:
                graded = await asyncio.wait_for(
                    get_task, timeout=_GRADE_HEARTBEAT_SECONDS,
                )
            except asyncio.TimeoutError:
                yield "heartbeat", None
                continue
            yield "graded", graded

    async def _grade_and_checkpoint(
        self,
        paper: dict,
        paper_id: str,
        assignment: dict,
    ) -> None:
        batch = self.batch
        try:
            sname, review, row = await _grade_paper_core(
                batch, paper, paper_id, assignment, self.teacher_id,
                detail_level=self.detail_level,
                grading_category=str(batch.category or "daily"),
                grading_exam_name=str(batch.exam_name or ""),
                grading_mode=str(batch.grade_mode or "precise"),
                answer_map=self.answer_map,
                stamp=self.stamp,
            )
        except Exception as exc:
            # A pipeline grade must never take recognition down. Leave the
            # paper pending: the existing confirm/resume flow retries it.
            logger.exception("逐卷流水批阅异常 paper=%s: %s", paper_id, exc)
            await self.queue.put((
                None, None,
                {"paper_id": paper_id, "_grading_pending": True, "error": str(exc)},
            ))
            return
        if review is None:
            self.done_count += 1
            await self.queue.put((sname, None, row))
            return
        if not _row_matches_rubric_pin(batch, row):
            # Stale task (rubric/template re-pinned meanwhile): never overwrite.
            await self.queue.put((sname, None, {
                "paper_id": paper_id, "_grading_pending": True, "stale": True,
            }))
            return
        _upsert_batch_paper_result(batch, review, row)
        self.done_count += 1
        try:
            await _save_quick_batch_meta(batch)
        except Exception:
            logger.exception("逐卷流水落盘失败 paper=%s", paper_id)
        await self.queue.put((sname, review, row))

    async def aclose(self) -> None:
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            async def settle_tasks():
                await asyncio.gather(*tasks, return_exceptions=True)

            # Shield the join, not the grading: all workers are cancelled once,
            # but repeated consumer cancellation must not interrupt their cleanup.
            close_task = asyncio.create_task(settle_tasks())
            await _settle_template_component(close_task, cancel_running=False)
        self.tasks.clear()


def _start_per_paper_grading(
    batch: QuickBatch,
    db: Session,
    teacher_id: int,
    template_session: TemplateSession,
) -> _PerPaperGradingPipeline:
    """Freeze the batch's answers and rubric pins, then start grading.

    The answer map comes from the frozen template and does not depend on
    recognition, so each paper can be graded as soon as its OCR is final.
    """
    _initialize_batch_answer_map(batch, teacher_id, template_session)
    _pin_batch_rubric(batch, template_session)
    return _PerPaperGradingPipeline(
        batch, db, teacher_id,
        answer_map=_pipeline_grading_answer_map(batch),
    )


def _pipeline_result_sse(pipeline: _PerPaperGradingPipeline, graded: tuple) -> str:
    sname, review, row = graded
    if review is None:
        marker = row or {}
        message = (
            f"{sname or '一张试卷'} 的批阅已按新版本作废，将按当前模板重新批阅"
            if marker.get("stale")
            else f"{sname or '一张试卷'} 的数学判分暂缓，其他试卷继续处理"
        )
        return _sse("progress", {
            "message": message,
            "current": pipeline.done_count,
            "total": pipeline.total_count,
        })
    return _sse("result", row)


def _contract_answer_specs(contract: dict) -> list[AnswerSpec]:
    return [
        AnswerSpec.model_validate(item)
        for item in (contract.get("confirmed") or [])
    ]


def _confirmation_questions_from_contract(contract: dict) -> list[dict]:
    questions = []
    for item in _contract_answer_specs(contract):
        questions.append({
            **item.directory_metadata(),
            **({"drawing_reference": copy.deepcopy(item.drawing_reference)} if item.drawing_reference is not None else {}),
            "question_no": item.question_no,
            "content": item.question_text,
            "question_type": item.question_type,
            "choice_mode": item.choice_mode,
            "answer": item.standard_answer,
            "answer_variants": list(item.answer_variants),
            "solution_steps": list(item.solution_steps),
            "max_score": item.max_score,
            "blank_count": item.blank_count,
            "answer_slots": list(item.answer_slots),
            "fill_scoring_mode": item.fill_scoring_mode,
            "blank_scores": list(item.blank_scores),
            "subquestion_count": item.subquestion_count,
            "subquestion_scores": list(item.subquestion_scores),
            "grading_method": item.grading_method,
            "chapter": item.chapter,
            "topic": item.topic,
            "matched": True,
            "source": "teacher_confirmed",
        })
    return questions


class _GradingRefused(Exception):
    """A grading request the batch's durable state cannot honour."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.payload = {"code": code, "message": message}


_REVIEW_FINISHED_STATUSES = {"review_ready", "voided", "archived"}


def _grading_contract_for_run(
    db: Session,
    batch: QuickBatch,
    req: ConfirmReq,
    teacher_id: int,
    confirmed: list[AnswerSpec],
) -> tuple[dict, str, bool]:
    """Choose the contract this run grades against.

    Returns ``(contract, request_hash, has_stored_contract)``. A batch with
    durable results keeps the contract it was frozen with: a request that
    differs from it, or a legacy batch without a recoverable contract, is
    refused rather than graded against something new.
    """
    stored_contract = (
        batch.grading_contract
        if isinstance(batch.grading_contract, dict)
        and batch.grading_contract.get("version") == _GRADING_CONTRACT_VERSION
        else {}
    )
    frozen_plan = stored_contract.get("identity_plan") or {}
    identity_plan = (
        _prepare_anonymous_identity_plan(batch)
        if batch.identity_mode == "anonymous"
        else _prepare_identity_plan(
            db,
            batch,
            req.identity_resolutions,
            teacher_id,
            frozen_fallback=frozen_plan,
        )
    )
    request_contract = _build_grading_contract(
        mode=req.mode,
        category=req.category,
        exam_name=str(req.exam_name or "").strip(),
        confirmed=confirmed,
        identity_plan=identity_plan,
        teaching_context=batch.teaching_context,
    )
    contract_hash = _grading_contract_hash(request_contract)
    has_durable_results = bool(batch.review_results or batch.results)
    if has_durable_results and batch.grading_contract_hash and not stored_contract:
        raise _GradingRefused(
            "GRADING_CONTRACT_UNRECOVERABLE",
            "旧批次缺少完整可恢复合同，不能安全续批；请新建批次",
        )
    stored_matches = bool(stored_contract) and _grading_contract_hash(stored_contract) == contract_hash
    if stored_contract and not stored_matches and has_durable_results:
        raise _GradingRefused(
            "GRADING_CONTRACT_CHANGED",
            "该批次已有部分结果，不能改用另一组答案或学生归属继续；请恢复原确认内容后重试",
        )
    contract = stored_contract if stored_matches else request_contract
    return contract, contract_hash, bool(stored_contract)


def _freeze_grading_contract(batch: QuickBatch, contract: dict, contract_hash: str) -> None:
    batch.grading_contract = contract
    batch.grading_contract_hash = contract_hash
    batch.confirmation_questions = _confirmation_questions_from_contract(contract)
    batch.grade_mode = str(contract["mode"])
    batch.category = str(contract["category"])
    batch.exam_name = str(contract.get("exam_name") or "")


def _start_grading_run(batch: QuickBatch, confirmed: list[AnswerSpec]) -> dict:
    """Bind the batch to its contract's answers and mark it grading.

    Returns the rubric stamp that every graded row carries.
    """
    # The frozen template is authoritative; grade only against the
    # confirmed contract.
    confirmed_map = _build_template_confirmed_map(batch, confirmed)
    if len(confirmed_map) < len(batch.questions):
        logger.warning(
            "batch %s: 确认题数 %d < 识别题数 %d，未确认题将不带预填答案批改",
            batch.batch_id, len(confirmed_map), len(batch.questions),
        )
    # 红线兜底：只含确认题的 map 替换原 map，彻底切断未确认答案进入判分的路径
    batch.answer_map = confirmed_map
    if not batch.grading_rubric_hash:
        _pin_batch_rubric(batch)
    batch.status = "grading"
    batch.error = None
    _mark_grading_started(batch)
    return {
        "grading_rubric_hash": batch.grading_rubric_hash,
        "template_contract_hash": batch.template_contract_hash,
    }


@dataclass
class _GradingProgress:
    """Which papers of a batch already have a checkpointed grade."""

    paper_ids: list[str]
    review_by_id: dict[str, dict]
    row_by_id: dict[str, dict]
    completed_ids: set[str]
    pending_items: list[tuple[int, dict]]
    # Papers whose grading is postponed to a later run.
    deferred_ids: set[str]

    @classmethod
    def of(cls, batch: QuickBatch) -> "_GradingProgress":
        """Papers whose review and row both match the batch's rubric pin are done."""
        paper_ids = [
            str(paper.get("paper_id") or f"paper-{index + 1}")
            for index, paper in enumerate(batch.papers)
        ]
        review_by_id = {
            str(item.get("paper_id") or item.get("review_id") or ""): item
            for item in batch.review_results
        }
        row_by_id = {
            str(item.get("paper_id") or item.get("review_id") or ""): item
            for item in batch.results
        }
        completed_ids = {
            paper_id
            for paper_id in (set(review_by_id) & set(row_by_id) & set(paper_ids))
            if _row_matches_rubric_pin(batch, row_by_id[paper_id])
            and _row_matches_rubric_pin(batch, review_by_id[paper_id])
        }
        pending_items = [
            (index, paper)
            for index, paper in enumerate(batch.papers)
            if paper_ids[index] not in completed_ids
        ]
        return cls(paper_ids, review_by_id, row_by_id, completed_ids, pending_items, set())

    def record(self, batch: QuickBatch, review: dict, row: dict) -> None:
        """Add one graded paper and mirror the progress on the batch."""
        paper_id = str(row.get("paper_id") or row.get("review_id") or "")
        self.review_by_id[paper_id] = review
        self.row_by_id[paper_id] = row
        self.completed_ids.add(paper_id)
        batch.review_results = [
            self.review_by_id[pid] for pid in self.paper_ids if pid in self.review_by_id
        ]
        batch.results = [self.row_by_id[pid] for pid in self.paper_ids if pid in self.row_by_id]
        batch.status = "grading"
        batch.error = None


async def _graded_papers(pending_items, grade_one, *, deadline_at: float):
    """Grade the pending papers, yielding each result as it completes.

    Yields ``("graded", (student_name, review, row))`` per finished paper,
    ``("heartbeat", None)`` when nothing finished within one heartbeat
    interval, and ``("deadline", None)`` once the batch deadline has passed.
    Completion order is intentional: a fast paper is checkpointed even if an
    earlier paper is slow or a concurrent model call fails. On exit the
    in-flight wait is settled and the dispatcher closed; consume this under
    ``contextlib.aclosing`` so that runs before the caller's own cleanup.
    """
    result_iter = bounded_completion_map(
        pending_items,
        grade_one,
        # Flash is the only grading model; its request lane is the valve.
        max_concurrent=FLASH_LOCAL_MAX_CONCURRENT,
    ).__aiter__()
    next_result_task = None
    try:
        while True:
            remaining = deadline_at - asyncio.get_running_loop().time()
            if remaining <= 0:
                yield "deadline", None
                return
            if next_result_task is None:
                next_result_task = asyncio.create_task(anext(result_iter))
            try:
                _position, graded = await asyncio.wait_for(
                    asyncio.shield(next_result_task),
                    timeout=min(_GRADE_HEARTBEAT_SECONDS, remaining),
                )
            except asyncio.TimeoutError:
                if asyncio.get_running_loop().time() >= deadline_at:
                    yield "deadline", None
                    return
                yield "heartbeat", None
                continue
            except StopAsyncIteration:
                next_result_task = None
                return
            next_result_task = None
            yield "graded", graded
    finally:
        if next_result_task is not None:
            if not next_result_task.done():
                next_result_task.cancel()
            await asyncio.gather(next_result_task, return_exceptions=True)
        await result_iter.aclose()


async def _stop_for_grading_deadline(batch: QuickBatch, progress: _GradingProgress) -> dict:
    """Keep the finished results and defer every paper still being graded."""
    progress.deferred_ids.update(
        str(paper.get("paper_id") or f"paper-{index + 1}")
        for index, paper in progress.pending_items
        if str(paper.get("paper_id") or f"paper-{index + 1}")
        not in progress.completed_ids
    )
    batch.status = "grading_interrupted"
    batch.error = (
        "批量数学判分达到总时限，已保存完成结果；"
        f"剩余 {len(progress.deferred_ids)} 张可重新提交继续。"
    )
    await _save_quick_batch_meta(batch)
    return {
        "code": "GRADING_BATCH_DEADLINE",
        "message": batch.error,
    }


async def _finish_grading_run(
    batch: QuickBatch,
    progress: _GradingProgress,
    teacher_id: int,
) -> None:
    """Publish the complete review, then start the batch report in the background."""
    batch.review_results = [progress.review_by_id[pid] for pid in progress.paper_ids]
    batch.results = [progress.row_by_id[pid] for pid in progress.paper_ids]
    batch.status = "review_ready"
    _refresh_batch_knowledge(batch, teacher_id)
    if not batch.review_ready_at:
        batch.review_ready_at = _review_timestamp()
    batch.grading_elapsed_seconds = _grading_elapsed_seconds(batch)
    batch.error = None
    await _save_quick_batch_meta(batch)
    logger.info(
        "批阅计时 batch=%s papers=%d graded_now=%d elapsed_s=%s",
        batch.batch_id,
        len(batch.results),
        len(progress.pending_items),
        batch.grading_elapsed_seconds,
    )
    # Publish the completed grading result first.  The report is a
    # separate, lightweight model call and must never delay the result
    # table or make a teacher wait for narrative text.
    await _start_batch_report_generation(batch, teacher_id)


def _finished_grading_replay(batch: QuickBatch) -> list[str]:
    """Re-send a finished batch's rows and its done event, marked idempotent."""
    return [
        *(_sse("result", row) for row in batch.results),
        _sse("done", {
            "total": len(batch.results),
            "avg_score": _review_avg_score(batch.results),
            "idempotent": True,
            "review_revision": batch.review_revision,
            "grading_elapsed_seconds": batch.grading_elapsed_seconds,
        }),
    ]


async def _scoring_chain_unavailable(batch: QuickBatch) -> dict | None:
    """The SSE error payload when the scoring chain cannot grade right now."""
    try:
        await _require_batch_scoring_chain(batch)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        return {
            "code": "GRADING_CHAIN_UNAVAILABLE",
            "status": str(detail.get("status") or "fault"),
            "message": str(
                detail.get("message")
                or "批改链路当前不可用，请等待恢复后再重试。"
            ),
        }
    return None


def _completed_papers_replay(progress: _GradingProgress, total: int) -> list[str]:
    """Re-send the already graded papers so a reconnecting client's counts add up."""
    completed = len([pid for pid in progress.paper_ids if pid in progress.completed_ids])
    events = []
    for paper_id in progress.paper_ids:
        if paper_id not in progress.completed_ids:
            continue
        row = {**progress.row_by_id[paper_id], "idempotent": True}
        events.append(_sse("progress", {
            "message": f"已恢复 {row.get('student_name') or '一张试卷'}",
            "current": completed,
            "total": total,
        }))
        events.append(_sse("result", row))
    return events


async def _record_graded_paper(
    batch: QuickBatch,
    progress: _GradingProgress,
    graded: tuple[str, dict | None, dict],
    total: int,
) -> list[str]:
    """Checkpoint one graded paper, or defer it when its grading is still pending."""
    student_name, review, row = graded
    if row.get("_grading_pending"):
        progress.deferred_ids.add(str(row.get("paper_id") or row.get("review_id") or ""))
        return [_sse("progress", {
            "message": f"{student_name} 的数学判分暂缓，其他试卷继续处理",
            "current": len(progress.completed_ids),
            "total": total,
        })]
    progress.record(batch, review, row)
    await _save_quick_batch_meta(batch)
    return [
        _sse("progress", {
            "message": f"已批改 {student_name}",
            "current": len(progress.completed_ids),
            "total": total,
            "grading_elapsed_seconds": _grading_elapsed_seconds(batch),
        }),
        _sse("result", row),
    ]


async def _stop_for_deferred_papers(batch: QuickBatch, progress: _GradingProgress) -> dict:
    """Keep the finished results; deferred papers wait for a resubmission."""
    failures = [
        {"paper_id": paper.get("paper_id"), "error_info": paper["grading_error_info"]}
        for paper in batch.papers
        if paper.get("paper_id") in progress.deferred_ids and paper.get("grading_error_info")
    ]
    batch.status = "grading_interrupted"
    batch.error = (
        f"{len(progress.deferred_ids)} 张试卷的数学判分已重试一次仍未完成；"
        "其他结果已保存，服务恢复后重新提交即可继续。"
    )
    await _save_quick_batch_meta(batch)
    return {
        "code": "GRADING_PAPERS_PENDING",
        "message": batch.error,
        "error_info": failures[0]["error_info"] if failures else None,
        "failures": failures,
    }


async def _mark_grading_interrupted(
    batch: QuickBatch,
    error: str,
    *,
    reason: str,
    keep_error: bool = False,
) -> None:
    """Checkpoint a run that ended early; its finished results stay saved."""
    batch.status = "grading_interrupted"
    if not (keep_error and batch.error):
        batch.error = error
    try:
        await _save_quick_batch_meta(batch)
    except Exception:
        logger.exception("failed to checkpoint %s grading batch %s", reason, batch.batch_id)


async def _grading_events(
    req: ConfirmReq,
    user: User,
    batch: QuickBatch,
    confirmed: list[AnswerSpec],
):
    """Grade a batch against its frozen contract (SSE), resuming from checkpoints.

    The batch lock is held for the whole run. A reconnect first receives the
    already graded prefix, then only unfinished papers are dispatched. Every
    graded paper is checkpointed; a run that ends early leaves the batch
    ``grading_interrupted`` with its finished results kept.
    """
    teacher_id = user.id
    db = SessionLocal()
    batch_lock = None
    grading_started = False
    try:
        try:
            batch_lock = batch_operation_lock(req.batch_id, UPLOAD_DIR)
            batch_lock.__enter__()
        except BatchOperationBusy:
            batch_lock = None
            yield _sse("error", {
                "code": "BATCH_GRADING_BUSY",
                "message": "该批次正在批改，请等待当前任务完成，不要重复提交",
            })
            return

        # The request may have waited behind another worker before entering
        # the generator. Always resume from the newest durable checkpoint.
        batch = QuickBatch.load_meta(req.batch_id, UPLOAD_DIR)
        _require_owned_batch(batch, user)
        _require_complete_batch_ocr(batch)
        _require_orientation_ready(batch)
        contract, contract_hash, has_stored_contract = _grading_contract_for_run(
            db, batch, req, teacher_id, confirmed,
        )
        confirmed_for_grading = _contract_answer_specs(contract)
        identity_plan = contract["identity_plan"]
        grading_mode = str(contract["mode"])
        grading_category = str(contract["category"])
        grading_exam_name = str(contract.get("exam_name") or "")

        # Terminal replay remains idempotent, but only for the exact frozen
        # contract. A changed request must never silently replay old marks.
        if batch.status in _REVIEW_FINISHED_STATUSES and batch.results:
            if not has_stored_contract:
                raise _GradingRefused(
                    "GRADING_CONTRACT_UNRECOVERABLE",
                    "旧批次缺少可验证的批改合同，不能安全重放；请新建批次",
                )
            for event in _finished_grading_replay(batch):
                yield event
            return

        # Recheck immediately before freezing the contract or switching to
        # grading. This closes the race between the HTTP preflight and
        # generator start.
        unavailable = await _scoring_chain_unavailable(batch)
        if unavailable is not None:
            yield _sse("error", unavailable)
            return

        _freeze_grading_contract(batch, contract, contract_hash)
        # _prepare_identity_plan may assign stable IDs to legacy entries.
        await _save_quick_batch_meta(batch)
        stamp = _start_grading_run(batch, confirmed_for_grading)
        await _save_quick_batch_meta(batch)
        grading_started = True

        total = len(batch.papers)
        yield _sse("progress", {
            "message": "恢复批改" if batch.results else "开始批改",
            "current": 0,
            "total": total,
            "grading_elapsed_seconds": _grading_elapsed_seconds(batch),
        })
        progress = _GradingProgress.of(batch)
        detail_level = "simple" if grading_mode in ("auto", "simple") else "detailed"
        deadline_at = asyncio.get_running_loop().time() + _GRADE_BATCH_DEADLINE_S
        # A reconnect receives the complete prefix, then only unfinished
        # papers are dispatched. This keeps frontend completion accounting
        # stable without paying for already checkpointed model calls.
        for event in _completed_papers_replay(progress, total):
            yield event

        async def grade_one(item: tuple[int, dict]) -> tuple[str, dict | None, dict]:
            idx, paper = item
            paper_id = str(paper.get("paper_id") or f"paper-{idx + 1}")
            return await _grade_paper_core(
                batch, paper, paper_id, identity_plan[paper_id], teacher_id,
                detail_level=detail_level,
                grading_category=grading_category,
                grading_exam_name=grading_exam_name,
                grading_mode=grading_mode,
                stamp=stamp,
            )

        async with aclosing(
            _graded_papers(progress.pending_items, grade_one, deadline_at=deadline_at)
        ) as graded_papers:
            async for kind, graded in graded_papers:
                if kind == "deadline":
                    yield _sse("error", await _stop_for_grading_deadline(batch, progress))
                    return
                if kind == "heartbeat":
                    yield _sse("progress", {
                        "message": "模型正在批改，请保持页面打开",
                        "current": len(progress.completed_ids),
                        "total": total,
                        "heartbeat": True,
                        "grading_elapsed_seconds": _grading_elapsed_seconds(batch),
                    })
                    continue
                for event in await _record_graded_paper(batch, progress, graded, total):
                    yield event

        if progress.deferred_ids:
            yield _sse("error", await _stop_for_deferred_papers(batch, progress))
            return
        if len(progress.completed_ids) != total:
            raise RuntimeError(
                f"grading checkpoint incomplete: {len(progress.completed_ids)}/{total}"
            )
        await _finish_grading_run(batch, progress, teacher_id)
        yield _sse("done", {
            "total": len(batch.results),
            "avg_score": _review_avg_score(batch.results),
            "review_revision": batch.review_revision,
            "grading_elapsed_seconds": batch.grading_elapsed_seconds,
        })
    except _GradingRefused as refused:
        yield _sse("error", refused.payload)
    except asyncio.CancelledError:
        if grading_started and batch.status == "grading":
            await _mark_grading_interrupted(
                batch, "批改连接中断，已保留完成结果，可重新提交继续", reason="cancelled",
            )
        raise
    except Exception as e:
        info = report_failure(e, stage="grading", task_id=batch.batch_id)
        try:
            db.rollback()
        except Exception:
            pass
        if grading_started:
            await _mark_grading_interrupted(batch, _GENERIC_GRADE_ERROR, reason="errored")
        yield _sse("error", {
            "code": "GRADING_CHAIN_FAILED",
            "message": "批改链路发生故障，已保留完成结果；请稍后重新提交以继续未完成试卷",
            "error_info": info,
        })
    finally:
        if grading_started and batch.status not in _REVIEW_FINISHED_STATUSES | {"done"}:
            await _mark_grading_interrupted(
                batch, "批改未完成，已保留完成结果，可重新提交继续",
                reason="unfinished", keep_error=True,
            )
        db.close()
        if batch_lock is not None:
            batch_lock.__exit__(None, None, None)


@router.post("/batch-grade/confirm")
async def batch_grade_confirm(
    req: ConfirmReq,
    user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    """阶段二：按批次冻结模板的答案批改（SSE）。

    两种模式流程完全一致（题型分流 + 整卷调用），只有 Flash 输出详细程度不同：
    - precise（详细）：输出解析 + 标准答案 + 指导建议
    - simple （简单）：只输出对/错（兼容旧值 auto）
    选择题恒走机械比对（0 成本），填空/大题走 Flash 整卷调用。
    批次必须来自已确认模板并直接复用冻结快照。

    SSE events:
      - progress: {message, current, total}
      - result:   {student_name, grading_result_id, profile_id, score, correct, partial, wrong, total, ...}
      - done:     {total, avg_score, [idempotent]}
      - error:    {message}
    """
    if req.mode not in ("precise", "auto", "simple"):
        raise HTTPException(400, detail="mode 必须是 precise/auto/simple")
    if req.category not in VALID_GRADING_CATEGORIES:
        raise HTTPException(400, detail="category 必须是 daily 或 exam")
    batch = QuickBatch.load_meta(req.batch_id, UPLOAD_DIR)
    _require_owned_batch(batch, user)
    _require_complete_batch_ocr(batch)
    _require_orientation_ready(batch)
    if not batch.papers and batch.status != "done":
        raise HTTPException(404, detail="批次不存在或已过期")

    # One answer per frozen question; empty or duplicate numbers fail with 409.
    confirmed = _authoritative_answers_for_confirmation(batch, req.category)
    terminal_result_replay = bool(
        batch.status in _REVIEW_FINISHED_STATUSES
        and batch.results
    )
    # Pure request validation must happen before StreamingResponse is created,
    # otherwise FastAPI has already committed HTTP 200 when an identity error
    # is discovered. The generator repeats this check after taking the batch
    # lock to protect against a concurrent metadata change.
    frozen_identity_plan = (
        batch.grading_contract.get("identity_plan") or {}
        if isinstance(batch.grading_contract, dict)
        else {}
    )
    if batch.identity_mode == "anonymous":
        _prepare_anonymous_identity_plan(batch)
    elif frozen_identity_plan or not terminal_result_replay:
        _prepare_identity_plan(
            db,
            batch,
            req.identity_resolutions,
            user.id,
            frozen_fallback=frozen_identity_plan,
        )

    # Completed batches only replay durable results (or return a frozen-contract
    # error), so they remain readable during an outage. Every request that can
    # start or resume grading must fail with HTTP 503 before StreamingResponse
    # commits its status line.
    if not terminal_result_replay:
        await _require_batch_scoring_chain(batch)

    return StreamingResponse(
        stream_teaching(batch.teaching_context, _grading_events(req, user, batch, confirmed)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _load_owned_review_batch(batch_id: str, user: User) -> QuickBatch:
    batch = QuickBatch.load_meta(batch_id, UPLOAD_DIR)
    if not batch.review_results:
        raise HTTPException(404, detail="批次不存在或尚无待审阅结果")
    _require_owned_batch(batch, user)
    return batch


# Per-paper pipeline grading checkpoints review rows while recognition and
# grading are still running, so "has review rows" no longer means "graded".
_REVIEW_COMPLETE_STATUSES = {"review_ready", "voided", "archived", "done"}


def _require_review_complete(batch: QuickBatch) -> None:
    if batch.status not in _REVIEW_COMPLETE_STATUSES:
        raise HTTPException(
            409,
            detail="本批次批阅尚未全部完成，请先继续批阅；完成后才能人工判定、作废或入档",
        )


@contextmanager
def _locked_owned_review_batch(batch_id: str, user: User):
    """Serialize review mutations so stale QuickBatch snapshots cannot overwrite each other."""
    # Validate existence/ownership before creating a lock inode, then reload after
    # acquisition so the mutation always starts from the newest serialized state.
    _require_review_complete(_load_owned_review_batch(batch_id, user))
    try:
        with batch_operation_lock(batch_id, UPLOAD_DIR):
            batch = _load_owned_review_batch(batch_id, user)
            _require_review_complete(batch)
            yield batch
    except BatchOperationBusy as exc:
        raise HTTPException(409, detail="该批次正在执行另一项审阅操作，请稍后重试") from exc


def _review_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mark_grading_started(batch: QuickBatch) -> None:
    """Start the batch grading stopwatch once, when the first paper is graded."""
    if not batch.grading_started_at:
        batch.grading_started_at = _review_timestamp()


def _grading_elapsed_seconds(batch: QuickBatch) -> float | None:
    """Wall-clock seconds since the first paper started grading."""
    try:
        started = datetime.fromisoformat(batch.grading_started_at)
    except (TypeError, ValueError):
        return None
    return round(max(0.0, (datetime.now(timezone.utc) - started).total_seconds()), 1)


@router.get("/batch-grade/report/{batch_id}")
async def batch_grade_get_report(
    batch_id: str,
    user: User = Depends(_require_user),
):
    """Return the durable teaching report, starting it lazily for old batches."""
    batch = _load_owned_review_batch(batch_id, user)
    batch = await _ensure_batch_report_generation(batch, user.id)
    return {
        "ok": True,
        "batch_id": batch.batch_id,
        "report": _batch_report_response(batch),
    }


def _find_review_result(batch: QuickBatch, paper_id: str) -> dict:
    review = next(
        (item for item in batch.review_results if str(item.get("paper_id") or item.get("review_id") or "") == paper_id),
        None,
    )
    if review is None:
        raise HTTPException(404, detail="该学生试卷不属于本批次")
    return review


# The recognition OCR phase lives in routers/grade_recognition.py, which reads
# the helpers above from this module; its names stay importable from here.
from routers.grade_recognition import (  # noqa: E402,F401
    _ocr_and_grading_events,
    _ocr_job_worker,
    _ocr_outcome_message,
    _ocr_phase_events,
    _recognize_upload,
)
