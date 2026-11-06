"""批改接口 — 支持日常作业/考试两类导入 + 批量批改"""

import asyncio
import copy
import os
import re
import uuid
import logging
from contextlib import aclosing
from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db, SessionLocal
from services.processing_errors import report_failure
from services.teaching_context import require_teacher_stage, stream_teaching
from services.ocr_service import (
    reset_vision_batch_paper_count,
    reset_vision_phase_key,
    set_vision_batch_paper_count,
    set_vision_phase_key,
)
from services.bounded_dispatch import (
    bounded_completion_map,
)
from services.paper_cache import PaperCache
from services.paper_orientation import orientation_is_ready
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
    ocr_job_attempts,
    queued_recognition_uploads,
    requeue_interrupted_ocr_jobs,
    requeue_retryable_ocr_failures,
    sync_pending_recognition_uploads,
)
from services.template_session import TemplateSession
from services.grading_contract import VALID_GRADING_CATEGORIES
from services.collection_identity import parse_source_paths
from services.identity_inheritance import inheritance_upload_order_key
from services.student_service import canonical_student_no
from models import Student
from models_user import User
from routers.auth import _require_user
from config import OCR_PREPROCESS_LOCAL_MAX_CONCURRENT, UPLOAD_DIR
# Plumbing shared with the other routers/grade_*.py modules.
from routers.grade_common import (
    _GRADE_HEARTBEAT_SECONDS,
    _SSE_PAUSE_CODES,
    _sse,
    _settle_template_component,
    _get_quick_batch_meta_io_semaphore,
    _save_quick_batch_meta,
    _new_template_session,
    _load_owned_template_session,
    _locked_owned_template_session,
    _template_knowledge_classifier,
    _review_avg_score,
    _require_owned_batch,
    _stable_review_image_hash,
    _load_owned_review_batch,
    _REVIEW_COMPLETE_STATUSES,
    _require_review_complete,
    _locked_owned_review_batch,
    _review_timestamp,
    _find_review_result,
)
from routers.grade_uploads import (
    _jpeg_dimensions,
    _validate_client_ocr_ready_upload,
    _client_ocr_ready_orientation,
    _hash_saved_uploads,
    _remove_batch_uploads,
    _stream_to_disk_capped_with_sha256,
    _save_batch_uploads,
    _resolve_safe_image_path,
    _check_limits_or_raise,
    _ORIENTATION_DIAGNOSTIC_FIELDS,
    _orientation_diagnostics,
)
from routers.grade_availability import router as _availability_router
from routers.grade_availability import (
    _with_grading_hardware,
    _batch_grade_service_availability,
    batch_grade_availability,
    _require_batch_grade_chain,
    _batch_grade_scoring_availability,
    _require_batch_scoring_chain,
)
from routers.grade_report import router as _report_router
from routers.grade_report import (
    _batch_report_tasks,
    _batch_report_response,
    _batch_knowledge_template,
    _refresh_batch_knowledge,
    _reconcile_batch_report_sync,
    _prepare_batch_report_knowledge,
    _finish_batch_report_sync,
    _fail_batch_report_sync,
    _run_batch_report_generation,
    _discard_batch_report_task,
    _start_batch_report_generation,
    _ensure_batch_report_generation,
    batch_grade_get_report,
)
from routers.grade_contract import (
    _validate_template_for_recognition,
    _template_handwriting_regions_for_mode,
    _build_template_paper_cache,
    _single_page_template_failure_questions,
    _mark_unverified_answer_alignment,
    _reject_answer_key_matching_local_recoveries,
    _initialize_batch_answer_map,
    _confirmation_questions,
    _authoritative_answers_for_confirmation,
    _GRADING_CONTRACT_VERSION,
    _grading_question_sort_key,
    _frozen_answer_payload,
    _build_grading_contract,
    _grading_contract_hash,
)
from routers.grade_papers import (
    _paper_source_metadata,
    _stored_paper_orientation,
    _anonymous_page_identity,
    _resolve_identity_with_filename_clues,
    _ingest_recognized_paper,
    _refresh_identity_inheritance,
    _recognize_batch_upload,
    _class_roster_names,
    _apply_ocr_outcome,
    _require_complete_batch_ocr,
    _require_orientation_ready,
    _RAW_PAPER_EXPIRED_MESSAGE,
    _batch_paper_display_path,
    _batch_review_image_path,
)
from routers.grade_paper_checks import router as _paper_checks_router
from routers.grade_paper_checks import (
    batch_grade_identity_options,
    _orientation_state_payload,
    batch_grade_orientation_state,
    _EXCLUDED_PAPER_MESSAGE,
    batch_grade_exclude_failed_paper,
    _save_client_orientation_upload,
    batch_grade_confirm_orientation,
    _orientation_action_id,
    _confirm_orientation_locked,
    _require_orientation_editable,
    _batch_upload_or_404,
    _orientation_action,
    _require_orientation_change,
    _orientation_template_evidence,
    _verified_original_source_hash,
    _require_rotatable_page,
    _begin_orientation_retry,
    _recognize_reoriented_upload,
    _finish_orientation_retry,
    batch_grade_paper_image,
)

logger = logging.getLogger(__name__)
router = APIRouter()
# GET /batch-grade/availability stays this router's first route.
router.include_router(_availability_router)


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


# ════════════════════════════════════════════════════════════════════
#  统一批阅流程：recognize（按冻结模板识别学生答案）→ confirm（按模式批改）
#  precise 模式出详细解析，simple/auto 模式只判对错（详略不同，
#  流程一致：选择题机械比对，其余题走 LLM）。
# ════════════════════════════════════════════════════════════════════


# ── Student recognition steps shared by recognize and resume ─────────────────


def _discard_unstarted_batch(batch: QuickBatch, saved_uploads: list[tuple]) -> None:
    """Remove a rejected batch's uploads and metadata before recognition starts."""
    _remove_batch_uploads(saved_uploads)
    try:
        os.remove(batch.meta_path)
    except OSError:
        pass


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


_REVIEW_FINISHED_STATUSES = {"review_ready", "voided", "archived"}


# Identity options, orientation, exclusion and paper images keep their place
# after the recognition routes.
router.include_router(_paper_checks_router)


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


# GET /batch-grade/report/{batch_id} stays this router's last route.
router.include_router(_report_router)


# The recognition OCR phase lives in routers/grade_recognition.py, which reads
# the helpers above from this module; its names stay importable from here.
from routers.grade_recognition import (  # noqa: E402,F401
    _ocr_and_grading_events,
    _ocr_job_worker,
    _ocr_outcome_message,
    _ocr_phase_events,
    _recognize_upload,
)

# Grading execution (the per-paper pipeline, its rubric pins and the confirm
# grading stream) lives in routers/grade_pipeline.py, which reads the helpers
# above from this module; its names stay importable from here.
from routers.grade_pipeline import (  # noqa: E402,F401
    _GENERIC_GRADE_ERROR,
    _GRADE_BATCH_DEADLINE_S,
    _GradingProgress,
    _GradingRefused,
    _PerPaperGradingPipeline,
    _RUBRIC_METADATA_KEYS,
    _batch_rubric_hash,
    _build_template_confirmed_map,
    _completed_papers_replay,
    _confirmation_questions_from_contract,
    _contract_answer_specs,
    _finish_grading_run,
    _finished_grading_replay,
    _freeze_grading_contract,
    _grade_paper_core,
    _graded_papers,
    _grading_contract_for_run,
    _grading_elapsed_seconds,
    _grading_events,
    _mark_grading_interrupted,
    _mark_grading_started,
    _pin_batch_rubric,
    _pipeline_grading_answer_map,
    _pipeline_identity_assignment,
    _pipeline_result_sse,
    _record_graded_paper,
    _row_matches_rubric_pin,
    _scoring_chain_unavailable,
    _start_grading_run,
    _start_per_paper_grading,
    _stop_for_deferred_papers,
    _stop_for_grading_deadline,
    _upsert_batch_paper_result,
)
