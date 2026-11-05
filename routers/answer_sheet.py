"""Independent answer-sheet grading workflow API."""
from __future__ import annotations

import asyncio
import copy
import inspect
import hashlib
import json
import logging
import os
import re
import shutil
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import AsyncIterator, Awaitable, Literal

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from config import (
    MAX_TEMPLATE_PAGES,
    MAX_UPLOAD_FILES,
    MAX_UPLOAD_FILE_SIZE,
    MAX_UPLOAD_TOTAL_SIZE,
    OCR_BATCH_DISPATCH_CONCURRENT,
    OCR_TEACHER_WAVE_SIZE,
    UPLOAD_DIR,
)
from database import get_db
from models import GradingCorrection, Student
from models_rate_limit import TeacherClass
from models_user import User
from routers.auth import _require_user
from services.answer_sheet_session import (
    BATCH_CONTRACT_VERSION,
    AnswerSheetOperationBusy,
    AnswerSheetSession,
    answer_sheet_batch_label_lock,
    answer_sheet_operation_lock,
    list_teacher_batches,
)
from services.answer_sheet_paper_templates import (
    PaperTemplateNotFound,
    PaperTemplateRegistry,
    get_paper_template_registry,
    list_detailed_template_sources,
    paper_answer_spec_hash,
)
# Heavy answer-sheet services drag image runtimes (numpy/cv2) — import them
# lazily inside the functions that use them, like the load_rgb_image shim
# below. A module-level import would break the app-startup import boundary
# test (tests/test_image_runtime_import_boundaries.py).
from services.answer_sheet_layout_contract import choice_row_keys
from services.answer_sheet_page_pairing import (
    PATH_PAIRED_STATUS,
    apply_path_pair_identity,
)
from services.collection_identity import normalize_source_path
from services.answer_sheet_template_locator import locate_answer_sheet_template
from services.answer_sheet_template_registry import (
    AnswerSheetTemplateIncompatible,
    AnswerSheetTemplateNotFound,
    get_answer_sheet_template_registry,
    normalize_template_code,
)
from services.accelerator_business import (
    adopt_accelerated_template_canonicalization,
    finish_business_preprocessing,
    resume_accelerated_template_canonicalization,
    try_accelerated_template_canonicalization,
    try_accelerated_evidence,
)
from services.answer_sheet_acceleration import (
    bind_answer_sheet_evidence_locations,
    build_answer_sheet_acceleration_plan,
    transcribe_answer_sheet_evidence,
)
from services.security import stream_to_disk_capped, validate_upload_path
from services.flash_grading import FLASH_LOCAL_MAX_CONCURRENT
from services.template_recognition import (
    generate_template_answers_batch,
    recognize_template_blank,
    recognize_template_with_answers,
)
from services.template_evidence import (
    TemplateEvidenceError,
    TemplateEvidenceMatcher,
    accept_canonical_template_candidate,
    build_template_artifact,
    # Kept as a module-level compatibility hook for desktop/test adapters.
    # Production layout uploads use the client canonicalization contract.
    canonicalize_template_image,
    parse_canonical_metadata,
)
from services.template_anchor_crops import AnchorCropPlanError, build_anchor_crop_plan
from services.paper_orientation import (
    create_teacher_oriented_copy,
    orientation_is_ready,
    sha256_file,
)
from services.template_column_layout import TemplateCutError, recognize_column_layout
from services.preprocess import ocr_ready_storage_suffix
from services.ocr_service import (
    _get_local_ocr_preprocess_semaphore,
    _run_local_ocr_image_component,
    reset_vision_batch_deadline_s,
    reset_vision_batch_paper_count,
    reset_vision_phase_key,
    reset_vision_route_ordinal,
    set_vision_batch_deadline_s,
    set_vision_batch_paper_count,
    set_vision_phase_key,
    set_vision_route_ordinal,
)
from services.bounded_dispatch import (
    bounded_completion_map,
    teacher_waved_completion_map,
)
from services.grading_contract import AnswerSpec
from services.scoring_engine import recalculate_result_from_details
from services.system_readiness import (
    READINESS_IO_TIMEOUT_SECONDS,
    get_public_hardware_readiness,
    get_upload_storage_readiness_async,
)
from services.teaching_context import (
    await_teaching,
    current_teaching_context,
    make_teaching_context,
    require_teacher_stage,
    stream_teaching,
    use_teaching_context,
)

router = APIRouter(prefix="/answer-sheet")
logger = logging.getLogger(__name__)
_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png"}
_CLIENT_OCR_READY_SUFFIX = ".ocr-ready-v1.jpg"
_CLIENT_OCR_READY_MAX_EDGE = 1920
_ALIGNED_EVIDENCE_RE = re.compile(r"aligned-[0-9a-f]{32}\.png\Z")
_ANSWER_CROP_RE = re.compile(r"crop-[0-9a-f]{24}\.png\Z")
_STUDENT_RECOGNITION_ATTEMPT_RE = re.compile(
    r"\.student-recognition-[0-9a-f]{32}\Z"
)
_ANSWER_SHEET_HEARTBEAT_SECONDS = 15.0
_ANSWER_SHEET_ACCELERATOR_WAIT_SECONDS = 60.0
_ANSWER_SHEET_OCR_BATCH_DEADLINE_SECONDS = 600.0
_GRADING_DRAFT_CHECKPOINT_KEY = "grading_draft_checkpoint"
_RAW_PAPER_EXPIRED_MESSAGE = "原卷已按隐私保留策略清理，错题、得分和正确率记录仍保留"
_ORDERED_DUPLEX_PAIRING = "ordered_duplex"
_IDENTITY_READY_STATUSES = {
    "recognized", "teacher_confirmed", "ordered_pair_inherited", PATH_PAIRED_STATUS,
}
_ORDERED_PAIRING_REASONS = {
    "ordered_pair_missing_back",
    "ordered_pair_template_unknown",
    "ordered_pair_page_type_mismatch",
    "ordered_pair_front_identity_missing",
    "ordered_pair_identity_conflict",
}


def load_rgb_image(image_path: str):
    from services.answer_sheet_identity import load_rgb_image as implementation

    return implementation(image_path)


def crop_normalized_rgb(image_rgb, bbox: list[int]):
    from services.answer_sheet_identity import crop_normalized_rgb as implementation

    return implementation(image_rgb, bbox)


def decode_machine_code(crop_rgb, code_type: str) -> dict:
    from services.answer_sheet_identity import decode_machine_code as implementation

    return implementation(crop_rgb, code_type)


def read_exam_no_marks(crop_rgb, config: dict) -> dict:
    from services.answer_sheet_identity import read_exam_no_marks as implementation

    return implementation(crop_rgb, config)


def read_choice_marks(crop_rgb, options: list[str]) -> dict:
    from services.answer_sheet_identity import read_choice_marks as implementation

    return implementation(crop_rgb, options)


class AnswerSheetAcceleratorState(RuntimeError):
    """A student page is durable but remote image work cannot continue yet."""

    def __init__(self, state: str, reason_code: str = ""):
        self.state = str(state or "fault")
        self.reason_code = str(reason_code or "")
        super().__init__(self.reason_code or self.state)


def _accelerator_managed_orientation(_path: str) -> dict:
    """Accept browser-normalized pixels; registration verifies page geometry."""

    return {
        "orientation_status": "auto_passed",
        "orientation_reason": "client_normalized_node_registered",
        "orientation_confidence": 1.0,
    }


def _answer_sheet_sse(event: str, payload: dict) -> str:
    return (
        f"event: {event}\n"
        f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


def _answer_sheet_stream(events: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _with_answer_sheet_heartbeats(
    awaitable: Awaitable,
    *,
    message: str,
    current: int,
    total: int,
):
    """Yield progress while one model or image operation is still alive."""
    task = asyncio.create_task(awaitable)
    try:
        while True:
            done, _ = await asyncio.wait(
                {task}, timeout=_ANSWER_SHEET_HEARTBEAT_SECONDS
            )
            if done:
                yield "result", task.result()
                return
            yield "progress", {
                "message": message,
                "current": current,
                "total": total,
                "heartbeat": True,
            }
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _with_answer_sheet_hardware(availability: dict, hardware: dict) -> dict:
    """Attach anonymous hardware readiness and apply its public severity."""
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
                "queued": "需排队",
                "unavailable": "暂不可用",
                "fault": "链路故障",
            }[mapped_status],
            message=public_hardware["message"],
        )
    return result


async def _answer_sheet_service_availability(user: User) -> dict:
    """Return feature, storage, model and coordinator readiness."""
    import config

    if not config.ANSWER_SHEET_GRADING_ENABLED:
        return {
            "status": "unavailable",
            "label": "暂不可用",
            "message": "答题卡批阅暂时不可用：真实学生答题卡裁剪验收尚未通过。",
        }

    storage = await get_upload_storage_readiness_async(
        UPLOAD_DIR,
        MAX_UPLOAD_TOTAL_SIZE,
        required_copies=2,
    )
    if not storage.get("ready"):
        if storage.get("status") == "unavailable":
            return {
                "status": "unavailable",
                "label": "暂不可用",
                "message": "服务器存储空间不足，已暂停接收新答题卡批阅任务，请联系管理员清理空间。",
            }
        return {
            "status": "fault",
            "label": "链路故障",
            "message": "服务器存储状态异常，已暂停接收新答题卡批阅任务，请联系管理员。",
        }

    from services.analyzer import get_grading_readiness, get_grading_readiness_async
    from services.model_concurrency import RedisModelLimiter, get_model_limiter
    from services.ocr_service import get_ocr_readiness
    from services.ocr_teacher_fairness import public_teacher_availability

    try:
        async with asyncio.timeout(READINESS_IO_TIMEOUT_SECONDS):
            limiter = get_model_limiter()
            if isinstance(limiter, RedisModelLimiter):
                await limiter.ping()
            ocr = await get_ocr_readiness()
            grading = get_grading_readiness()
            if isinstance(limiter, RedisModelLimiter):
                grading = await get_grading_readiness_async(limiter)
    except Exception:
        logger.exception("answer-sheet grading readiness coordination failed")
        return {
            "status": "fault",
            "label": "链路故障",
            "message": "答题卡批阅任务协调服务发生故障，请稍后重试或联系管理员。",
        }

    if not ocr.get("configured") or not ocr.get("ready"):
        return {
            "status": "unavailable",
            "label": "暂不可用",
            "message": "视觉识别服务尚未就绪，请稍后重试或联系管理员。",
        }
    if not grading.get("configured") or not grading.get("ready"):
        return {
            "status": "unavailable",
            "label": "暂不可用",
            "message": "数学批改服务尚未就绪，请稍后重试或联系管理员。",
        }

    try:
        async with asyncio.timeout(READINESS_IO_TIMEOUT_SECONDS):
            capacity = await public_teacher_availability(str(user.id))
    except Exception:
        logger.exception("answer-sheet teacher availability failed")
        return {
            "status": "fault",
            "label": "链路故障",
            "message": "答题卡批阅任务协调服务发生故障，请稍后重试或联系管理员。",
        }
    if capacity.get("status") == "queued":
        return {
            "status": "queued",
            "label": "需排队",
            "message": "当前使用人数较多，提交后会按顺序处理。",
        }
    if capacity.get("status") == "fault":
        return {
            "status": "fault",
            "label": "链路故障",
            "message": "答题卡批阅任务协调服务发生故障，请稍后重试或联系管理员。",
        }
    if capacity.get("status") != "available":
        return {
            "status": "unavailable",
            "label": "暂不可用",
            "message": "视觉识别服务暂不可用，请稍后重试或联系管理员。",
        }
    return {
        "status": "available",
        "label": "可用",
        "message": "视觉识别、数学批改和任务协调链路已就绪。",
    }


@router.get("/availability")
async def answer_sheet_availability(user: User = Depends(_require_user)):
    """Expose whole-chain readiness without probing a billable model API."""
    hardware, availability = await asyncio.gather(
        get_public_hardware_readiness(user.id),
        _answer_sheet_service_availability(user),
    )
    return _with_answer_sheet_hardware(availability, hardware)


class SessionCreateRequest(BaseModel):
    # The class is inherited from the teacher's global current class, and the
    # daily/exam grading type is no longer a batch choice.  Rejecting unknown
    # fields makes stale clients fail visibly instead of choosing silently.
    model_config = ConfigDict(extra="forbid")
    paper_template_code: str = Field(..., min_length=8, max_length=40)
    answer_sheet_template_code: str | None = Field(None, min_length=8, max_length=40)


class PaperTemplateRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_id: str = Field(..., min_length=1, max_length=64)
    alias: str = Field("", max_length=40)


class TemplateAliasRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alias: str = Field("", max_length=40)


class AnswerConfirmRequest(BaseModel):
    questions: list[dict] = Field(..., min_length=1, max_length=200)


class LayoutSaveRequest(BaseModel):
    answer_regions: list[dict] = Field(default_factory=list, max_length=200)
    identity_regions: list[dict] = Field(default_factory=list, max_length=100)


class TemplateCodeRequest(BaseModel):
    template_code: str = Field(..., min_length=8, max_length=40)


class BubbleGridCell(BaseModel):
    model_config = ConfigDict(extra="forbid")
    option: str = Field(..., min_length=1, max_length=1)
    bbox: list[int] = Field(..., min_length=4, max_length=4)


class BubbleGridKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question_no: str = Field(..., min_length=1, max_length=20)
    subquestion_no: str = Field("", max_length=4)


class BubbleGridConfirmation(BubbleGridKey):
    # Who and when are stamped by the server; a request carrying
    # confirmation state or provenance is rejected, not trusted.
    grid_order: Literal["left_to_right", "top_to_bottom"] = "left_to_right"
    cells: list[BubbleGridCell] = Field(..., min_length=2, max_length=8)


class BubbleGridConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grids: list[BubbleGridConfirmation] = Field(..., min_length=1, max_length=200)


class BubbleGridDiscardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keys: list[BubbleGridKey] = Field(..., min_length=1, max_length=200)


class OrientationConfirmRequest(BaseModel):
    quarter_turns_clockwise: int = Field(..., ge=0, le=3)


class StudentUploadStartRequest(BaseModel):
    restart: bool = False
    pairing_mode: Literal["ordered_duplex"] = _ORDERED_DUPLEX_PAIRING


class ReviewConfirmRequest(BaseModel):
    student_id: int = Field(..., ge=1)
    answers: dict[str, str] = Field(default_factory=dict)


class GradingDecision(BaseModel):
    question_no: str = Field(..., min_length=1, max_length=50)
    result: Literal["correct", "partial", "wrong"]
    note: str = Field("", max_length=500)


class GradingDecisionRequest(BaseModel):
    student_id: int = Field(..., ge=1)
    review_revision: int = Field(..., ge=0)
    decisions: list[GradingDecision] = Field(..., min_length=1, max_length=200)


def _owned_class(db: Session, class_id: int, user_id: int) -> TeacherClass:
    row = db.query(TeacherClass).filter(
        TeacherClass.id == class_id,
        TeacherClass.teacher_id == user_id,
        TeacherClass.deleted_at.is_(None),
    ).first()
    if not row:
        raise HTTPException(404, detail="班级不存在")
    return row


def _inherited_class_id(db: Session, user: User) -> int:
    """Every new batch inherits the teacher's global current class."""
    class_id = getattr(user, "current_class_id", None)
    if class_id is None:
        raise HTTPException(400, detail="请先在顶部导航选择当前班级后再建立批次")
    return _owned_class(db, int(class_id), user.id).id


@contextmanager
def _batch_label_binding(session: AnswerSheetSession, template_code: str, user_id: int):
    """Assign the storage label and keep it unique until the caller saves.

    Historical sessions keep their legacy identity.  The teacher/template
    label lock spans the uniqueness scan and the caller's save, so two
    simultaneous requests cannot both claim the same second; a second batch of
    the same template in the same second is rejected instead of sharing a label.
    """
    if not session.is_batch:
        yield
        return
    with ExitStack() as stack:
        try:
            stack.enter_context(
                answer_sheet_batch_label_lock(UPLOAD_DIR, user_id, template_code)
            )
        except AnswerSheetOperationBusy as exc:
            raise HTTPException(409, detail="同一答题卡模板正在建立批次，请稍后重试") from exc
        already_bound = bool(session.batch_label)
        try:
            label = session.bind_batch_label(template_code)
        except ValueError as exc:
            raise HTTPException(409, detail=str(exc)) from exc
        if not already_bound and any(
            item.get("batch_label") == label and item.get("session_id") != session.session_id
            for item in list_teacher_batches(UPLOAD_DIR, user_id, limit=10_000)
        ):
            session.batch_label = ""
            session.answer_sheet_template_code = ""
            raise HTTPException(409, detail="同一答题卡模板在同一秒内已建立批次，请稍后重试")
        yield


def _verified_paper_template(session: AnswerSheetSession) -> dict | None:
    """Fail closed when a batch snapshot no longer matches its paper template."""
    if not session.is_batch:
        return None
    try:
        manifest = get_paper_template_registry().load(
            session.paper_template_code, int(session.teacher_id or 0),
        )
    except ValueError as exc:
        raise HTTPException(409, detail=f"试卷模板不可用，禁止判分：{exc}") from exc
    content = manifest["content"]
    expected = manifest["answer_spec_hash"]
    actual = paper_answer_spec_hash(
        session.answer_spec_snapshot,
        content.get("teaching_context") or {},
        str(content.get("scoring_category") or ""),
    )
    if session.answer_spec_hash != expected or actual != expected:
        raise HTTPException(409, detail="试卷模板 answer_spec_hash 与批次快照不一致，禁止判分")
    return manifest


def _result_scoring(session: AnswerSheetSession) -> tuple[str, str]:
    """Return (result-system category, exam name) without a batch-level choice.

    A new batch takes both from its frozen paper template; a historical
    session keeps its read-only legacy values.
    """
    manifest = _verified_paper_template(session)
    if manifest is None:
        return session.result_category(), session.result_exam_name()
    content = manifest["content"]
    return str(content["scoring_category"]), str(content.get("exam_name") or "")


def _verified_bubble_grids(session: AnswerSheetSession) -> dict | None:
    """Fail closed unless a batch's choice marks resolve via confirmed grids.

    The geometry is read from the frozen answer-sheet template only; a batch
    without a published template, with a changed template, or with any grid
    row that is still a model proposal cannot be graded.
    """
    if not session.is_batch or not any(
        region.get("region_type") == "choice_mark" for region in session.answer_regions
    ):
        return None
    try:
        return get_answer_sheet_template_registry().resolve_bubble_regions(session)
    except ValueError as exc:
        raise HTTPException(409, detail=f"涂卡网格未通过教师确认模板校验，禁止判分：{exc}") from exc


def _load_owned(session_id: str, user_id: int) -> AnswerSheetSession:
    try:
        session = AnswerSheetSession.load(session_id, UPLOAD_DIR)
    except (ValueError, OSError):
        raise HTTPException(404, detail="答题卡会话不存在")
    try:
        session.ensure_owned(user_id)
    except PermissionError:
        raise HTTPException(404, detail="答题卡会话不存在")
    return session


@contextmanager
def _locked_owned(session_id: str, user_id: int):
    """Serialize grading mutations and always reload after lock acquisition."""
    _load_owned(session_id, user_id)
    try:
        with answer_sheet_operation_lock(session_id, UPLOAD_DIR):
            yield _load_owned(session_id, user_id)
    except AnswerSheetOperationBusy as exc:
        raise HTTPException(
            409,
            detail="该答题卡会话正在执行另一项操作，请稍后重试",
        ) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, detail="答题卡会话不存在") from exc


def _serialized_session_mutation(function):
    """Serialize one complete metadata mutation while preserving FastAPI's signature."""
    signature = inspect.signature(function)

    def lock_arguments(args, kwargs):
        bound = signature.bind_partial(*args, **kwargs)
        return str(bound.arguments["session_id"]), bound.arguments["user"]

    if inspect.iscoroutinefunction(function):
        @wraps(function)
        async def async_wrapper(*args, **kwargs):
            session_id, user = lock_arguments(args, kwargs)
            with _locked_owned(session_id, user.id):
                return await function(*args, **kwargs)

        return async_wrapper

    @wraps(function)
    def sync_wrapper(*args, **kwargs):
        session_id, user = lock_arguments(args, kwargs)
        with _locked_owned(session_id, user.id):
            return function(*args, **kwargs)

    return sync_wrapper


def _answer_sheet_writes_enabled() -> None:
    """Fail closed for every grading mutation while read-only evidence stays available."""
    import config

    if not config.ANSWER_SHEET_GRADING_ENABLED:
        raise HTTPException(
            503,
            detail="答题卡批阅暂时不可用：真实学生答题卡裁剪验收尚未通过。",
        )


async def _require_answer_sheet_chain(user: User) -> None:
    """Reject new model/image work while preserving read-only session access."""
    availability = await answer_sheet_availability(user)
    if availability.get("status") in {"available", "queued"}:
        return
    detail = "答题卡批阅服务当前暂不可用，请稍后重试。"
    if availability.get("status") == "fault":
        detail = str(
            availability.get("message")
            or "答题卡批阅链路发生故障，请稍后重试或联系管理员。"
        )
    raise HTTPException(503, detail=detail)


def _requires_answer_sheet_writes(function):
    """Preserve FastAPI signatures while guarding sync and async write routes."""
    signature = inspect.signature(function)

    def task_context(args, kwargs):
        bound = signature.bind_partial(*args, **kwargs)
        session_id = bound.arguments.get("session_id")
        user = bound.arguments.get("user")
        if session_id and user is not None:
            return getattr(_load_owned(str(session_id), user.id), "teaching_context", {})
        return {}

    if inspect.iscoroutinefunction(function):
        @wraps(function)
        async def async_wrapper(*args, **kwargs):
            _answer_sheet_writes_enabled()
            with use_teaching_context(task_context(args, kwargs)):
                return await function(*args, **kwargs)

        return async_wrapper

    @wraps(function)
    def sync_wrapper(*args, **kwargs):
        _answer_sheet_writes_enabled()
        with use_teaching_context(task_context(args, kwargs)):
            return function(*args, **kwargs)

    return sync_wrapper


def _remove_upload_path(path: str | os.PathLike[str] | None) -> None:
    """Best-effort removal for a file that may have been only partly written."""
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        logger.warning("failed to remove incomplete upload asset")


async def _save_images(
    files: list[UploadFile],
    target: str,
    prefix: str,
    *,
    total_byte_budget: int | None = None,
) -> list[dict]:
    if not files:
        raise HTTPException(400, detail="未上传图片")
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(413, detail=f"单次最多上传 {MAX_UPLOAD_FILES} 个文件")
    if total_byte_budget is None:
        total_byte_budget = MAX_UPLOAD_TOTAL_SIZE
    if total_byte_budget <= 0 or total_byte_budget > MAX_UPLOAD_TOTAL_SIZE:
        raise HTTPException(413, detail="上传文件总大小超过限制")
    storage = await get_upload_storage_readiness_async(
        target,
        min(total_byte_budget, len(files) * MAX_UPLOAD_FILE_SIZE),
        required_copies=1,
    )
    if not storage.get("ready"):
        raise HTTPException(
            503,
            detail={
                "error": "SERVER_STORAGE_UNAVAILABLE",
                "message": "服务器存储空间不足或不可写，当前无法接收新的答题卡批阅任务。",
            },
        )
    os.makedirs(target, exist_ok=True)
    saved: list[dict] = []
    attempted_paths: list[str] = []
    total_bytes = 0
    completed = False
    try:
        for index, file in enumerate(files, 1):
            suffix = Path(file.filename or "").suffix.lower()
            if suffix not in _ALLOWED_EXTENSIONS:
                raise HTTPException(400, detail="仅支持 JPG、JPEG、PNG 图片")
            remaining_bytes = total_byte_budget - total_bytes
            if remaining_bytes <= 0:
                raise HTTPException(413, detail="上传文件总大小超过限制")
            storage_suffix = (
                ocr_ready_storage_suffix(file.filename or "") or suffix
            )
            name = f"{prefix}_{index}_{uuid.uuid4().hex}{storage_suffix}"
            path = os.path.join(target, name)
            attempted_paths.append(path)
            byte_limit = min(MAX_UPLOAD_FILE_SIZE, remaining_bytes)
            size = await stream_to_disk_capped(file, path, byte_limit)
            if size < 0:
                _remove_upload_path(path)
                if remaining_bytes < MAX_UPLOAD_FILE_SIZE:
                    raise HTTPException(413, detail="上传文件总大小超过限制")
                raise HTTPException(413, detail="单个上传文件大小超过限制")
            if size == 0:
                raise HTTPException(400, detail="上传图片为空")
            validation = await _run_answer_sheet_image_work(
                validate_upload_path,
                file.filename or f"upload{suffix}",
                path,
            )
            if not validation.get("safe"):
                raise HTTPException(
                    400,
                    detail=validation.get("reason") or "图片校验失败",
                )
            digest = await _run_answer_sheet_image_work(sha256_file, path)
            saved.append({
                "page_id": f"page_{index}",
                "index": index - 1,
                "path": path,
                "sha256": digest,
                "filename": file.filename or name,
            })
            total_bytes += size
        completed = True
    finally:
        if not completed:
            for path in attempted_paths:
                _remove_upload_path(path)
    return saved


def _session_asset_path(root: Path, value: object, *, relative: bool) -> Path | None:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    try:
        candidate = (root / value if relative else Path(value)).resolve(strict=False)
        candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate if candidate != root else None


def _require_raw_assets_available(session: AnswerSheetSession) -> None:
    lifecycle = session.asset_lifecycle if isinstance(session.asset_lifecycle, dict) else {}
    if lifecycle.get("status") == "expired":
        raise HTTPException(410, detail=_RAW_PAPER_EXPIRED_MESSAGE)


def _layout_asset_paths(
    session_dir: str,
    pages: list[dict],
    artifacts: dict[str, dict],
) -> set[Path]:
    from services.answer_sheet_page_registration import registered_image_path

    root = Path(session_dir).resolve()
    paths: set[Path] = set()
    for item in [*pages, *artifacts.values()]:
        if not isinstance(item, dict):
            continue
        for field in ("path", "working_path", "orientation_working_path"):
            candidate = _session_asset_path(root, item.get(field), relative=False)
            if candidate is not None:
                paths.add(candidate)
        template_image = _session_asset_path(
            root,
            item.get("template_image"),
            relative=True,
        )
        if template_image is not None:
            paths.add(template_image)
        canonicalization = item.get("canonicalization")
        if isinstance(canonicalization, dict):
            source_image = _session_asset_path(
                root,
                canonicalization.get("source_image"),
                relative=True,
            )
            if source_image is not None:
                paths.add(source_image)
        registered = item.get("registered_image")
        if isinstance(registered, dict) and registered.get("file"):
            try:
                paths.add(registered_image_path(root, registered))
            except ValueError:
                pass
    return paths


def _remove_layout_assets(
    session_dir: str,
    pages: list[dict],
    artifacts: dict[str, dict],
    *,
    preserve_pages: list[dict] | None = None,
    preserve_artifacts: dict[str, dict] | None = None,
) -> None:
    root = Path(session_dir).resolve()
    paths = _layout_asset_paths(session_dir, pages, artifacts)
    preserved = _layout_asset_paths(
        session_dir,
        preserve_pages or [],
        preserve_artifacts or {},
    )
    removed_parents: set[Path] = set()
    for path in paths - preserved:
        try:
            path.unlink(missing_ok=True)
            removed_parents.add(path.parent)
        except OSError:
            continue
    for parent in sorted(removed_parents, key=lambda item: len(item.parts), reverse=True):
        while parent != root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


@router.post("/sessions")
@_requires_answer_sheet_writes
async def create_session(
    req: SessionCreateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    context = make_teaching_context(require_teacher_stage(user))
    await _require_answer_sheet_chain(user)
    class_id = _inherited_class_id(db, user)
    paper_registry = get_paper_template_registry()
    try:
        paper = paper_registry.load(req.paper_template_code, user.id)
    except PaperTemplateNotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    if paper["content"].get("teaching_context") != context:
        raise HTTPException(409, detail="试卷模板学段与当前学段不一致，请重新登记试卷模板")
    answer_sheet_code = None
    if req.answer_sheet_template_code:
        try:
            answer_sheet_code = normalize_template_code(req.answer_sheet_template_code)
        except ValueError as exc:
            raise HTTPException(409, detail=str(exc)) from exc

    session = AnswerSheetSession(
        uuid.uuid4().hex[:16], UPLOAD_DIR, teacher_id=user.id, class_id=class_id,
        teaching_context=context,
    )
    session.batch_contract_version = BATCH_CONTRACT_VERSION
    session.bind_paper_template(
        paper["template_code"],
        PaperTemplateRegistry.questions(paper),
        paper["answer_spec_hash"],
    )
    if answer_sheet_code is None:
        session.save()
    else:
        registry = get_answer_sheet_template_registry()
        try:
            with _batch_label_binding(session, answer_sheet_code, user.id):
                registry.apply_to_session(session, answer_sheet_code)
            registry.remember(user.id, answer_sheet_code)
        except BaseException as exc:
            shutil.rmtree(session.session_dir, ignore_errors=True)
            if isinstance(exc, AnswerSheetTemplateNotFound):
                raise HTTPException(404, detail=str(exc)) from exc
            if isinstance(exc, ValueError):
                raise HTTPException(409, detail=str(exc)) from exc
            raise
    paper_registry.remember(user.id, paper["template_code"])
    return session.to_dict(public=True)


@router.get("/sessions")
def list_sessions(user: User = Depends(_require_user)):
    """List the current teacher's batches by their template-code storage label."""
    return {"batches": list_teacher_batches(UPLOAD_DIR, user.id)}


@router.get("/paper-templates")
def list_paper_templates(user: User = Depends(_require_user)):
    """Return the teacher's frozen paper templates (private to the owner)."""
    return {"templates": get_paper_template_registry().list_templates(user.id)}


@router.get("/paper-templates/sources")
def list_paper_template_sources(user: User = Depends(_require_user)):
    """Confirmed detailed-grading templates that can be frozen for reuse."""
    return {"sources": list_detailed_template_sources(UPLOAD_DIR, user.id)}


@router.post("/paper-templates")
@_requires_answer_sheet_writes
def register_paper_template(
    req: PaperTemplateRegisterRequest,
    user: User = Depends(_require_user),
):
    """Freeze a teacher-confirmed detailed-grading template for reuse."""
    from services.template_session import TemplateSession

    try:
        source = TemplateSession.load_meta(req.template_id, UPLOAD_DIR)
    except (OSError, ValueError) as exc:
        raise HTTPException(404, detail="详细批改模板不存在或不属于当前教师") from exc
    if source.teacher_id != user.id or not os.path.isfile(source.meta_path):
        raise HTTPException(404, detail="详细批改模板不存在或不属于当前教师")
    try:
        template = get_paper_template_registry().register(
            source, teacher_id=user.id, alias=req.alias,
        )
    except PaperTemplateNotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return {"template": template}


@router.get("/paper-templates/{template_code}")
def get_paper_template(template_code: str, user: User = Depends(_require_user)):
    """Read-only view of a frozen paper template for teacher review."""
    registry = get_paper_template_registry()
    try:
        manifest = registry.load(template_code, user.id)
    except PaperTemplateNotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    entry = next(
        (
            item for item in registry.list_templates(user.id)
            if item["template_code"] == manifest["template_code"]
        ),
        None,
    )
    return {
        "template": entry or registry.summary(manifest),
        "questions": PaperTemplateRegistry.questions(manifest),
    }


@router.put("/paper-templates/{template_code}/alias")
@_requires_answer_sheet_writes
def set_paper_template_alias(
    template_code: str,
    req: TemplateAliasRequest,
    user: User = Depends(_require_user),
):
    try:
        template = get_paper_template_registry().set_alias(user.id, template_code, req.alias)
    except PaperTemplateNotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return {"template": template}


@router.get("/sessions/{session_id}")
def get_session(session_id: str, user: User = Depends(_require_user)):
    return _load_owned(session_id, user.id).to_dict(public=True)


@router.get("/templates/history")
def template_code_history(user: User = Depends(_require_user)):
    """Return only the current teacher's recently used shared template codes."""

    registry = get_answer_sheet_template_registry()
    return {"templates": registry.history(user.id)}


@router.delete("/sessions/{session_id}")
@_serialized_session_mutation
def delete_session(session_id: str, user: User = Depends(_require_user)):
    session = _load_owned(session_id, user.id)
    try:
        session.delete()
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/sessions/{session_id}/source/upload")
@_requires_answer_sheet_writes
@_serialized_session_mutation
async def upload_source(
    session_id: str,
    source_files: list[UploadFile] = File(...),
    answer_files: list[UploadFile] | None = File(None),
    answer_source: str = Form("embedded"),
    user: User = Depends(_require_user),
):
    if len(source_files) > MAX_TEMPLATE_PAGES:
        raise HTTPException(413, detail=f"原始试卷最多上传 {MAX_TEMPLATE_PAGES} 页")
    if answer_files and len(answer_files) > MAX_TEMPLATE_PAGES:
        raise HTTPException(413, detail=f"答案文件最多上传 {MAX_TEMPLATE_PAGES} 页")
    if answer_source not in {"embedded", "separate", "ai_generated"}:
        raise HTTPException(400, detail="答案来源无效")
    if answer_source == "separate" and not answer_files:
        raise HTTPException(400, detail="分离答案模式必须上传答案文件")
    session = _load_owned(session_id, user.id)
    if session.status not in {"draft", "failed"}:
        raise HTTPException(409, detail="当前阶段不能重新上传原始试卷")
    previous_source_pages = list(session.source_exam_pages)
    previous_answer_pages = list(session.answer_pages)
    previous_state = (
        session.answer_source,
        session.source_recognition_generation,
        list(session.source_question_drafts),
        session.status,
        session.error,
    )
    source_dir = os.path.join(
        session.session_dir,
        f"source_{uuid.uuid4().hex}",
    )
    try:
        source_pages = await _save_images(source_files, source_dir, "source")
        remaining_budget = MAX_UPLOAD_TOTAL_SIZE - sum(
            os.path.getsize(page["path"])
            for page in source_pages
        )
        saved_answer_pages = (
            await _save_images(
                answer_files,
                source_dir,
                "answer",
                total_byte_budget=remaining_budget,
            )
            if answer_files
            else []
        )
        session.source_exam_pages = source_pages
        session.answer_pages = saved_answer_pages
        session.answer_source = answer_source
        session.source_recognition_generation = 0
        session.source_question_drafts = []
        session.status = "source_uploaded"
        session.error = None
        session.save()
    except BaseException:
        shutil.rmtree(source_dir, ignore_errors=True)
        session.source_exam_pages = previous_source_pages
        session.answer_pages = previous_answer_pages
        (
            session.answer_source,
            session.source_recognition_generation,
            session.source_question_drafts,
            session.status,
            session.error,
        ) = previous_state
        raise
    _remove_layout_assets(
        session.session_dir,
        [*previous_source_pages, *previous_answer_pages],
        {},
        preserve_pages=[*source_pages, *saved_answer_pages],
    )
    return session.to_dict(public=True)


def _source_recognition_is_resume(session: AnswerSheetSession) -> bool:
    if session.status == "failed" and not session.source_question_drafts:
        return True
    if session.status != "source_uploaded":
        return False
    if session.error:
        return True
    return any(
        isinstance(page.get("recognition_checkpoint"), dict)
        for page in [*session.source_exam_pages, *session.answer_pages]
    )


def _source_recognition_phase(session: AnswerSheetSession):
    return set_vision_phase_key(
        f"answer-sheet-source:{session.source_recognition_generation}",
        retry_scope_key="answer-sheet-source",
    )


@router.post("/sessions/{session_id}/source/recognize")
@_requires_answer_sheet_writes
@_serialized_session_mutation
async def recognize_source(session_id: str, user: User = Depends(_require_user)):
    """Recognize source-exam semantics; blank answer-sheet pixels are never used."""
    session = _load_owned(session_id, user.id)
    if session.status not in {"source_uploaded", "failed"} or not session.source_exam_pages:
        raise HTTPException(409, detail="请先上传原始试卷")
    await _require_answer_sheet_chain(user)
    if _source_recognition_is_resume(session):
        session.source_recognition_generation += 1
        session.save()
    vision_phase_token = _source_recognition_phase(session)
    try:
        drafts: list[dict] = []
        if session.answer_source == "embedded":
            for page in session.source_exam_pages:
                result = await recognize_template_with_answers(page["path"])
                if result.get("error") or not result.get("questions"):
                    raise ValueError(result.get("error") or "原始试卷未识别到题目")
                drafts.extend(result["questions"])
        else:
            for page in session.source_exam_pages:
                result = await recognize_template_blank(page["path"])
                if result.get("error") or not result.get("questions"):
                    raise ValueError(result.get("error") or "原始试卷未识别到题目")
                drafts.extend(result["questions"])
            if session.answer_source == "ai_generated":
                generated = await generate_template_answers_batch(drafts)
                if generated.get("error"):
                    raise ValueError(generated["error"])
                for question in drafts:
                    number = str(question.get("question_number") or question.get("question_no") or "").strip()
                    answer = generated["answers"].get(number)
                    if not answer:
                        raise ValueError(f"第{number}题缺少AI候选答案")
                    question["standard_answer"] = answer["standard_answer"]
                    question["solution_steps"] = answer["model_answer"]
            else:
                answer_drafts: list[dict] = []
                for page in session.answer_pages:
                    result = await recognize_template_with_answers(page["path"])
                    if result.get("error") or not result.get("questions"):
                        raise ValueError(result.get("error") or "答案文件未识别到答案")
                    answer_drafts.extend(result["questions"])
                by_number = {
                    str(item.get("question_number") or item.get("question_no") or "").strip(): item
                    for item in answer_drafts
                }
                for question in drafts:
                    number = str(question.get("question_number") or question.get("question_no") or "").strip()
                    answer = by_number.get(number)
                    if not answer or not str(answer.get("standard_answer") or "").strip():
                        raise ValueError(f"答案文件缺少第{number}题")
                    question["standard_answer"] = answer["standard_answer"]
                    question["solution_steps"] = answer.get("solution_steps") or []
        numbers = [str(item.get("question_number") or item.get("question_no") or "").strip() for item in drafts]
        if not numbers or any(not number for number in numbers) or len(numbers) != len(set(numbers)):
            raise ValueError("原始试卷题号为空或跨页重复，请教师检查")
        session.source_question_drafts = drafts
        session.status = "source_recognized"
        session.error = None
        session.save()
        return session.to_dict(public=True)
    except ValueError as exc:
        session.status = "failed"
        session.error = str(exc)
        session.save()
        raise HTTPException(422, detail=str(exc)) from exc
    finally:
        reset_vision_phase_key(vision_phase_token)


def _source_page_checkpoint(page: dict, mode: str, teaching_context=None) -> list[dict] | None:
    checkpoint = page.get("recognition_checkpoint")
    if not isinstance(checkpoint, dict):
        return None
    if checkpoint.get("mode") != mode:
        return None
    if checkpoint.get("sha256") != page.get("sha256"):
        return None
    if (checkpoint.get("teaching_context") or {}) != (teaching_context or {}):
        return None
    questions = checkpoint.get("questions")
    return list(questions) if isinstance(questions, list) and questions else None


async def _recognize_source_stream_events(
    session: AnswerSheetSession,
) -> AsyncIterator[str]:
    source_mode = "embedded" if session.answer_source == "embedded" else "blank"
    work: list[tuple[dict, str, str]] = [
        (page, source_mode, "原始试卷") for page in session.source_exam_pages
    ]
    if session.answer_source == "separate":
        work.extend((page, "embedded", "答案文件") for page in session.answer_pages)
    total = len(work) + (1 if session.answer_source == "ai_generated" else 0)
    completed = 0

    yield _answer_sheet_sse("progress", {
        "message": "正在恢复已保存进度" if any(
            _source_page_checkpoint(page, mode, session.teaching_context) is not None
            for page, mode, _label in work
        ) else "正在识别原始试卷",
        "current": 0,
        "total": total,
    })

    for page, mode, label in work:
        questions = _source_page_checkpoint(page, mode, session.teaching_context)
        if questions is None:
            recognize = (
                recognize_template_with_answers
                if mode == "embedded"
                else recognize_template_blank
            )
            result = None
            async for event, payload in _with_answer_sheet_heartbeats(
                recognize(page["path"]),
                message=f"正在识别{label}第 {completed + 1} 项",
                current=completed,
                total=total,
            ):
                if event == "progress":
                    yield _answer_sheet_sse("progress", payload)
                else:
                    result = payload
            if not isinstance(result, dict) or result.get("error") or not result.get("questions"):
                message = result.get("error") if isinstance(result, dict) else None
                raise ValueError(message or f"{label}未识别到题目")
            questions = list(result["questions"])
            page["recognition_checkpoint"] = {
                "mode": mode,
                "sha256": page.get("sha256"),
                "questions": questions,
                "teaching_context": session.teaching_context,
            }
            session.status = "source_uploaded"
            session.error = None
            await asyncio.to_thread(session.save)
        completed += 1
        yield _answer_sheet_sse("progress", {
            "message": f"{label}识别进度已保存",
            "current": completed,
            "total": total,
            "checkpoint": True,
        })

    drafts = [
        question
        for page in session.source_exam_pages
        for question in (_source_page_checkpoint(page, source_mode, session.teaching_context) or [])
    ]
    if session.answer_source == "ai_generated":
        generated = None
        async for event, payload in _with_answer_sheet_heartbeats(
            generate_template_answers_batch(drafts),
            message="正在生成参考答案",
            current=completed,
            total=total,
        ):
            if event == "progress":
                yield _answer_sheet_sse("progress", payload)
            else:
                generated = payload
        if not isinstance(generated, dict) or generated.get("error"):
            message = generated.get("error") if isinstance(generated, dict) else None
            raise ValueError(message or "AI候选答案生成失败")
        for question in drafts:
            number = str(
                question.get("question_number") or question.get("question_no") or ""
            ).strip()
            answer = generated.get("answers", {}).get(number)
            if not answer:
                raise ValueError(f"第{number}题缺少AI候选答案")
            question["standard_answer"] = answer["standard_answer"]
            question["solution_steps"] = answer["model_answer"]
        completed += 1
    elif session.answer_source == "separate":
        answer_drafts = [
            question
            for page in session.answer_pages
            for question in (_source_page_checkpoint(page, "embedded", session.teaching_context) or [])
        ]
        by_number = {
            str(item.get("question_number") or item.get("question_no") or "").strip(): item
            for item in answer_drafts
        }
        for question in drafts:
            number = str(
                question.get("question_number") or question.get("question_no") or ""
            ).strip()
            answer = by_number.get(number)
            if not answer or not str(answer.get("standard_answer") or "").strip():
                raise ValueError(f"答案文件缺少第{number}题")
            question["standard_answer"] = answer["standard_answer"]
            question["solution_steps"] = answer.get("solution_steps") or []

    numbers = [
        str(item.get("question_number") or item.get("question_no") or "").strip()
        for item in drafts
    ]
    if not numbers or any(not number for number in numbers) or len(numbers) != len(set(numbers)):
        raise ValueError("原始试卷题号为空或跨页重复，请教师检查")

    session.source_question_drafts = drafts
    for page, _mode, _label in work:
        page.pop("recognition_checkpoint", None)
    session.status = "source_recognized"
    session.error = None
    await asyncio.to_thread(session.save)
    yield _answer_sheet_sse("done", {
        "message": "原始试卷识别完成",
        "current": total,
        "total": total,
        "session": session.to_dict(public=True),
    })


@router.post("/sessions/{session_id}/source/recognize/stream")
@_requires_answer_sheet_writes
async def recognize_source_stream(
    session_id: str,
    user: User = Depends(_require_user),
):
    session = _load_owned(session_id, user.id)
    if session.status not in {"source_uploaded", "source_recognized", "failed"} or not session.source_exam_pages:
        raise HTTPException(409, detail="请先上传原始试卷")

    async def events():
        try:
            with _locked_owned(session_id, user.id) as locked:
                if locked.status == "source_recognized" and locked.source_question_drafts:
                    yield _answer_sheet_sse("done", {
                        "message": "原始试卷识别已完成",
                        "current": len(locked.source_exam_pages),
                        "total": len(locked.source_exam_pages),
                        "idempotent": True,
                        "session": locked.to_dict(public=True),
                    })
                    return
                await _require_answer_sheet_chain(user)
                if _source_recognition_is_resume(locked):
                    locked.source_recognition_generation += 1
                    await asyncio.to_thread(locked.save)
                vision_phase_token = _source_recognition_phase(locked)
                try:
                    try:
                        async for event in _recognize_source_stream_events(locked):
                            yield event
                    except (asyncio.CancelledError, GeneratorExit):
                        if locked.status != "source_recognized":
                            locked.status = "source_uploaded"
                            locked.error = "题目识别连接中断，已保存完成页；再次执行即可恢复。"
                            await asyncio.to_thread(locked.save)
                        raise
                    except ValueError as exc:
                        locked.status = "failed"
                        locked.error = str(exc)
                        await asyncio.to_thread(locked.save)
                        yield _answer_sheet_sse("error", {
                            "code": "TEMPLATE_OCR_FAILED",
                            "message": str(exc),
                        })
                    except Exception:
                        logger.exception("answer-sheet source recognition stream failed")
                        locked.status = "source_uploaded"
                        locked.error = "题目识别链路中断，已保存完成页；请稍后重试恢复。"
                        await asyncio.to_thread(locked.save)
                        yield _answer_sheet_sse("error", {
                            "code": "GRADING_CHAIN_FAILED",
                            "message": locked.error,
                        })
                finally:
                    reset_vision_phase_key(vision_phase_token)
        except HTTPException as exc:
            yield _answer_sheet_sse("error", {
                "code": "ANSWER_SHEET_OPERATION_BUSY" if exc.status_code == 409 else "GRADING_CHAIN_FAILED",
                "message": str(exc.detail),
            })

    return _answer_sheet_stream(stream_teaching(session.teaching_context, events()))


@router.post("/sessions/{session_id}/answers/confirm")
@_requires_answer_sheet_writes
@_serialized_session_mutation
def confirm_answers(
    session_id: str,
    req: AnswerConfirmRequest = Body(...),
    user: User = Depends(_require_user),
):
    session = _load_owned(session_id, user.id)
    try:
        session.confirm_answers(req.questions)
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return session.to_dict(public=True)


@router.post("/sessions/{session_id}/layout/upload")
@_requires_answer_sheet_writes
@_serialized_session_mutation
async def upload_layout(
    session_id: str,
    files: list[UploadFile] = File(...),
    canonical_files: list[UploadFile] | None = File(None),
    canonical_metadata: str | None = Form(None),
    user: User = Depends(_require_user),
):
    session = _load_owned(session_id, user.id)
    if session.status not in {"answers_confirmed", "layout_draft"}:
        raise HTTPException(409, detail="请先确认题目与答案")
    previous_pages = list(session.answer_sheet_pages)
    previous_pending_pages = list(session.pending_layout_pages)
    previous_artifacts = dict(session.layout_artifacts)
    has_active_layout = bool(previous_pages) and all(
        page.get("working_path") and isinstance(page.get("canonicalization"), dict)
        for page in previous_pages
    )
    has_canonical_files = canonical_files is not None
    has_canonical_metadata = canonical_metadata is not None
    if has_canonical_files != has_canonical_metadata:
        raise HTTPException(
            422,
            detail="canonical_files 与 canonical_metadata 必须同时提交",
        )
    if len(files) > MAX_TEMPLATE_PAGES:
        raise HTTPException(
            413,
            detail=f"空白答题卡最多上传 {MAX_TEMPLATE_PAGES} 页原图",
        )
    submitted_metadata = None
    if has_canonical_files:
        if len(canonical_files) != len(files):
            raise HTTPException(422, detail="标准模板候选数量必须与原图一致")
        if any(
            Path(upload.filename or "").suffix.lower() != ".png"
            for upload in canonical_files
        ):
            raise HTTPException(422, detail="标准模板候选必须为 PNG")
        try:
            submitted_metadata = parse_canonical_metadata(
                canonical_metadata,
                len(files),
            )
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
    layout_dir = os.path.join(session.session_dir, "layout")
    storage_namespace = f"layout_canonical_{uuid.uuid4().hex}"
    candidate_dir = os.path.join(
        session.session_dir,
        f"layout_candidates_{uuid.uuid4().hex}",
    )
    pages: list[dict] = []
    candidate_pages: list[dict] = []
    accelerator_states: list[dict] = []
    try:
        pages = await _save_images(files, layout_dir, "layout")
        if submitted_metadata is not None:
            candidate_pages = await _save_images(
                canonical_files,
                candidate_dir,
                "canonical",
                total_byte_budget=(
                    MAX_UPLOAD_TOTAL_SIZE
                    - sum(os.path.getsize(page["path"]) for page in pages)
                ),
            )
        for page_index, page in enumerate(pages):
            if submitted_metadata is not None:
                canonical = await _run_answer_sheet_image_work(
                    accept_canonical_template_candidate,
                    page["path"],
                    candidate_pages[page_index]["path"],
                    submitted_metadata[page_index],
                    session.session_dir,
                    page_index,
                    storage_namespace=storage_namespace,
                )
            else:
                accelerated = await try_accelerated_template_canonicalization(
                    teacher_id=user.id,
                    operation_id=(
                        f"answer-sheet:{session.session_id}:layout:"
                        f"{page_index}:{page['sha256'][:16]}"
                    ),
                    source_path=page["path"],
                    wait_seconds=0,
                )
                if accelerated.state != "ready":
                    checkpoint = {
                        "state": str(accelerated.state or "fault"),
                        "reason_code": str(accelerated.reason_code or ""),
                        "job_id": str(accelerated.job_id or ""),
                        "source_sha256": page["sha256"],
                        "storage_namespace": storage_namespace,
                    }
                    page["canonicalization_accelerator"] = checkpoint
                    accelerator_states.append(checkpoint)
                    continue
                adopted = await asyncio.to_thread(
                    adopt_accelerated_template_canonicalization,
                    accelerated,
                    teacher_id=user.id,
                    template_dir=session.session_dir,
                    page_index=page_index,
                    storage_namespace=storage_namespace,
                )
                canonical_path = str(adopted.canonical_path or "")
                canonicalization = dict(adopted.canonicalization or {})
            if submitted_metadata is not None:
                canonical_path = canonical.image_path
                canonicalization = canonical.canonicalization
            page["working_path"] = canonical_path
            page["working_sha256"] = canonicalization[
                "canonical_sha256"
            ]
            page["canonicalization"] = canonicalization
    except ValueError as exc:
        for page in pages:
            try:
                os.remove(page["path"])
            except OSError:
                pass
        shutil.rmtree(
            os.path.join(session.session_dir, storage_namespace),
            ignore_errors=True,
        )
        raise HTTPException(
            422,
            detail=(
                "客户端标准模板候选校验失败"
                if submitted_metadata is not None
                else "答题卡页面未能完整拉正，请使用边界完整、方向正确且清晰的图片重新上传"
            ),
        ) from exc
    except BaseException:
        for page in pages:
            try:
                os.remove(page["path"])
            except OSError:
                pass
        shutil.rmtree(
            os.path.join(session.session_dir, storage_namespace),
            ignore_errors=True,
        )
        raise
    finally:
        shutil.rmtree(candidate_dir, ignore_errors=True)
    try:
        if accelerator_states:
            rank = {"pending": 0, "unavailable": 1, "fault": 2, "failed": 2}
            worst = max(
                accelerator_states,
                key=lambda item: rank.get(str(item.get("state") or "fault"), 2),
            )
            raw_state = str(worst.get("state") or "fault")
            public_state = {
                "pending": "waiting_accelerator",
                "unavailable": "unavailable",
                "failed": "fault",
                "fault": "fault",
            }.get(raw_state, "fault")
            messages = {
                "waiting_accelerator": "空白答题卡已保存，正在等待客户端或机会式加速节点处理页面。",
                "unavailable": "空白答题卡已保存，但当前没有可用加速节点；服务器不会回退处理图像。",
                "fault": "空白答题卡已保存，但加速图像处理发生故障；服务器不会回退处理图像。",
            }
            session.asset_lifecycle["layout_acceleration"] = {
                "status": public_state,
                "reason_code": str(worst.get("reason_code") or ""),
                "page_count": len(pages),
                "ready_count": len(pages) - len(accelerator_states),
            }
            session.pending_layout_pages = pages
            if not has_active_layout:
                session.answer_sheet_pages = []
                session.answer_regions = []
                session.identity_regions = []
                session.layout_hash = ""
                session.layout_artifacts = {}
                session.status = "answers_confirmed"
            else:
                session.status = "layout_draft"
            session.error = messages[public_state]
        else:
            session.answer_sheet_pages = pages
            session.pending_layout_pages = []
            session.answer_regions = []
            session.identity_regions = []
            session.layout_hash = ""
            session.layout_artifacts = {}
            session.layout_localization = {}
            session.shared_template_code = ""
            session.shared_template_hash = ""
            session.asset_lifecycle.pop("layout_acceleration", None)
            session.status = "layout_draft"
            session.error = None
        session.save()
    except Exception:
        _remove_layout_assets(session.session_dir, pages, {})
        raise
    _remove_layout_assets(
        session.session_dir,
        previous_pending_pages,
        {},
        preserve_pages=pages,
    )
    if not accelerator_states:
        _remove_layout_assets(
            session.session_dir,
            previous_pages,
            previous_artifacts,
            preserve_pages=pages,
        )
    elif not has_active_layout:
        _remove_layout_assets(
            session.session_dir,
            previous_pages,
            previous_artifacts,
            preserve_pages=pages,
        )
    payload = session.to_dict(public=True)
    accelerator = session.asset_lifecycle.get("layout_acceleration")
    if isinstance(accelerator, dict):
        payload["accelerator"] = {
            "status": accelerator.get("status"),
            "reason_code": accelerator.get("reason_code"),
            "message": session.error,
        }
        return JSONResponse(status_code=202, content=payload)
    return payload


@router.post("/sessions/{session_id}/layout/process")
@_requires_answer_sheet_writes
@_serialized_session_mutation
async def process_layout_acceleration(
    session_id: str,
    user: User = Depends(_require_user),
):
    """Resume durable canonicalization jobs without re-uploading the pages."""
    session = _load_owned(session_id, user.id)
    if session.status not in {"answers_confirmed", "layout_draft"}:
        raise HTTPException(409, detail="当前答题卡版面状态不能继续处理")
    active_pages = list(session.answer_sheet_pages)
    pending_pages = list(session.pending_layout_pages)
    legacy_pending = (
        not pending_pages
        and any(
            isinstance(page.get("canonicalization_accelerator"), dict)
            for page in active_pages
        )
    )
    pages = pending_pages or (active_pages if legacy_pending else [])
    if not pages:
        raise HTTPException(409, detail="请先上传空白答题卡")

    remaining: list[dict] = []
    for page_index, page in enumerate(pages):
        if page.get("working_path") and isinstance(page.get("canonicalization"), dict):
            continue
        checkpoint = page.get("canonicalization_accelerator")
        if not isinstance(checkpoint, dict):
            remaining.append({
                "state": "fault",
                "reason_code": "canonicalization_checkpoint_missing",
            })
            continue
        if checkpoint.get("source_sha256") != page.get("sha256"):
            checkpoint.update(
                state="fault",
                reason_code="canonicalization_source_changed",
            )
            remaining.append(checkpoint)
            continue
        job_id = str(checkpoint.get("job_id") or "")
        if not job_id:
            checkpoint.update(
                state="unavailable",
                reason_code="canonicalization_job_missing",
            )
            remaining.append(checkpoint)
            continue
        accelerated = await resume_accelerated_template_canonicalization(
            teacher_id=user.id,
            job_id=job_id,
        )
        if accelerated.state != "ready":
            checkpoint.update(
                state=str(accelerated.state or "fault"),
                reason_code=str(accelerated.reason_code or ""),
            )
            remaining.append(checkpoint)
            continue
        try:
            adopted = await asyncio.to_thread(
                adopt_accelerated_template_canonicalization,
                accelerated,
                teacher_id=user.id,
                template_dir=session.session_dir,
                page_index=page_index,
                storage_namespace=str(
                    checkpoint.get("storage_namespace") or "layout_canonical"
                ),
            )
        except (OSError, PermissionError, ValueError) as exc:
            logger.warning(
                "answer-sheet canonicalization adoption failed page=%s: %s",
                page_index,
                exc,
            )
            checkpoint.update(
                state="fault",
                reason_code="canonicalization_adoption_failed",
            )
            remaining.append(checkpoint)
            continue
        page["working_path"] = str(adopted.canonical_path or "")
        page["canonicalization"] = dict(adopted.canonicalization or {})
        page["working_sha256"] = page["canonicalization"].get(
            "canonical_sha256", ""
        )
        page.pop("canonicalization_accelerator", None)

    if remaining:
        rank = {"pending": 0, "unavailable": 1, "fault": 2, "failed": 2}
        worst = max(
            remaining,
            key=lambda item: rank.get(str(item.get("state") or "fault"), 2),
        )
        public_state = {
            "pending": "waiting_accelerator",
            "unavailable": "unavailable",
            "failed": "fault",
            "fault": "fault",
        }.get(str(worst.get("state") or "fault"), "fault")
        message = {
            "waiting_accelerator": "空白答题卡已保存，正在等待客户端或机会式加速节点处理页面。",
            "unavailable": "当前没有可用加速节点；已保存页面，服务器不会回退处理图像。",
            "fault": "加速图像处理发生故障；已保存页面，服务器不会回退处理图像。",
        }[public_state]
        session.asset_lifecycle["layout_acceleration"] = {
            "status": public_state,
            "reason_code": str(worst.get("reason_code") or ""),
            "page_count": len(pages),
            "ready_count": len(pages) - len(remaining),
        }
        if pending_pages:
            session.pending_layout_pages = pages
            session.status = "layout_draft" if active_pages else "answers_confirmed"
        else:
            session.answer_sheet_pages = pages
            session.status = "answers_confirmed"
        session.error = message
    else:
        previous_pages = active_pages if pending_pages else []
        previous_artifacts = dict(session.layout_artifacts) if pending_pages else {}
        session.answer_sheet_pages = pages
        session.pending_layout_pages = []
        session.answer_regions = []
        session.identity_regions = []
        session.layout_hash = ""
        session.layout_artifacts = {}
        session.layout_localization = {}
        session.shared_template_code = ""
        session.shared_template_hash = ""
        session.asset_lifecycle.pop("layout_acceleration", None)
        session.status = "layout_draft"
        session.error = None
    await asyncio.to_thread(session.save)
    if not remaining and previous_pages:
        _remove_layout_assets(
            session.session_dir,
            previous_pages,
            previous_artifacts,
            preserve_pages=pages,
        )
    payload = session.to_dict(public=True)
    accelerator = session.asset_lifecycle.get("layout_acceleration")
    if isinstance(accelerator, dict):
        payload["accelerator"] = {
            "status": accelerator.get("status"),
            "reason_code": accelerator.get("reason_code"),
            "message": session.error,
        }
        return JSONResponse(status_code=202, content=payload)
    return payload


@router.get("/sessions/{session_id}/layout/pages/{page_id}/image")
def layout_image(session_id: str, page_id: str, user: User = Depends(_require_user)):
    session = _load_owned(session_id, user.id)
    _require_raw_assets_available(session)
    page = next((item for item in session.answer_sheet_pages if item.get("page_id") == page_id), None)
    active_path = str((page or {}).get("working_path") or (page or {}).get("path") or "")
    if not page or not os.path.isfile(active_path):
        raise HTTPException(404, detail="答题卡页面不存在")
    return FileResponse(active_path)


@router.post("/sessions/{session_id}/layout/locate")
@_requires_answer_sheet_writes
@_serialized_session_mutation
async def locate_layout_regions(
    session_id: str,
    user: User = Depends(_require_user),
):
    """Ask qwen3.8max/high for editable, never-authoritative region proposals."""

    session = _load_owned(session_id, user.id)
    if session.status != "layout_draft" or not session.answer_sheet_pages:
        raise HTTPException(409, detail="请先上传并完成空白答题卡页面矫正")
    try:
        localization = await locate_answer_sheet_template(
            session.answer_sheet_pages,
            session.answer_spec_snapshot,
            teacher_id=user.id,
        )
        specs = {
            str(item.get("question_no") or "").strip(): item
            for item in session.answer_spec_snapshot
        }
        answer_regions: list[dict] = []
        identity_regions: list[dict] = []
        grid_proposals: list[dict] = []
        for page in localization["pages"]:
            page_id = str(page["page_id"])
            for raw in page["answer_regions"]:
                question_no = str(raw["question_no"])
                spec = specs[question_no]
                question_type = str(spec.get("question_type") or "solve")
                if question_type == "choice" and choice_row_keys(spec) == [""]:
                    # The locator frames all option bubbles of a bubble-style
                    # choice question; that box is only a placement proposal.
                    # Small-question rows are left for the teacher to place.
                    grid_proposals.append({
                        "question_no": question_no,
                        "grid_bbox": raw["bbox"],
                        "confidence": raw.get("confidence"),
                    })
                answer_regions.append({
                    "region_id": f"answer_{len(answer_regions) + 1}",
                    "page_id": page_id,
                    "question_no": question_no,
                    "bbox": raw["bbox"],
                    "region_type": (
                        "choice_mark" if question_type == "choice" else question_type
                    ),
                    "reading_order": len(answer_regions) + 1,
                    "required": True,
                    "teacher_confirmed": False,
                    "choice_options": list(spec.get("choice_options") or ["A", "B", "C", "D"]),
                    "allow_multiple": (
                        question_type == "choice"
                        and str(spec.get("choice_mode") or "unknown") == "multiple"
                    ),
                })
            for raw in page["identity_regions"]:
                identity_regions.append({
                    "region_id": f"identity_{len(identity_regions) + 1}",
                    "page_id": page_id,
                    "bbox": raw["bbox"],
                    "identity_type": raw["identity_type"],
                    "teacher_confirmed": False,
                    "config": dict(raw.get("config") or {}),
                })
        summary = session.save_layout(answer_regions, identity_regions)
        session.layout_localization = localization
        if session.is_batch and grid_proposals:
            session.propose_bubble_grids(grid_proposals, proposer={
                "contract_version": localization.get("contract_version"),
                "provider_route": localization.get("provider_route"),
            })
            summary = session.layout_summary()
        session.save()
    except ValueError as exc:
        raise HTTPException(409, detail=f"答题卡粗定位结果需重新处理：{exc}") from exc
    except Exception as exc:
        logger.exception("qwen answer-sheet template localization failed")
        raise HTTPException(
            503,
            detail="qwen3.8max 暂时无法完成答题卡粗定位；已上传页面仍可手工绑定或稍后重试",
        ) from exc
    return {
        "session": session.to_dict(public=True),
        "summary": summary,
        "localization": localization,
    }


@router.post("/sessions/{session_id}/layout/templates/use")
@_requires_answer_sheet_writes
@_serialized_session_mutation
def use_layout_template_code(
    session_id: str,
    req: TemplateCodeRequest,
    user: User = Depends(_require_user),
):
    """Apply an immutable shared template without opening any model route."""

    session = _load_owned(session_id, user.id)
    previous_pages = list(session.answer_sheet_pages)
    previous_artifacts = dict(session.layout_artifacts)
    registry = get_answer_sheet_template_registry()
    try:
        code = normalize_template_code(req.template_code)
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    try:
        # The label is bound in memory first; applying the template persists
        # it together with the frozen layout in a single save.
        with _batch_label_binding(session, code, user.id):
            template = registry.apply_to_session(session, code)
        registry.remember(user.id, template["template_code"])
    except AnswerSheetTemplateNotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except AnswerSheetTemplateIncompatible as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    _remove_layout_assets(
        session.session_dir,
        previous_pages,
        previous_artifacts,
        preserve_pages=session.answer_sheet_pages,
        preserve_artifacts=session.layout_artifacts,
    )
    return {"session": session.to_dict(public=True), "template": template}


@router.post("/sessions/{session_id}/layout/templates/publish")
@_requires_answer_sheet_writes
@_serialized_session_mutation
def publish_layout_template_code(
    session_id: str,
    user: User = Depends(_require_user),
):
    """Publish the frozen teacher-confirmed layout and remember its code."""

    session = _load_owned(session_id, user.id)
    if session.status != "frozen":
        raise HTTPException(409, detail="请先确认并冻结答题卡版面")
    registry = get_answer_sheet_template_registry()
    try:
        template = registry.publish(session, creator_teacher_id=user.id)
        with _batch_label_binding(session, template["template_code"], user.id):
            session.shared_template_code = template["template_code"]
            session.shared_template_hash = str(template.pop("_fingerprint"))
            # The confirmed grids (and who confirmed them) now live in the
            # template; the batch keeps no copy of its own.
            session.bubble_grids = []
            session.freeze_layout()
        registry.remember(user.id, template["template_code"])
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return {"session": session.to_dict(public=True), "template": template}


@router.post("/sessions/{session_id}/layout/bubble-grids/confirm")
@_requires_answer_sheet_writes
@_serialized_session_mutation
def confirm_layout_bubble_grids(
    session_id: str,
    req: BubbleGridConfirmRequest,
    user: User = Depends(_require_user),
):
    """Teacher confirms or adjusts bubble-grid placements with explicit cells."""

    session = _load_owned(session_id, user.id)
    try:
        summary = session.confirm_bubble_grids(
            [item.model_dump() for item in req.grids], teacher_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return {"session": session.to_dict(public=True), "summary": summary}


@router.post("/sessions/{session_id}/layout/bubble-grids/discard")
@_requires_answer_sheet_writes
@_serialized_session_mutation
def discard_layout_bubble_grids(
    session_id: str,
    req: BubbleGridDiscardRequest,
    user: User = Depends(_require_user),
):
    """Teacher removes proposed or confirmed grid rows before freezing."""

    session = _load_owned(session_id, user.id)
    try:
        summary = session.discard_bubble_grids([item.model_dump() for item in req.keys])
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return {"session": session.to_dict(public=True), "summary": summary}


_STUDENT_UPLOAD_ACTIVE_STATES = {"uploading", "processing"}


def _valid_student_upload_id(value: object) -> bool:
    text = str(value or "")
    return len(text) == 32 and all(char in "0123456789abcdef" for char in text)


def _student_upload_pages(session: AnswerSheetSession) -> list[dict]:
    upload = session.student_upload
    pages = upload.get("pages") if isinstance(upload, dict) else None
    return pages if isinstance(pages, list) else []


def _student_upload_is_active(session: AnswerSheetSession) -> bool:
    upload = session.student_upload
    return (
        isinstance(upload, dict)
        and upload.get("status") in _STUDENT_UPLOAD_ACTIVE_STATES
        and _valid_student_upload_id(upload.get("upload_id"))
    )


def _require_no_student_upload(session: AnswerSheetSession) -> None:
    if _student_upload_is_active(session):
        raise HTTPException(
            409,
            detail="学生答题卡仍在上传或方向处理中，请先完成或放弃本次上传",
        )


def _require_student_upload(
    session: AnswerSheetSession,
    upload_id: str,
) -> dict:
    upload = session.student_upload
    if (
        not isinstance(upload, dict)
        or upload.get("upload_id") != upload_id
        or not _valid_student_upload_id(upload_id)
    ):
        raise HTTPException(404, detail="学生答题卡上传任务不存在")
    return upload


def _student_upload_checkpoint_complete(page: dict) -> bool:
    checkpoint = page.get("upload_checkpoint")
    return (
        isinstance(checkpoint, dict)
        and checkpoint.get("state") == "orientation_complete"
        and checkpoint.get("sha256") == page.get("sha256")
    )


@router.post("/sessions/{session_id}/students/upload/start")
@_requires_answer_sheet_writes
@_serialized_session_mutation
def start_student_upload(
    session_id: str,
    req: StudentUploadStartRequest = Body(default_factory=StudentUploadStartRequest),
    user: User = Depends(_require_user),
):
    session = _load_owned(session_id, user.id)
    if session.status not in {"frozen", "review"}:
        raise HTTPException(409, detail="请先冻结答题卡模板")
    if _student_upload_is_active(session) and not req.restart:
        session.student_upload.setdefault("pairing_mode", _ORDERED_DUPLEX_PAIRING)
        session.student_upload.setdefault("order_contract", "front_back_adjacent")
        session.save()
        return session.to_dict(public=True)

    previous_staged = _student_upload_pages(session)
    upload_id = uuid.uuid4().hex
    session.student_upload = {
        "upload_id": upload_id,
        "status": "uploading",
        "pages": [],
        "total_bytes": 0,
        "pairing_mode": req.pairing_mode,
        "order_contract": "front_back_adjacent",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "error": None,
    }
    session.error = None
    session.save()
    _remove_layout_assets(session.session_dir, previous_staged, {})
    return session.to_dict(public=True)


@router.delete("/sessions/{session_id}/students/upload/{upload_id}")
@_requires_answer_sheet_writes
@_serialized_session_mutation
def discard_student_upload(
    session_id: str,
    upload_id: str,
    user: User = Depends(_require_user),
):
    session = _load_owned(session_id, user.id)
    upload = _require_student_upload(session, upload_id)
    staged = _student_upload_pages(session)
    if upload.get("status") == "completed":
        staged = []
    session.student_upload = {}
    session.error = None
    session.save()
    _remove_layout_assets(session.session_dir, staged, {})
    return session.to_dict(public=True)


@router.post("/sessions/{session_id}/students/upload")
@_requires_answer_sheet_writes
@_serialized_session_mutation
async def upload_students(
    session_id: str,
    upload_id: str = Form(...),
    files: list[UploadFile] = File(...),
    source_path: str = Form(""),
    user: User = Depends(_require_user),
):
    session = _load_owned(session_id, user.id)
    if session.status not in {"frozen", "review"}:
        raise HTTPException(409, detail="请先冻结答题卡模板")
    upload = _require_student_upload(session, upload_id)
    try:
        # Display and identity metadata only, never a storage path.
        source_relative_path = normalize_source_path(source_path) if source_path else ""
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    if upload.get("status") not in _STUDENT_UPLOAD_ACTIVE_STATES:
        raise HTTPException(409, detail="学生答题卡上传任务已经结束")
    if len(files) != 1:
        raise HTTPException(413, detail="学生答题卡必须逐页上传")

    file = files[0]
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _ALLOWED_EXTENSIONS:
        raise HTTPException(400, detail="仅支持 JPG、JPEG、PNG 图片")

    written_path: str | None = None
    persisted = False
    try:
        pages = _student_upload_pages(session)
        if len(pages) >= MAX_UPLOAD_FILES:
            raise HTTPException(413, detail=f"学生答题卡最多上传 {MAX_UPLOAD_FILES} 页")
        total_bytes = sum(
            max(0, int(page.get("byte_size") or 0))
            for page in pages
            if isinstance(page, dict)
        )
        remaining_bytes = MAX_UPLOAD_TOTAL_SIZE - total_bytes
        if remaining_bytes <= 0:
            raise HTTPException(413, detail="上传文件总大小超过限制")
        byte_limit = min(MAX_UPLOAD_FILE_SIZE, remaining_bytes)
        target = os.path.join(session.session_dir, f"student_upload_{upload_id}")
        storage = await get_upload_storage_readiness_async(
            target,
            byte_limit,
            required_copies=1,
        )
        if not storage.get("ready"):
            raise HTTPException(
                503,
                detail={
                    "error": "SERVER_STORAGE_UNAVAILABLE",
                    "message": "服务器存储空间不足或不可写，当前无法接收新的答题卡批阅任务。",
                },
            )
        os.makedirs(target, exist_ok=True)
        storage_suffix = ocr_ready_storage_suffix(file.filename or "") or suffix
        name = (
            f"student_{len(pages) + 1}_{uuid.uuid4().hex}"
            f"{storage_suffix}"
        )
        written_path = os.path.join(target, name)
        size = await stream_to_disk_capped(file, written_path, byte_limit)
        if size < 0:
            if remaining_bytes < MAX_UPLOAD_FILE_SIZE:
                raise HTTPException(413, detail="上传文件总大小超过限制")
            raise HTTPException(413, detail="单个上传文件大小超过限制")
        if size == 0:
            raise HTTPException(400, detail="上传图片为空")
        validation = await _run_answer_sheet_image_work(
            validate_upload_path,
            file.filename or f"student{suffix}",
            written_path,
        )
        if not validation.get("safe"):
            raise HTTPException(
                400,
                detail=validation.get("reason") or "图片校验失败",
            )
        digest = await _run_answer_sheet_image_work(sha256_file, written_path)
        if any(page.get("sha256") == digest for page in pages):
            _remove_upload_path(written_path)
            written_path = None
            upload["status"] = "uploading"
            upload["error"] = None
            session.save()
            persisted = True
            return session.to_dict(public=True)
        physical_order = len(pages) + 1
        page = {
            "paper_id": f"student_{len(pages) + 1}",
            "index": len(pages),
            "physical_order": physical_order,
            "physical_pair_index": (physical_order + 1) // 2,
            "physical_side": "front" if physical_order % 2 else "back",
            "path": written_path,
            "sha256": digest,
            "byte_size": size,
            "filename": file.filename or name,
            "source_relative_path": source_relative_path,
            "orientation_status": "pending",
            "identity": {
                "status": "pending",
                "student_id": None,
                "candidates": [],
            },
            "answers": [],
            "review_reasons": [],
        }
        pages.append(page)
        upload["pages"] = pages
        upload["total_bytes"] = total_bytes + size
        upload["status"] = "uploading"
        upload["error"] = None
        try:
            session.save()
        except BaseException:
            pages.pop()
            raise
        persisted = True
        return session.to_dict(public=True)
    finally:
        if written_path is not None and not persisted:
            _remove_upload_path(written_path)
    return session.to_dict(public=True)


@router.post("/sessions/{session_id}/students/upload/{upload_id}/process/stream")
@_requires_answer_sheet_writes
async def process_student_upload_stream(
    session_id: str,
    upload_id: str,
    user: User = Depends(_require_user),
):
    session = _load_owned(session_id, user.id)
    upload = _require_student_upload(session, upload_id)
    if upload.get("status") == "completed" and session.student_pages:
        async def completed_events():
            yield _answer_sheet_sse("done", {
                "message": "学生答题卡上传处理已完成",
                "current": len(session.student_pages),
                "total": len(session.student_pages),
                "idempotent": True,
                "session": session.to_dict(public=True),
            })

        return _answer_sheet_stream(completed_events())
    try:
        await _require_answer_sheet_chain(user)
    except HTTPException as exc:
        unavailable_message = str(exc.detail)

        async def unavailable_events():
            yield _answer_sheet_sse("error", {
                "code": "GRADING_CHAIN_FAILED",
                "message": unavailable_message,
            })

        return _answer_sheet_stream(unavailable_events())
    if session.status not in {"frozen", "review"}:
        raise HTTPException(409, detail="请先冻结答题卡模板")
    if not _student_upload_pages(session):
        raise HTTPException(409, detail="请先上传学生答题卡")

    async def events():
        try:
            with _locked_owned(session_id, user.id) as locked:
                locked_upload = _require_student_upload(locked, upload_id)
                pages = _student_upload_pages(locked)
                if locked_upload.get("status") == "completed" and locked.student_pages:
                    yield _answer_sheet_sse("done", {
                        "message": "学生答题卡上传处理已完成",
                        "current": len(locked.student_pages),
                        "total": len(locked.student_pages),
                        "idempotent": True,
                        "session": locked.to_dict(public=True),
                    })
                    return
                if locked.status not in {"frozen", "review"} or not pages:
                    raise HTTPException(409, detail="学生答题卡上传状态已经变化，请刷新后重试")
                total = len(pages)
                completed = sum(
                    1 for page in pages if _student_upload_checkpoint_complete(page)
                )
                locked_upload["status"] = "processing"
                locked_upload["error"] = None
                locked.error = None
                await asyncio.to_thread(locked.save)
                yield _answer_sheet_sse("progress", {
                    "message": "正在恢复学生答题卡方向处理" if completed else "正在检查学生答题卡方向",
                    "current": completed,
                    "total": total,
                })
                finalized = False
                # Template-code batches register every page on the server
                # right here; recognition later reads only that image.
                register_pages = locked.is_batch
                registration_matcher: dict = {}
                try:
                    for page in pages:
                        oriented = _student_upload_checkpoint_complete(page)
                        if oriented and (
                            not register_pages or _batch_page_registration_current(locked, page)
                        ):
                            continue
                        if not oriented:
                            orientation = None
                            async for event, payload in _with_answer_sheet_heartbeats(
                                _run_answer_sheet_image_work(
                                    _accelerator_managed_orientation,
                                    page["path"],
                                ),
                                message=f"正在检查 {page.get('filename') or page.get('paper_id')} 的方向",
                                current=completed,
                                total=total,
                            ):
                                if event == "progress":
                                    yield _answer_sheet_sse("progress", payload)
                                else:
                                    orientation = payload
                            if not isinstance(orientation, dict):
                                raise RuntimeError("student orientation task ended without a result")
                            page.update(orientation)
                            page["orientation_source_sha256"] = page.get("sha256")
                            page["orientation_working_sha256"] = page.get("sha256")
                            page["upload_checkpoint"] = {
                                "state": "orientation_complete",
                                "sha256": page.get("sha256"),
                            }
                            completed += 1
                        superseded = None
                        if register_pages:
                            registration = None
                            async for event, payload in _with_answer_sheet_heartbeats(
                                _register_batch_page(locked, page, registration_matcher),
                                message=f"正在配准 {page.get('filename') or page.get('paper_id')}",
                                current=completed,
                                total=total,
                            ):
                                if event == "progress":
                                    yield _answer_sheet_sse("progress", payload)
                                else:
                                    registration = payload
                            if not isinstance(registration, dict):
                                raise RuntimeError("student page registration ended without a result")
                            superseded = page.get("registered_image")
                            page["registered_image"] = registration
                        await asyncio.to_thread(locked.save)
                        _remove_superseded_registration(locked, superseded, page.get("registered_image"))
                        yield _answer_sheet_sse("progress", {
                            "message": "当前页方向进度已保存",
                            "current": completed,
                            "total": total,
                            "checkpoint": True,
                            "paper_id": page.get("paper_id"),
                        })

                    previous_pages = list(locked.student_pages)
                    for page in pages:
                        page.pop("upload_checkpoint", None)
                    total_bytes = sum(
                        max(0, int(page.get("byte_size") or 0))
                        for page in pages
                        if isinstance(page, dict)
                    )
                    locked.student_pages = pages
                    locked.invalidate_grading_drafts()
                    locked.asset_lifecycle.pop(_GRADING_DRAFT_CHECKPOINT_KEY, None)
                    locked.status = "recognizing"
                    locked.error = None
                    locked.student_upload = {
                        "upload_id": upload_id,
                        "status": "completed",
                        "pairing_mode": str(
                            locked_upload.get("pairing_mode") or _ORDERED_DUPLEX_PAIRING
                        ),
                        "order_contract": "front_back_adjacent",
                        "uploaded_count": total,
                        "processed_count": total,
                        "total_bytes": total_bytes,
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "error": None,
                    }
                    await asyncio.to_thread(locked.save)
                    finalized = True
                    _remove_layout_assets(
                        locked.session_dir,
                        previous_pages,
                        {},
                        preserve_pages=pages,
                    )
                    yield _answer_sheet_sse("done", {
                        "message": "学生答题卡上传处理完成",
                        "current": total,
                        "total": total,
                        "session": locked.to_dict(public=True),
                    })
                except (asyncio.CancelledError, GeneratorExit):
                    if finalized:
                        raise
                    locked_upload["status"] = "processing"
                    locked_upload["error"] = "方向处理连接中断，已保存完成页；再次执行即可恢复。"
                    locked.error = locked_upload["error"]
                    await asyncio.to_thread(locked.save)
                    raise
                except Exception:
                    if finalized:
                        raise
                    logger.exception("answer-sheet student upload processing failed")
                    locked_upload["status"] = "processing"
                    locked_upload["error"] = "学生答题卡方向处理链路中断，已保存完成页；请稍后重试恢复。"
                    locked.error = locked_upload["error"]
                    await asyncio.to_thread(locked.save)
                    yield _answer_sheet_sse("error", {
                        "code": "GRADING_CHAIN_FAILED",
                        "message": locked.error,
                    })
        except HTTPException as exc:
            yield _answer_sheet_sse("error", {
                "code": "ANSWER_SHEET_OPERATION_BUSY" if exc.status_code == 409 else "GRADING_CHAIN_FAILED",
                "message": str(exc.detail),
            })

    return _answer_sheet_stream(stream_teaching(session.teaching_context, events()))


@router.get("/sessions/{session_id}/students/{paper_id}/image")
def student_page_image(
    session_id: str,
    paper_id: str,
    user: User = Depends(_require_user),
):
    """Return one immutable, teacher-owned student answer-sheet page."""
    session = _load_owned(session_id, user.id)
    _require_raw_assets_available(session)
    page = next(
        (item for item in session.student_pages if item.get("paper_id") == paper_id),
        None,
    )
    root = Path(session.session_dir).resolve()
    image_path = _session_asset_path(
        root,
        (page or {}).get("path"),
        relative=False,
    )
    if not page or image_path is None or not image_path.is_file():
        raise HTTPException(404, detail="学生答题卡页面不存在")
    return FileResponse(
        image_path,
        headers={"Cache-Control": "private, no-store"},
    )


_EVIDENCE_MEDIA_TYPES = {
    ".png": "image/png",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
}


@router.get("/sessions/{session_id}/students/{paper_id}/evidence")
def student_page_evidence_index(
    session_id: str,
    paper_id: str,
    user: User = Depends(_require_user),
):
    """List transcription-evidence manifests recorded for one student page.

    Teacher-owned read-only view: only manifests already attached to the
    page's recognized answers are exposed; file bytes stay behind the
    integrity-checked route below.
    """

    session = _load_owned(session_id, user.id)
    _require_raw_assets_available(session)
    page = next(
        (item for item in session.student_pages if item.get("paper_id") == paper_id),
        None,
    )
    if not page:
        raise HTTPException(404, detail="学生答题卡页面不存在")
    grouped: dict[str, dict] = {}
    for answer in page.get("answers") or []:
        transcription = answer.get("transcription") if isinstance(answer.get("transcription"), dict) else None
        evidence = (transcription or {}).get("evidence")
        directory = evidence.get("directory") if isinstance(evidence, dict) else None
        if not isinstance(directory, str) or not directory:
            continue
        entry = grouped.setdefault(
            directory,
            {
                "directory": directory,
                "question_nos": [],
                "files": [
                    {"file": item.get("file"), "kind": item.get("kind"), "bytes": item.get("bytes")}
                    for item in evidence.get("files") or []
                    if isinstance(item, dict) and item.get("file")
                ],
            },
        )
        question_no = str(answer.get("question_no", ""))
        if question_no and question_no not in entry["question_nos"]:
            entry["question_nos"].append(question_no)
    return {
        "session_id": session.session_id,
        "paper_id": paper_id,
        "records": list(grouped.values()),
    }


@router.get("/sessions/{session_id}/students/{paper_id}/evidence/{question_no}/{file_name}")
def student_page_evidence_file(
    session_id: str,
    paper_id: str,
    question_no: str,
    file_name: str,
    user: User = Depends(_require_user),
):
    """One integrity-checked transcription-evidence file for teacher review."""

    session = _load_owned(session_id, user.id)
    _require_raw_assets_available(session)
    page = next(
        (item for item in session.student_pages if item.get("paper_id") == paper_id),
        None,
    )
    if not page:
        raise HTTPException(404, detail="学生答题卡页面不存在")
    answer = next(
        (
            item
            for item in page.get("answers") or []
            if str(item.get("question_no", "")) == question_no
        ),
        None,
    )
    transcription = (answer or {}).get("transcription")
    record = transcription if isinstance(transcription, dict) else None
    if not record or not isinstance(record.get("evidence"), dict):
        raise HTTPException(404, detail="转录证据不存在")
    from services.answer_sheet_column_transcription import read_evidence_file

    try:
        data = read_evidence_file(session.session_dir, record, file_name)
    except ValueError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    suffix = f".{file_name.rsplit('.', 1)[-1].lower()}" if "." in file_name else ""
    media_type = _EVIDENCE_MEDIA_TYPES.get(suffix, "application/octet-stream")
    return Response(
        content=data,
        media_type=media_type,
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/sessions/{session_id}/students/{paper_id}/orientation")
@_requires_answer_sheet_writes
@_serialized_session_mutation
async def confirm_student_orientation(
    session_id: str, paper_id: str, req: OrientationConfirmRequest,
    user: User = Depends(_require_user),
):
    session = _load_owned(session_id, user.id)
    _require_no_student_upload(session)
    page = next((item for item in session.student_pages if item.get("paper_id") == paper_id), None)
    if not page:
        raise HTTPException(404, detail="学生答题卡页面不存在")
    source_hash = await _run_answer_sheet_image_work(sha256_file, page["path"])
    try:
        copy = await _run_answer_sheet_image_work(
            create_teacher_oriented_copy,
            page["path"], os.path.join(session.session_dir, "oriented"), paper_id,
            req.quarter_turns_clockwise,
        )
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    page["working_path"] = copy["image_path"]
    page["orientation_working_path"] = copy["image_path"]
    page["orientation_status"] = "teacher_confirmed"
    page["orientation_reason"] = "teacher_confirmed_rotation"
    page["manual_quarter_turns"] = req.quarter_turns_clockwise
    page["orientation_source_sha256"] = source_hash
    page["orientation_working_sha256"] = copy["working_sha256"]
    page["orientation_confirmed_by"] = user.id
    session.invalidate_grading_drafts()
    session.save()
    return session.to_dict(public=True)


def _unique_student_for_code(
    db: Session,
    session: AnswerSheetSession,
    value: str,
    *,
    rows: list[Student] | None = None,
):
    if not value:
        return None
    if rows is None:
        rows = db.query(Student).filter(
            Student.teacher_id == session.teacher_id,
            Student.class_id == session.class_id,
        ).all()
    matches = [row for row in rows if value in {str(row.exam_no or ""), str(row.student_no or "")}]
    return matches[0] if len(matches) == 1 else None


def _extract_student_page_regions(
    aligned_path: str,
    output_dir: str,
    paper_id: str,
    page_id: str,
    identity_regions: list[dict],
    answer_regions: list[dict],
    created_files: list[str] | None = None,
) -> tuple[list[dict], list[dict], list[str]]:
    aligned_rgb = load_rgb_image(aligned_path)
    identity_candidates: list[dict] = []
    answers: list[dict] = []
    review_reasons: list[str] = []
    evidence_root = Path(output_dir)
    evidence_root.mkdir(parents=True, exist_ok=True)
    written_paths: list[Path] = []
    crop_generation = uuid.uuid4().hex

    try:
        for region in identity_regions:
            if region["page_id"] != page_id:
                continue
            identity_type = region["identity_type"]
            crop = crop_normalized_rgb(aligned_rgb, region["bbox"])
            try:
                if identity_type in {"qr", "barcode"}:
                    result = decode_machine_code(crop, identity_type)
                elif identity_type == "exam_no_marks":
                    result = read_exam_no_marks(crop, region.get("config") or {})
                else:
                    result = {
                        "value": "",
                        "confidence": 0.0,
                        "status": "needs_review",
                        "reason": "teacher_confirmation_required",
                    }
                result["identity_type"] = identity_type
                identity_candidates.append(result)
            finally:
                del crop

        for region in answer_regions:
            if region["page_id"] != page_id:
                continue
            crop = crop_normalized_rgb(aligned_rgb, region["bbox"])
            crop_token = hashlib.sha256(
                (
                    f"{paper_id}\0{region['region_id']}\0{crop_generation}"
                ).encode("utf-8")
            ).hexdigest()[:24]
            crop_name = f"crop-{crop_token}.png"
            crop_path = evidence_root / crop_name
            temporary = evidence_root / f".{crop_name}.{uuid.uuid4().hex}.tmp"
            from PIL import Image

            crop_image = Image.fromarray(crop, mode="RGB")
            try:
                crop_image.save(temporary, format="PNG")
                os.replace(temporary, crop_path)
                written_paths.append(crop_path)
                if created_files is not None:
                    created_files.append(str(crop_path.resolve()))
                item = {
                    "region_id": region["region_id"],
                    "question_no": region["question_no"],
                    "region_type": region["region_type"],
                    "evidence_file": crop_name,
                    "answer": "",
                    "status": "needs_review",
                    "reason": "handwritten_transcription_required",
                }
                if region["region_type"] == "choice_mark":
                    item.update(
                        read_choice_marks(
                            crop,
                            region.get("choice_options")
                            or ["A", "B", "C", "D"],
                        )
                    )
                if item["status"] != "recognized":
                    review_reasons.append(
                        f"answer_{region['question_no']}_requires_review"
                    )
                answers.append(item)
            finally:
                crop_image.close()
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    logger.warning("failed to remove incomplete answer crop temp file")
                del crop
        return identity_candidates, answers, review_reasons
    except BaseException:
        for path in written_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("failed to remove incomplete answer crop")
        raise
    finally:
        del aligned_rgb


async def _run_answer_sheet_image_work(function, /, *args, **kwargs):
    return await _run_local_ocr_image_component(
        function,
        *args,
        semaphore=_get_local_ocr_preprocess_semaphore(),
        **kwargs,
    )


def _student_recognition_source_path(page: dict) -> str:
    """Return an immutable/oriented source, never a previous aligned output."""
    oriented = str(page.get("orientation_working_path") or "").strip()
    if oriented and Path(oriented).name.startswith("teacher-oriented-"):
        return oriented
    working = str(page.get("working_path") or "").strip()
    if working and Path(working).name.startswith("teacher-oriented-"):
        return working
    return str(page["path"])


def _registration_source(page: dict) -> tuple[str, str]:
    """The oriented source a batch page is registered from, and its hash."""
    return (
        _student_recognition_source_path(page),
        str(page.get("orientation_working_sha256") or page.get("sha256") or ""),
    )


def _batch_page_registration_current(session: AnswerSheetSession, page: dict) -> bool:
    from services.answer_sheet_page_registration import registration_is_current

    _path, source_sha256 = _registration_source(page)
    return registration_is_current(
        page.get("registered_image"),
        template_fingerprint=str(session.shared_template_hash or ""),
        source_sha256=source_sha256,
    )


async def _register_batch_page(
    session: AnswerSheetSession,
    page: dict,
    matcher_holder: dict,
) -> dict:
    """Register one template-code batch page on the server (answer-sheet B5).

    Always returns a record: a page that cannot be registered gets a
    ``failed`` record, which recognition later refuses (fail closed).
    """
    from services.answer_sheet_page_registration import (
        failed_registration,
        register_student_page,
    )

    source_path, source_sha256 = _registration_source(page)
    fingerprint = str(session.shared_template_hash or "")
    if not session.shared_template_code or not fingerprint:
        return failed_registration(
            "template_not_published",
            "批次尚未绑定已发布的答题卡模板码，无法配准学生页面",
            template_fingerprint=fingerprint,
            source_sha256=source_sha256,
        )
    matcher = matcher_holder.get("matcher")
    if matcher is None:
        try:
            matcher = await _run_answer_sheet_image_work(
                TemplateEvidenceMatcher, session.layout_artifacts, session.session_dir,
            )
        except (OSError, RuntimeError, ValueError):
            logger.exception("answer-sheet template artifacts failed verification for registration")
            return failed_registration(
                "template_artifacts_invalid",
                "答题卡模板配准资源校验失败，无法配准学生页面",
                template_fingerprint=fingerprint,
                source_sha256=source_sha256,
            )
        matcher_holder["matcher"] = matcher
    return await _run_answer_sheet_image_work(
        register_student_page,
        matcher,
        source_path,
        session.session_dir,
        answer_regions=session.answer_regions,
        identity_regions=session.identity_regions,
        template_fingerprint=fingerprint,
        source_sha256=source_sha256,
    )


def _remove_superseded_registration(
    session: AnswerSheetSession,
    previous: object,
    current: object,
) -> None:
    """Delete a replaced registered image once the new record is saved."""
    from services.answer_sheet_page_registration import registered_image_path

    if not isinstance(previous, dict) or not previous.get("file"):
        return
    if isinstance(current, dict) and current.get("file") == previous.get("file"):
        return
    try:
        registered_image_path(session.session_dir, previous).unlink(missing_ok=True)
    except (OSError, ValueError):
        logger.warning("failed to remove superseded registered answer sheet")


def _cleanup_replaced_student_assets(
    session: AnswerSheetSession,
    page: dict,
    *,
    aligned_path: str,
    answers: list[dict],
) -> None:
    """Remove only generated assets superseded by a successful checkpoint."""
    evidence_root = (Path(session.session_dir) / "evidence").resolve()
    old_working = str(page.get("working_path") or "").strip()
    if old_working and os.path.realpath(old_working) != os.path.realpath(aligned_path):
        old_path = Path(old_working).resolve(strict=False)
        if (
            old_path.parent == evidence_root
            and _ALIGNED_EVIDENCE_RE.fullmatch(old_path.name)
        ):
            try:
                old_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("failed to remove superseded aligned answer sheet")

    retained_crops = {
        str(item.get("evidence_file") or "")
        for item in answers
        if isinstance(item, dict)
    }
    for old_answer in page.get("answers") or []:
        if not isinstance(old_answer, dict):
            continue
        name = str(old_answer.get("evidence_file") or "")
        if name in retained_crops or not _ANSWER_CROP_RE.fullmatch(name):
            continue
        try:
            (evidence_root / name).unlink(missing_ok=True)
        except OSError:
            logger.warning("failed to remove superseded answer crop")


def _cleanup_failed_aligned_asset(
    session: AnswerSheetSession,
    aligned_path: str,
    *,
    crop_paths: list[str] | tuple[str, ...] = (),
) -> None:
    evidence_root = (Path(session.session_dir) / "evidence").resolve()
    for raw_path in crop_paths:
        candidate = Path(raw_path).resolve(strict=False)
        if (
            candidate.parent == evidence_root
            and _ANSWER_CROP_RE.fullmatch(candidate.name)
        ):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                logger.warning("failed to remove incomplete answer crop")
    candidate = Path(aligned_path).resolve(strict=False)
    if (
        candidate.parent == evidence_root
        and _ALIGNED_EVIDENCE_RE.fullmatch(candidate.name)
    ):
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            logger.warning("failed to remove incomplete aligned answer sheet")


def _cleanup_student_recognition_attempt(
    evidence_root: Path,
    attempt_root: Path,
) -> None:
    root = evidence_root.resolve()
    candidate = attempt_root.resolve(strict=False)
    if (
        candidate.parent != root
        or not _STUDENT_RECOGNITION_ATTEMPT_RE.fullmatch(candidate.name)
    ):
        logger.error("refusing to remove invalid student recognition attempt path")
        return
    try:
        shutil.rmtree(candidate, ignore_errors=False)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("failed to remove student recognition attempt directory")


def _restore_student_page(page: dict, previous_page: dict) -> None:
    page.clear()
    page.update(copy.deepcopy(previous_page))


def _discard_student_asset_replacement(
    session: AnswerSheetSession,
    replacement: dict | None,
) -> None:
    if not replacement:
        return
    if replacement.get("accelerator_handle") is not None:
        # The remote artifact remains validated and retryable. It is consumed
        # only after the page checkpoint has been durably saved.
        return
    _cleanup_failed_aligned_asset(
        session,
        replacement["aligned_path"],
        crop_paths=replacement["crop_paths"],
    )
    _restore_student_page(replacement["page"], replacement["previous_page"])


def _commit_student_asset_replacement(
    session: AnswerSheetSession,
    replacement: dict | None,
) -> None:
    if not replacement:
        return
    if replacement.get("accelerator_handle") is not None:
        return
    _cleanup_replaced_student_assets(
        session,
        replacement["previous_page"],
        aligned_path=replacement["aligned_path"],
        answers=replacement["answers"],
    )


async def _finish_student_accelerator_replacement(
    replacement: dict | None,
) -> None:
    handle = replacement.get("accelerator_handle") if replacement else None
    if handle is None:
        return
    try:
        await asyncio.to_thread(
            finish_business_preprocessing,
            handle,
            fallback_succeeded=True,
        )
    except Exception:
        # The durable page is authoritative. A cleanup failure must not make a
        # teacher resubmit a page or cause a second image-processing job.
        logger.exception("failed to consume answer-sheet accelerator evidence")


def _student_recognition_signature(session: AnswerSheetSession, page: dict) -> str:
    image_hash = str(
        page.get("orientation_working_sha256") or page.get("sha256") or ""
    )
    signature = f"{session.layout_hash}:{image_hash}"
    if session.teaching_context:
        signature += ":" + hashlib.sha256(json.dumps(
            session.teaching_context, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
    return signature


def _student_page_checkpoint_complete(
    session: AnswerSheetSession,
    page: dict,
) -> bool:
    checkpoint = page.get("recognition_checkpoint")
    return (
        isinstance(checkpoint, dict)
        and checkpoint.get("signature") == _student_recognition_signature(session, page)
        and checkpoint.get("state") in {"completed", "awaiting_orientation"}
    )


def _student_accelerator_state(state: str) -> str:
    return {
        "pending": "waiting_accelerator",
        "unavailable": "unavailable",
        "failed": "fault",
        "fault": "fault",
    }.get(str(state or ""), "fault")


def _student_accelerator_message(state: str) -> str:
    return {
        "waiting_accelerator": "等待教师客户端加速节点处理当前答题卡页，进度已保存。",
        "unavailable": "教师客户端或机会式加速节点当前不可用，答题卡页已保存。",
        "fault": "客户端图像处理失败，当前答题卡页已停止批改。",
    }.get(state, "客户端图像处理状态异常，当前答题卡页已停止批改。")


def _identity_row_for_transcription(
    item: dict,
    roster_rows: list[Student],
) -> tuple[dict, Student | None]:
    value = str(item.get("text") or "").strip()
    confidence = float(item.get("confidence") or 0.0)
    identity_type = str(item.get("identity_type") or "")
    candidate = {
        "identity_type": identity_type,
        "value": value,
        "confidence": confidence,
        "status": "needs_review",
        "reason": "teacher_confirmation_required",
    }
    if item.get("state") != "visible" or not value:
        candidate["reason"] = (
            "identity_unreadable"
            if item.get("state") == "unreadable"
            else "identity_blank"
        )
        return candidate, None

    if identity_type == "name":
        matches = [row for row in roster_rows if str(row.name or "").strip() == value]
    elif identity_type in {"student_no", "exam_no_text"}:
        matches = [
            row for row in roster_rows
            if value in {str(row.student_no or ""), str(row.exam_no or "")}
        ]
    else:
        # QR, barcode and mark-grid text is still shown to the teacher, but a
        # generative transcription is not treated as a machine-code decoder.
        matches = []
    matched = matches[0] if len(matches) == 1 else None
    if matched is not None and confidence >= 0.90:
        candidate.update({
            "status": "recognized",
            "reason": "unique_high_confidence_roster_match",
            "student_id": matched.id,
        })
        return candidate, matched
    candidate["reason"] = (
        "identity_low_confidence"
        if matched is not None
        else "identity_requires_teacher_confirmation"
    )
    return candidate, None


def _add_pairing_reason(page: dict, reason: str) -> None:
    reasons = list(page.get("review_reasons") or [])
    if reason not in reasons:
        reasons.append(reason)
    page["review_reasons"] = reasons


def _clear_pairing_reasons(page: dict) -> None:
    page["review_reasons"] = [
        reason
        for reason in list(page.get("review_reasons") or [])
        if reason not in _ORDERED_PAIRING_REASONS
    ]


def _ordered_duplex_layout_ids(session: AnswerSheetSession) -> list[str]:
    page_ids = [
        str(page.get("page_id") or "").strip()
        for page in session.answer_sheet_pages
        if str(page.get("page_id") or "").strip()
    ]
    return page_ids


def _apply_student_page_pairing(
    session: AnswerSheetSession,
    roster_rows: list[Student],
) -> None:
    """Pair by upload folder, side-marked file name or roster name when every
    page fits one rule, otherwise by upload order."""
    roster = [
        {
            "student_id": row.id,
            "name": row.name,
            "student_no": row.student_no or "",
            "class_id": row.class_id,
        }
        for row in roster_rows
    ]
    if apply_path_pair_identity(
        session.student_pages,
        _ordered_duplex_layout_ids(session),
        roster,
    ):
        return
    _apply_ordered_duplex_identity(session)


def _apply_ordered_duplex_identity(session: AnswerSheetSession) -> None:
    """Pair adjacent capture pages and inherit identity only after hard checks."""
    upload = session.student_upload if isinstance(session.student_upload, dict) else {}
    if str(upload.get("pairing_mode") or _ORDERED_DUPLEX_PAIRING) != _ORDERED_DUPLEX_PAIRING:
        return

    pages = sorted(
        session.student_pages,
        key=lambda item: int(item.get("physical_order") or item.get("index", 0) + 1),
    )
    layout_ids = _ordered_duplex_layout_ids(session)
    template_page_count = len(layout_ids)
    if template_page_count == 1:
        for position, page in enumerate(pages, 1):
            page["physical_order"] = position
            page["physical_pair_index"] = None
            page["physical_side"] = "front"
            page["sheet_index"] = position
            page["sheet_page_index"] = 1
            _clear_pairing_reasons(page)
            page["pairing"] = {
                "mode": "single_page",
                "status": "not_applicable",
                "sheet_index": position,
                "side": "front",
            }
        return
    valid_template = template_page_count >= 2 and template_page_count % 2 == 0
    for position, page in enumerate(pages, 1):
        page["physical_order"] = position
        page["physical_pair_index"] = (position + 1) // 2
        page["physical_side"] = "front" if position % 2 else "back"
        page["sheet_index"] = (position - 1) // template_page_count + 1 if valid_template else None
        page["sheet_page_index"] = (position - 1) % template_page_count + 1 if valid_template else None
        _clear_pairing_reasons(page)
        page["pairing"] = {
            "mode": _ORDERED_DUPLEX_PAIRING,
            "pair_index": (position + 1) // 2,
            "side": "front" if position % 2 else "back",
            "status": "pending",
        }

    if not valid_template:
        for page in pages:
            page["pairing"]["status"] = "template_unknown"
            _add_pairing_reason(page, "ordered_pair_template_unknown")
        return

    if len(pages) % template_page_count:
        incomplete_start = len(pages) - (len(pages) % template_page_count)
        for page in pages[incomplete_start:]:
            page["pairing"]["status"] = "missing_back"
            _add_pairing_reason(page, "ordered_pair_missing_back")

    for sheet_start in range(0, len(pages) - (len(pages) % template_page_count), template_page_count):
        sheet_pages = pages[sheet_start:sheet_start + template_page_count]
        page_type_ok = all(
            page.get("page_id") == layout_ids[index]
            for index, page in enumerate(sheet_pages)
        )
        if not page_type_ok:
            for page in sheet_pages:
                page["pairing"]["status"] = "page_type_mismatch"
                _add_pairing_reason(page, "ordered_pair_page_type_mismatch")
            continue

        front_pages = sheet_pages[::2]
        identity_page = front_pages[0]
        identity = identity_page.get("identity") or {}
        front_id = identity.get("student_id")
        front_ready = identity.get("status") in {"recognized", "teacher_confirmed"} and front_id
        if not front_ready:
            for page in sheet_pages:
                page["pairing"]["status"] = "front_identity_missing"
                _add_pairing_reason(page, "ordered_pair_front_identity_missing")
            continue

        conflict = False
        for page in sheet_pages[1:]:
            page_identity = page.get("identity") or {}
            page_id = page_identity.get("student_id")
            if (
                page_identity.get("status") in {"recognized", "teacher_confirmed"}
                and page_id
                and str(page_id) != str(front_id)
            ):
                conflict = True
                break
        if conflict:
            for page in sheet_pages:
                page["pairing"]["status"] = "identity_conflict"
                _add_pairing_reason(page, "ordered_pair_identity_conflict")
            continue

        for page in sheet_pages:
            page_identity = page.get("identity") or {}
            if page is not identity_page and page_identity.get("status") not in {"recognized", "teacher_confirmed"}:
                page["identity"] = {
                    **page_identity,
                    "status": "ordered_pair_inherited",
                    "student_id": front_id,
                    "student_name": identity.get("student_name") or "",
                    "source": "physical_order_front",
                    "source_paper_id": identity_page.get("paper_id"),
                }
                page["review_reasons"] = [
                    reason
                    for reason in list(page.get("review_reasons") or [])
                    if not reason.startswith("identity_")
                ]
        for pair_offset in range(0, template_page_count, 2):
            front = sheet_pages[pair_offset]
            back = sheet_pages[pair_offset + 1]
            front["pairing"].update(status="paired", paired_paper_id=back.get("paper_id"))
            back["pairing"].update(status="identity_inherited", paired_paper_id=front.get("paper_id"))


def _answer_row_for_transcription(item: dict) -> tuple[dict, str | None]:
    from services.grading_contract import normalize_choice_answer

    state = str(item.get("state") or "")
    value = str(item.get("text") or "").strip()
    result = {
        "region_id": item.get("region_id"),
        "question_no": str(item.get("question_no") or ""),
        "region_type": item.get("region_type"),
        "answer": value,
        "status": "needs_review",
        "reason": "handwritten_transcription_requires_review",
        "vision_confidence": float(item.get("confidence") or 0.0),
    }
    if state != "visible" or not value:
        result["answer"] = "" if state == "blank" else "[无法识别]"
        result["reason"] = (
            "blank_requires_teacher_confirmation"
            if state == "blank"
            else "unreadable_requires_teacher_confirmation"
        )
        return result, f"answer_{result['question_no']}_requires_review"

    if str(item.get("question_type") or "") == "choice":
        normalized = normalize_choice_answer(value)
        options = set(item.get("choice_options") or ["A", "B", "C", "D"])
        mode = str(item.get("choice_mode") or "single")
        valid = bool(
            normalized
            and set(normalized).issubset(options)
            and (
                (mode == "single" and len(normalized) == 1)
                or (mode == "multiple" and 1 <= len(normalized) <= len(options))
            )
        )
        if not valid:
            result.update(
                answer="[无法识别]",
                reason="choice_transcription_invalid",
            )
            return result, f"answer_{result['question_no']}_requires_review"
        result["answer"] = normalized

    result.update(status="recognized", reason="vision_evidence_transcribed")
    return result, None


def _required_answer_source_complete(
    diagnostics: dict,
    region_plan: list[dict],
) -> bool:
    incomplete_raw = diagnostics.get("incomplete_answer_regions")
    if not isinstance(incomplete_raw, list):
        return bool(diagnostics.get("answer_source_complete", False))

    known_regions: set[str] = set()
    required_regions: set[str] = set()
    for item in region_plan:
        question_number = str(item.get("question_number") or "").strip()
        kind = str(item.get("kind") or "")
        if not question_number or kind not in {"identity", "answer"}:
            return False
        known_regions.add(question_number)
        if kind == "answer":
            required_regions.add(question_number)

    incomplete_regions = {
        str(value).strip()
        for value in incomplete_raw
        if str(value).strip()
    }
    if not incomplete_regions.issubset(known_regions):
        return False
    return incomplete_regions.isdisjoint(required_regions)


async def _recognize_one_student_page_accelerated(
    session: AnswerSheetSession,
    page: dict,
    roster_rows: list[Student],
) -> dict | None:
    signature = _student_recognition_signature(session, page)
    if not orientation_is_ready(page):
        page["review_reasons"] = ["orientation_requires_teacher_confirmation"]
        page["recognition_checkpoint"] = {
            "signature": signature,
            "state": "awaiting_orientation",
        }
        return None

    try:
        accelerated_artifacts, region_plans = build_answer_sheet_acceleration_plan(
            session.layout_artifacts,
            session.identity_regions,
            session.answer_regions,
        )
    except (TypeError, ValueError) as exc:
        page["recognition_checkpoint"] = {
            "signature": signature,
            "state": "fault",
            "reason_code": "answer_sheet_layout_contract_invalid",
        }
        page["review_reasons"] = ["fault"]
        raise AnswerSheetAcceleratorState(
            "fault",
            "answer_sheet_layout_contract_invalid",
        ) from exc
    handle = await try_accelerated_evidence(
        teacher_id=str(session.teacher_id or ""),
        batch_id=session.session_id,
        paper_id=str(page.get("paper_id") or ""),
        source_path=_student_recognition_source_path(page),
        template_id=session.session_id,
        artifacts=accelerated_artifacts,
        template_dir=session.session_dir,
        wait_seconds=_ANSWER_SHEET_ACCELERATOR_WAIT_SECONDS,
    )
    if handle.state != "ready" or handle.evidence is None:
        state = _student_accelerator_state(handle.state)
        page["recognition_checkpoint"] = {
            "signature": signature,
            "state": state,
            "job_ids": list(handle.job_ids),
            "reason_code": handle.reason_code,
        }
        page["review_reasons"] = [state]
        raise AnswerSheetAcceleratorState(state, handle.reason_code)

    evidence = handle.evidence
    plan = region_plans.get(evidence.page_type_id)
    if not plan or [item["question_number"] for item in plan] != evidence.question_numbers:
        page["recognition_checkpoint"] = {
            "signature": signature,
            "state": "fault",
            "job_ids": list(handle.job_ids),
            "reason_code": "accelerator_region_contract_mismatch",
        }
        page["review_reasons"] = ["fault"]
        raise AnswerSheetAcceleratorState(
            "fault",
            "accelerator_region_contract_mismatch",
        )

    evidence_paths = tuple(getattr(evidence, "evidence_paths", ()) or ())
    if not evidence_paths:
        evidence_paths = (evidence.evidence_path,)
    try:
        plan = bind_answer_sheet_evidence_locations(
            plan,
            dict(evidence.diagnostics or {}),
            len(evidence_paths),
        )
    except (TypeError, ValueError) as exc:
        page["recognition_checkpoint"] = {
            "signature": signature,
            "state": "fault",
            "job_ids": list(handle.job_ids),
            "reason_code": "accelerator_evidence_location_mismatch",
        }
        page["review_reasons"] = ["fault"]
        raise AnswerSheetAcceleratorState(
            "fault",
            "accelerator_evidence_location_mismatch",
        ) from exc
    transcriptions = await transcribe_answer_sheet_evidence(
        evidence_paths,
        plan,
        teacher_id=session.teacher_id or "",
    )
    identity_candidates: list[dict] = []
    matched_students: list[Student] = []
    answers: list[dict] = []
    review_reasons: list[str] = []
    for item in transcriptions:
        if item.get("kind") == "identity":
            candidate, matched = _identity_row_for_transcription(item, roster_rows)
            identity_candidates.append(candidate)
            if matched is not None:
                matched_students.append(matched)
            continue
        answer, review_reason = _answer_row_for_transcription(item)
        answers.append(answer)
        if review_reason:
            review_reasons.append(review_reason)

    matched_ids = {student.id for student in matched_students}
    if len(matched_ids) == 1:
        matched = matched_students[0]
        identity = {
            "status": "recognized",
            "student_id": matched.id,
            "student_name": matched.name,
            "candidates": identity_candidates,
        }
    else:
        identity = {
            "status": "needs_review",
            "student_id": None,
            "candidates": identity_candidates,
        }
        review_reasons.append("identity_requires_teacher_confirmation")

    diagnostics = dict(evidence.diagnostics or {})
    answer_source_complete = _required_answer_source_complete(
        diagnostics,
        plan,
    )
    page["page_id"] = evidence.page_type_id
    page["registration"] = {
        "status": "passed" if answer_source_complete else "needs_review",
        "diagnostics": {
            **diagnostics,
            "answer_source_complete": answer_source_complete,
        },
    }
    page["identity"] = identity
    page["answers"] = answers
    page["review_reasons"] = list(dict.fromkeys(review_reasons))
    page["recognition_checkpoint"] = {
        "signature": signature,
        "state": "completed",
    }
    return {"accelerator_handle": handle}


async def _recognize_one_student_page(
    session: AnswerSheetSession,
    page: dict,
    matcher: TemplateEvidenceMatcher,
    output_dir: str,
    db: Session,
    roster_rows: list[Student],
) -> dict | None:
    return await _recognize_one_student_page_accelerated(
        session,
        page,
        roster_rows,
    )

    previous_page = copy.deepcopy(page)
    review_reasons: list[str] = []
    signature = _student_recognition_signature(session, page)
    if not orientation_is_ready(page):
        page["review_reasons"] = ["orientation_requires_teacher_confirmation"]
        page["recognition_checkpoint"] = {
            "signature": signature,
            "state": "awaiting_orientation",
        }
        return None

    active_path = _student_recognition_source_path(page)
    evidence_root = Path(output_dir).resolve()
    attempt_root = (
        evidence_root / f".student-recognition-{uuid.uuid4().hex}"
    )
    attempt_root.mkdir(parents=True, exist_ok=False)
    match_handed_off = False

    def mark_match_handoff() -> None:
        nonlocal match_handed_off
        match_handed_off = True

    def cleanup_attempt() -> None:
        _cleanup_student_recognition_attempt(evidence_root, attempt_root)

    try:
        evidence = await _run_answer_sheet_image_work(
            matcher.match_and_build,
            active_path,
            str(attempt_root),
            include_evidence=False,
            on_handoff=mark_match_handoff,
            settle_callbacks=(cleanup_attempt,),
        )
    except TemplateEvidenceError as exc:
        cleanup_attempt()
        page["review_reasons"] = ["page_registration_failed"]
        page["registration"] = {
            "status": "needs_review",
            "diagnostics": exc.diagnostics,
        }
        page.pop("recognition_checkpoint", None)
        return None
    except BaseException:
        if not match_handed_off:
            cleanup_attempt()
        _restore_student_page(page, previous_page)
        raise

    diagnostics = dict(evidence.diagnostics)
    try:
        staged_aligned = Path(
            str(diagnostics.pop("aligned_image_path"))
        ).resolve(strict=False)
        if (
            staged_aligned.parent != attempt_root.resolve()
            or not _ALIGNED_EVIDENCE_RE.fullmatch(staged_aligned.name)
            or not staged_aligned.is_file()
        ):
            raise RuntimeError("student recognition produced an invalid aligned asset")
        final_aligned = evidence_root / staged_aligned.name
        os.replace(staged_aligned, final_aligned)
        aligned_path = str(final_aligned.resolve())
    except BaseException:
        cleanup_attempt()
        _restore_student_page(page, previous_page)
        raise
    cleanup_attempt()

    page_id = evidence.page_type_id
    registration = {"status": "passed", "diagnostics": diagnostics}
    if not diagnostics.get("answer_source_complete", False):
        registration["status"] = "needs_review"
        page["working_path"] = aligned_path
        page["page_id"] = page_id
        page["registration"] = registration
        page["review_reasons"] = ["answer_source_pixels_incomplete"]
        page["identity"] = {
            "status": "needs_review",
            "student_id": None,
            "candidates": [],
        }
        page["answers"] = []
        page["recognition_checkpoint"] = {
            "signature": signature,
            "state": "completed",
        }
        return {
            "page": page,
            "previous_page": previous_page,
            "aligned_path": aligned_path,
            "crop_paths": [],
            "answers": [],
        }

    created_crop_paths: list[str] = []
    extraction_handed_off = False

    def mark_extraction_handoff() -> None:
        nonlocal extraction_handed_off
        extraction_handed_off = True

    def cleanup_uncommitted_assets() -> None:
        _cleanup_failed_aligned_asset(
            session,
            aligned_path,
            crop_paths=created_crop_paths,
        )

    try:
        identity_candidates, answers, answer_review_reasons = (
            await _run_answer_sheet_image_work(
                _extract_student_page_regions,
                aligned_path,
                output_dir,
                str(page["paper_id"]),
                page_id,
                session.identity_regions,
                session.answer_regions,
                created_crop_paths,
                on_handoff=mark_extraction_handoff,
                settle_callbacks=(cleanup_uncommitted_assets,),
            )
        )
        values = {
            item["value"]
            for item in identity_candidates
            if item.get("status") == "recognized" and item.get("value")
        }
        matched = (
            _unique_student_for_code(
                db,
                session,
                next(iter(values)),
                rows=roster_rows,
            )
            if len(values) == 1
            else None
        )
        if matched:
            identity = {
                "status": "recognized",
                "student_id": matched.id,
                "student_name": matched.name,
                "candidates": identity_candidates,
            }
        else:
            identity = {
                "status": "needs_review",
                "student_id": None,
                "candidates": identity_candidates,
            }
            review_reasons.append("identity_requires_teacher_confirmation")
    except BaseException:
        if not extraction_handed_off:
            cleanup_uncommitted_assets()
        _restore_student_page(page, previous_page)
        raise

    page["working_path"] = aligned_path
    page["page_id"] = page_id
    page["registration"] = registration
    page["identity"] = identity
    page["review_reasons"] = [*review_reasons, *answer_review_reasons]
    page["answers"] = answers
    page["recognition_checkpoint"] = {
        "signature": signature,
        "state": "completed",
    }
    return {
        "page": page,
        "previous_page": previous_page,
        "aligned_path": aligned_path,
        "crop_paths": created_crop_paths,
        "answers": answers,
    }


class _StudentRecognitionWorkerError(RuntimeError):
    """Carry an isolated page result out of one bounded worker failure."""

    def __init__(self, page_index: int, page: dict, cause: Exception):
        self.page_index = page_index
        self.page = page
        self.cause = cause
        super().__init__(str(cause))


async def _persist_student_recognition_result(
    session: AnswerSheetSession,
    result: dict,
) -> None:
    """Publish and save one isolated page before consuming its remote asset."""

    page_index = int(result["page_index"])
    if not 0 <= page_index < len(session.student_pages):
        raise RuntimeError("student recognition page index is no longer valid")
    previous_page = session.student_pages[page_index]
    replacement = result.get("replacement")
    session.student_pages[page_index] = result["page"]
    try:
        await asyncio.to_thread(session.save)
    except BaseException:
        session.student_pages[page_index] = previous_page
        _discard_student_asset_replacement(session, replacement)
        raise
    _commit_student_asset_replacement(session, replacement)
    await _finish_student_accelerator_replacement(replacement)


async def _student_recognition_completion_stream(
    session: AnswerSheetSession,
    matcher: TemplateEvidenceMatcher | None,
    output_dir: str,
    db: Session,
    roster_rows: list[Student],
):
    """Recognize pending pages in fair bounded waves using isolated page copies."""

    pending = [
        (page_index, page)
        for page_index, page in enumerate(session.student_pages)
        if not _student_page_checkpoint_complete(session, page)
    ]
    if not pending:
        return

    pending_count = len(pending)
    phase_key = f"answer-sheet-students:{uuid.uuid4().hex}"

    async def worker(indexed_page: tuple[int, dict]) -> dict:
        page_index, source_page = indexed_page
        page = copy.deepcopy(source_page)
        phase_token = set_vision_phase_key(
            phase_key,
            retry_scope_key="answer-sheet-students",
        )
        batch_size_token = set_vision_batch_paper_count(pending_count)
        batch_deadline_token = set_vision_batch_deadline_s(
            _ANSWER_SHEET_OCR_BATCH_DEADLINE_SECONDS
        )
        route_token = set_vision_route_ordinal(page_index)
        try:
            replacement = await _recognize_one_student_page(
                session,
                page,
                matcher,
                output_dir,
                db,
                roster_rows,
            )
            return {
                "page_index": page_index,
                "page": page,
                "replacement": replacement,
            }
        except Exception as exc:
            raise _StudentRecognitionWorkerError(
                page_index,
                page,
                exc,
            ) from exc
        finally:
            reset_vision_route_ordinal(route_token)
            reset_vision_batch_deadline_s(batch_deadline_token)
            reset_vision_batch_paper_count(batch_size_token)
            reset_vision_phase_key(phase_token)

    result_stream = teacher_waved_completion_map(
        pending,
        worker,
        teacher_id=str(session.teacher_id or ""),
        batch_id=phase_key,
        wave_size=OCR_TEACHER_WAVE_SIZE,
        max_concurrent=OCR_BATCH_DISPATCH_CONCURRENT,
    )
    try:
        async for _position, result in result_stream:
            yield result
    except _StudentRecognitionWorkerError as exc:
        if isinstance(exc.cause, AnswerSheetAcceleratorState):
            await _persist_student_recognition_result(session, {
                "page_index": exc.page_index,
                "page": exc.page,
                "replacement": None,
            })
        raise exc.cause
    finally:
        await result_stream.aclose()


@router.post("/sessions/{session_id}/students/recognize")
@_requires_answer_sheet_writes
@_serialized_session_mutation
async def recognize_students(
    session_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    session = _load_owned(session_id, user.id)
    _require_no_student_upload(session)
    if session.status not in {"recognizing", "review"} or not session.student_pages:
        raise HTTPException(409, detail="请先上传学生答题卡")
    await _require_answer_sheet_chain(user)
    matcher = None
    output_dir = os.path.join(session.session_dir, "evidence")
    roster_rows = db.query(Student).filter(
        Student.teacher_id == session.teacher_id,
        Student.class_id == session.class_id,
    ).all()
    result_stream = _student_recognition_completion_stream(
        session,
        matcher,
        output_dir,
        db,
        roster_rows,
    )
    try:
        async for result in result_stream:
            await _persist_student_recognition_result(session, result)
        _apply_student_page_pairing(session, roster_rows)
        session.status = "review"
        session.invalidate_grading_drafts()
        session.save()
    except AnswerSheetAcceleratorState as exc:
        session.status = "recognizing"
        session.error = _student_accelerator_message(exc.state)
        session.save()
        payload = session.to_dict(public=True)
        payload["accelerator"] = {
            "status": exc.state,
            "reason_code": exc.reason_code,
            "message": session.error,
        }
        return JSONResponse(status_code=202, content=payload)
    finally:
        await result_stream.aclose()
    return session.to_dict(public=True)


@router.post("/sessions/{session_id}/students/recognize/stream")
@_requires_answer_sheet_writes
async def recognize_students_stream(
    session_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    session = _load_owned(session_id, user.id)
    _require_no_student_upload(session)
    if session.status not in {"recognizing", "review"} or not session.student_pages:
        raise HTTPException(409, detail="请先上传学生答题卡")
    roster_rows = db.query(Student).filter(
        Student.teacher_id == session.teacher_id,
        Student.class_id == session.class_id,
    ).all()

    async def events():
        try:
            with _locked_owned(session_id, user.id) as locked:
                _require_no_student_upload(locked)
                if (
                    locked.status == "review"
                    and all(
                        _student_page_checkpoint_complete(locked, page)
                        for page in locked.student_pages
                    )
                ):
                    yield _answer_sheet_sse("done", {
                        "message": "学生答题卡识别已完成",
                        "current": len(locked.student_pages),
                        "total": len(locked.student_pages),
                        "idempotent": True,
                        "session": locked.to_dict(public=True),
                    })
                    return
                await _require_answer_sheet_chain(user)
                matcher = None
                output_dir = os.path.join(locked.session_dir, "evidence")
                total = len(locked.student_pages)
                completed = sum(
                    1
                    for page in locked.student_pages
                    if _student_page_checkpoint_complete(locked, page)
                )
                if locked.grouping_decisions:
                    locked.invalidate_grading_drafts()
                locked.asset_lifecycle.pop(_GRADING_DRAFT_CHECKPOINT_KEY, None)
                locked.status = "recognizing"
                locked.error = None
                await asyncio.to_thread(locked.save)
                yield _answer_sheet_sse("progress", {
                    "message": "正在恢复学生答题卡识别" if completed else "正在识别学生答题卡",
                    "current": completed,
                    "total": total,
                })
                try:
                    result_stream = _student_recognition_completion_stream(
                        locked,
                        matcher,
                        output_dir,
                        db,
                        roster_rows,
                    )
                    next_result = asyncio.create_task(anext(result_stream))
                    try:
                        while True:
                            done, _ = await asyncio.wait(
                                {next_result},
                                timeout=_ANSWER_SHEET_HEARTBEAT_SECONDS,
                            )
                            if not done:
                                yield _answer_sheet_sse("progress", {
                                    "message": "正在并发识别学生答题卡",
                                    "current": completed,
                                    "total": total,
                                    "heartbeat": True,
                                })
                                continue
                            try:
                                result = next_result.result()
                            except StopAsyncIteration:
                                break
                            await _persist_student_recognition_result(locked, result)
                            completed += 1
                            page = result["page"]
                            yield _answer_sheet_sse("progress", {
                                "message": "当前页识别进度已保存",
                                "current": completed,
                                "total": total,
                                "checkpoint": True,
                                "paper_id": page.get("paper_id"),
                            })
                            next_result = asyncio.create_task(anext(result_stream))
                    finally:
                        if not next_result.done():
                            next_result.cancel()
                        await asyncio.gather(next_result, return_exceptions=True)
                        await result_stream.aclose()

                    _apply_student_page_pairing(locked, roster_rows)
                    locked.status = "review"
                    locked.error = None
                    await asyncio.to_thread(locked.save)
                    yield _answer_sheet_sse("done", {
                        "message": "学生答题卡识别完成",
                        "current": total,
                        "total": total,
                        "session": locked.to_dict(public=True),
                    })
                except AnswerSheetAcceleratorState as exc:
                    locked.status = "recognizing"
                    locked.error = _student_accelerator_message(exc.state)
                    await asyncio.to_thread(locked.save)
                    yield _answer_sheet_sse("error", {
                        "code": {
                            "waiting_accelerator": "ACCELERATOR_WAITING",
                            "unavailable": "ACCELERATOR_UNAVAILABLE",
                            "fault": "ACCELERATOR_FAULT",
                        }.get(exc.state, "ACCELERATOR_FAULT"),
                        "status": exc.state,
                        "reason_code": exc.reason_code,
                        "message": locked.error,
                    })
                except (asyncio.CancelledError, GeneratorExit):
                    if locked.status != "review":
                        locked.status = "recognizing"
                        locked.error = "学生答题卡识别连接中断，已保存完成页；再次执行即可恢复。"
                        await asyncio.to_thread(locked.save)
                    raise
                except Exception:
                    logger.exception("answer-sheet student recognition stream failed")
                    locked.status = "recognizing"
                    locked.error = "学生答题卡识别链路中断，已保存完成页；请稍后重试恢复。"
                    await asyncio.to_thread(locked.save)
                    yield _answer_sheet_sse("error", {
                        "code": "GRADING_CHAIN_FAILED",
                        "message": locked.error,
                    })
        except HTTPException as exc:
            yield _answer_sheet_sse("error", {
                "code": "ANSWER_SHEET_OPERATION_BUSY" if exc.status_code == 409 else "GRADING_CHAIN_FAILED",
                "message": str(exc.detail),
            })

    return _answer_sheet_stream(stream_teaching(session.teaching_context, events()))


@router.post("/sessions/{session_id}/students/{paper_id}/review-confirm")
@_requires_answer_sheet_writes
@_serialized_session_mutation
def confirm_student_review(
    session_id: str, paper_id: str, req: ReviewConfirmRequest,
    db: Session = Depends(get_db), user: User = Depends(_require_user),
):
    session = _load_owned(session_id, user.id)
    _require_no_student_upload(session)
    if session.status != "review":
        raise HTTPException(409, detail="当前不在教师复核阶段")
    page = next((item for item in session.student_pages if item.get("paper_id") == paper_id), None)
    if not page:
        raise HTTPException(404, detail="学生答题卡页面不存在")
    student = db.query(Student).filter(
        Student.id == req.student_id, Student.teacher_id == user.id,
        Student.class_id == session.class_id,
    ).first()
    if not student:
        raise HTTPException(404, detail="当前班级不存在该学生")
    expected = {item["question_no"] for item in page.get("answers") or []}
    if set(req.answers) - expected:
        raise HTTPException(409, detail="提交了不属于本页的题号")
    for item in page.get("answers") or []:
        value = str(req.answers.get(item["question_no"], item.get("answer") or "")).strip()
        if not value:
            raise HTTPException(409, detail=f"第{item['question_no']}题答案仍未确认")
        item.update({"answer": value, "status": "teacher_confirmed", "reason": "teacher_confirmed"})
    page["identity"] = {
        **(page.get("identity") or {}), "status": "teacher_confirmed",
        "student_id": student.id, "student_name": student.name,
    }
    page["review_reasons"] = []
    _apply_student_page_pairing(
        session,
        db.query(Student).filter(
            Student.teacher_id == session.teacher_id,
            Student.class_id == session.class_id,
        ).all(),
    )
    session.invalidate_grading_drafts()
    session.save()
    return session.to_dict(public=True)


@router.post("/sessions/{session_id}/grading/draft")
@_requires_answer_sheet_writes
async def build_grading_draft(session_id: str, user: User = Depends(_require_user)):
    from services.grader import grade_paper_unified

    session = _load_owned(session_id, user.id)
    _require_no_student_upload(session)
    start_revision = session.review_revision
    if session.status != "review" or not session.student_pages:
        raise HTTPException(409, detail="请先完成学生答题卡识别与教师复核")
    if any(page.get("review_reasons") for page in session.student_pages):
        raise HTTPException(409, detail="仍有页面需要教师复核，不能开始批阅")
    _verified_bubble_grids(session)
    groups = _grading_groups(session)
    result_category, _exam_name = _result_scoring(session)
    answer_map = {
        item["question_no"]: AnswerSpec.model_validate(item).to_answer_map_entry(source="answer_sheet_confirmed")
        for item in session.answer_spec_snapshot
    }
    await _require_answer_sheet_chain(user)
    async def grade_group(item: tuple[int, dict]) -> dict:
        student_id, group = item
        return await _grade_answer_sheet_group(
            grade_paper_unified,
            student_id,
            group,
            answer_map,
            user.id,
            result_category,
            teaching_context=session.teaching_context,
        )

    drafts: list[dict] = []
    result_stream = bounded_completion_map(
        groups.items(),
        grade_group,
        max_concurrent=FLASH_LOCAL_MAX_CONCURRENT,
    )
    try:
        async for _position, draft in result_stream:
            drafts.append(draft)
    finally:
        await result_stream.aclose()
    drafts_by_student = {int(item["student_id"]): item for item in drafts}
    drafts = [drafts_by_student[student_id] for student_id in groups]
    with _locked_owned(session_id, user.id) as latest:
        if latest.review_revision != start_revision:
            raise HTTPException(409, detail="答题卡证据已更新，请重新生成批阅草稿")
        latest.replace_grading_drafts(drafts)
        latest.save()
        persisted_revision = latest.review_revision
    return {
        "session_id": session.session_id, "layout_hash": session.layout_hash,
        "answer_spec_hash": session.answer_spec_hash,
        "review_revision": persisted_revision,
        "drafts": drafts,
    }


def _grading_groups(session: AnswerSheetSession) -> dict[int, dict]:
    groups: dict[int, dict] = {}
    for page in session.student_pages:
        if not orientation_is_ready(page):
            raise HTTPException(409, detail="仍有页面方向未确认，不能开始批阅")
        registration = page.get("registration") or {}
        diagnostics = registration.get("diagnostics") or {}
        if (
            registration.get("status") != "passed"
            or not diagnostics.get("answer_source_complete", False)
        ):
            raise HTTPException(409, detail="仍有答案区域缺少真实源像素，不能开始批阅")
        identity = page.get("identity") or {}
        student_id = identity.get("student_id")
        if identity.get("status") not in _IDENTITY_READY_STATUSES or not student_id:
            raise HTTPException(409, detail="仍有学生身份未确认")
        group = groups.setdefault(int(student_id), {
            "student_name": identity.get("student_name") or "",
            "questions": [],
            "pages": [],
            "question_pages": {},
        })
        group["pages"].append(page["paper_id"])
        for item in page.get("answers") or []:
            if (
                item.get("status") not in {"recognized", "teacher_confirmed"}
                or not str(item.get("answer") or "").strip()
            ):
                raise HTTPException(
                    409,
                    detail=f"第{item.get('question_no')}题答案仍未确认",
                )
            group["questions"].append({
                "question_number": item["question_no"],
                "student_answer": item["answer"],
            })
            question_no = str(item["question_no"])
            question_pages = group["question_pages"].setdefault(question_no, [])
            if page["paper_id"] not in question_pages:
                question_pages.append(page["paper_id"])
    return groups


async def _grade_answer_sheet_group(
    grade_function,
    student_id: int,
    group: dict,
    answer_map: dict,
    teacher_id: int,
    category: str,
    *,
    teaching_context: dict | None = None,
) -> dict:
    snapshot = current_teaching_context() if teaching_context is None else teaching_context
    recognized = {
        "questions": group["questions"],
        "ocr_answers_only": True,
        "ocr_alignment_verified": True,
    }
    if snapshot:
        recognized["teaching_context"] = snapshot
    result = await await_teaching(snapshot, grade_function(
        recognized,
        answer_map,
        group["student_name"],
        teacher_id,
        category=category,
    ))
    if not isinstance(result, dict):
        raise RuntimeError("grading task returned an invalid result")
    for detail in result.get("details") or []:
        if snapshot:
            detail["teaching_context"] = dict(snapshot)
        question_no = str(
            detail.get("parent_question_no")
            or detail.get("question_no")
            or detail.get("question_number")
            or ""
        )
        detail["paper_ids"] = list(
            group["question_pages"].get(question_no, [])
        )
    if snapshot:
        result["teaching_context"] = dict(snapshot)
    return {
        "student_id": student_id,
        "paper_ids": group["pages"],
        "question_pages": group["question_pages"],
        "manual_corrections": [],
        "result": result,
    }


def _grading_checkpoint_drafts(
    session: AnswerSheetSession,
    groups: dict[int, dict],
) -> list[dict]:
    lifecycle = session.asset_lifecycle
    if not isinstance(lifecycle, dict):
        session.asset_lifecycle = {}
        return []
    checkpoint = lifecycle.get(_GRADING_DRAFT_CHECKPOINT_KEY)
    if not isinstance(checkpoint, dict):
        return []
    if (
        checkpoint.get("review_revision") != session.review_revision
        or checkpoint.get("layout_hash") != session.layout_hash
        or checkpoint.get("answer_spec_hash") != session.answer_spec_hash
        or checkpoint.get("student_ids") != list(groups)
        or (checkpoint.get("teaching_context") or {}) != session.teaching_context
    ):
        lifecycle.pop(_GRADING_DRAFT_CHECKPOINT_KEY, None)
        return []
    drafts = checkpoint.get("drafts")
    if not isinstance(drafts, list):
        lifecycle.pop(_GRADING_DRAFT_CHECKPOINT_KEY, None)
        return []
    seen: set[int] = set()
    valid: list[dict] = []
    for draft in drafts:
        if not isinstance(draft, dict):
            return []
        try:
            student_id = int(draft.get("student_id"))
        except (TypeError, ValueError):
            return []
        if student_id not in groups or student_id in seen or not isinstance(draft.get("result"), dict):
            return []
        seen.add(student_id)
        valid.append(draft)
    return valid


@router.post("/sessions/{session_id}/grading/draft/stream")
@_requires_answer_sheet_writes
async def build_grading_draft_stream(
    session_id: str,
    user: User = Depends(_require_user),
):
    from services.grader import grade_paper_unified

    session = _load_owned(session_id, user.id)
    _require_no_student_upload(session)
    if session.status != "review" or not session.student_pages:
        raise HTTPException(409, detail="请先完成学生答题卡识别与教师复核")
    if any(page.get("review_reasons") for page in session.student_pages):
        raise HTTPException(409, detail="仍有页面需要教师复核，不能开始批阅")

    async def events():
        try:
            with _locked_owned(session_id, user.id) as locked:
                _require_no_student_upload(locked)
                finalized = False
                try:
                    lifecycle = locked.asset_lifecycle
                    checkpoint = (
                        lifecycle.get(_GRADING_DRAFT_CHECKPOINT_KEY)
                        if isinstance(lifecycle, dict)
                        else None
                    )
                    if locked.grouping_decisions and not isinstance(checkpoint, dict):
                        finalized = True
                        yield _answer_sheet_sse("done", {
                            "message": "批阅草稿已生成",
                            "current": len(locked.grouping_decisions),
                            "total": len(locked.grouping_decisions),
                            "idempotent": True,
                            "review_revision": locked.review_revision,
                            "drafts": locked.grouping_decisions,
                        })
                        return
                    _verified_bubble_grids(locked)
                    groups = _grading_groups(locked)
                    result_category, _exam_name = _result_scoring(locked)
                    await _require_answer_sheet_chain(user)
                    answer_map = {
                        item["question_no"]: AnswerSpec.model_validate(item).to_answer_map_entry(
                            source="answer_sheet_confirmed"
                        )
                        for item in locked.answer_spec_snapshot
                    }
                    drafts = _grading_checkpoint_drafts(locked, groups)
                    completed_ids = {
                        int(item["student_id"])
                        for item in drafts
                    }
                    total = len(groups)
                    yield _answer_sheet_sse("progress", {
                        "message": "正在恢复已保存的批阅草稿" if drafts else "正在生成批阅草稿",
                        "current": len(drafts),
                        "total": total,
                    })
                    pending_groups = [
                        item
                        for item in groups.items()
                        if item[0] not in completed_ids
                    ]

                    async def grade_group(item: tuple[int, dict]) -> dict:
                        student_id, group = item
                        return await _grade_answer_sheet_group(
                            grade_paper_unified,
                            student_id,
                            group,
                            answer_map,
                            user.id,
                            result_category,
                            teaching_context=locked.teaching_context,
                        )

                    result_stream = bounded_completion_map(
                        pending_groups,
                        grade_group,
                        max_concurrent=FLASH_LOCAL_MAX_CONCURRENT,
                    )
                    next_result = asyncio.create_task(anext(result_stream))
                    try:
                        while True:
                            done, _ = await asyncio.wait(
                                {next_result},
                                timeout=_ANSWER_SHEET_HEARTBEAT_SECONDS,
                            )
                            if not done:
                                yield _answer_sheet_sse("progress", {
                                    "message": "正在并发生成批阅草稿",
                                    "current": len(drafts),
                                    "total": total,
                                    "heartbeat": True,
                                })
                                continue
                            try:
                                _position, draft = next_result.result()
                            except StopAsyncIteration:
                                break
                            drafts.append(draft)
                            locked.asset_lifecycle[_GRADING_DRAFT_CHECKPOINT_KEY] = {
                                "review_revision": locked.review_revision,
                                "layout_hash": locked.layout_hash,
                                "answer_spec_hash": locked.answer_spec_hash,
                                "student_ids": list(groups),
                                "teaching_context": session.teaching_context,
                                "drafts": drafts,
                            }
                            locked.error = None
                            await asyncio.to_thread(locked.save)
                            yield _answer_sheet_sse("progress", {
                                "message": "当前学生批阅进度已保存",
                                "current": len(drafts),
                                "total": total,
                                "checkpoint": True,
                                "student_id": draft["student_id"],
                            })
                            next_result = asyncio.create_task(anext(result_stream))
                    finally:
                        if not next_result.done():
                            next_result.cancel()
                        await asyncio.gather(next_result, return_exceptions=True)
                        await result_stream.aclose()

                    drafts_by_student = {
                        int(item["student_id"]): item for item in drafts
                    }
                    ordered_drafts = [
                        drafts_by_student[student_id] for student_id in groups
                    ]
                    locked.replace_grading_drafts(ordered_drafts)
                    locked.asset_lifecycle.pop(_GRADING_DRAFT_CHECKPOINT_KEY, None)
                    locked.error = None
                    await asyncio.to_thread(locked.save)
                    finalized = True
                    yield _answer_sheet_sse("done", {
                        "message": "批阅草稿已生成",
                        "current": total,
                        "total": total,
                        "review_revision": locked.review_revision,
                        "drafts": ordered_drafts,
                    })
                except (asyncio.CancelledError, GeneratorExit):
                    if not finalized:
                        locked.error = "批阅草稿连接中断，已保存完成学生；再次生成即可恢复。"
                        await asyncio.to_thread(locked.save)
                    raise
                except HTTPException as exc:
                    yield _answer_sheet_sse("error", {
                        "code": (
                            "GRADING_CHAIN_FAILED"
                            if exc.status_code >= 500
                            else "ANSWER_SHEET_VALIDATION_FAILED"
                        ),
                        "message": str(exc.detail),
                    })
                except Exception:
                    logger.exception("answer-sheet grading draft stream failed")
                    locked.error = "数学批改链路中断，已保存完成学生；请稍后重试恢复。"
                    await asyncio.to_thread(locked.save)
                    yield _answer_sheet_sse("error", {
                        "code": "GRADING_CHAIN_FAILED",
                        "message": locked.error,
                    })
        except HTTPException as exc:
            yield _answer_sheet_sse("error", {
                "code": "ANSWER_SHEET_OPERATION_BUSY" if exc.status_code == 409 else "GRADING_CHAIN_FAILED",
                "message": str(exc.detail),
            })

    return _answer_sheet_stream(stream_teaching(session.teaching_context, events()))


@router.post("/sessions/{session_id}/grading/decision")
@_requires_answer_sheet_writes
def save_grading_decision(
    session_id: str,
    req: GradingDecisionRequest,
    user: User = Depends(_require_user),
):
    """Persist a complete teacher verdict set with optimistic concurrency."""
    with _locked_owned(session_id, user.id) as session:
        _require_no_student_upload(session)
        if session.status != "review" or not session.grouping_decisions:
            raise HTTPException(409, detail="请先生成批阅草稿")
        if req.review_revision != session.review_revision:
            raise HTTPException(409, detail="批阅草稿已更新，请刷新后重试")
        draft = next(
            (
                item for item in session.grouping_decisions
                if int(item.get("student_id") or 0) == req.student_id
            ),
            None,
        )
        if draft is None:
            raise HTTPException(404, detail="该学生批阅草稿不存在")

        result = draft.get("result") or {}
        details = result.get("details") or []
        by_question: dict[str, dict] = {}
        for detail in details:
            question_no = str(
                detail.get("question_no")
                or detail.get("question_number")
                or detail.get("parent_question_no")
                or ""
            ).strip()
            if not question_no or question_no in by_question:
                raise HTTPException(409, detail="批阅草稿题号无效，不能保存人工判定")
            by_question[question_no] = detail
        submitted = {item.question_no.strip(): item for item in req.decisions}
        if "" in submitted or len(submitted) != len(req.decisions):
            raise HTTPException(400, detail="人工判定题号不能为空且不得重复")
        if set(submitted) != set(by_question):
            raise HTTPException(400, detail="人工判定必须完整覆盖该学生的全部题目")

        corrections = draft.setdefault("manual_corrections", [])
        changed = 0
        timestamp = datetime.now(timezone.utc).isoformat()
        default_paper_id = str((draft.get("paper_ids") or [""])[0])
        for question_no, decision in submitted.items():
            detail = by_question[question_no]
            old_result = str(detail.get("result") or "needs_review")
            if old_result == decision.result:
                continue
            detail["result"] = decision.result
            detail["teacher_regraded"] = True
            if decision.result == "correct":
                detail["error_type"] = ""
            paper_id = str((detail.get("paper_ids") or [default_paper_id])[0])
            corrections.append({
                "question_no": question_no,
                "paper_id": paper_id,
                "old_result": old_result,
                "new_result": decision.result,
                "note": decision.note.strip(),
                "teacher_id": user.id,
                "created_at": timestamp,
                "audit_key": uuid.uuid4().hex,
            })
            changed += 1

        recalculate_result_from_details(result, _result_scoring(session)[0])
        session.review_revision += 1
        session.save()
        return {
            "ok": True,
            "session_id": session.session_id,
            "student_id": req.student_id,
            "changed": changed,
            "review_revision": session.review_revision,
            "draft": draft,
        }


@router.post("/sessions/{session_id}/grading/archive")
@_requires_answer_sheet_writes
def archive_grading_drafts(
    session_id: str, db: Session = Depends(get_db), user: User = Depends(_require_user),
):
    from services.grading_persistence import (
        persist_grading_result,
        write_deferred_filesystem_archives,
    )

    with _locked_owned(session_id, user.id) as session:
        _require_no_student_upload(session)
        if session.status == "archived":
            return {
                "ok": True,
                "archived": 0,
                "idempotent": True,
                "result_ids": list(session.archived_result_ids),
            }
        if session.status != "review" or not session.grouping_decisions:
            raise HTTPException(409, detail="请先生成并复核批阅草稿")
        for page in session.student_pages:
            registration = page.get("registration") or {}
            diagnostics = registration.get("diagnostics") or {}
            if (
                not orientation_is_ready(page)
                or registration.get("status") != "passed"
                or not diagnostics.get("answer_source_complete", False)
            ):
                raise HTTPException(409, detail="方向或答案区域完整性门禁未通过，不能归档")
        _verified_bubble_grids(session)
        result_category, result_exam_name = _result_scoring(session)

        pages_by_id = {
            str(page.get("paper_id") or ""): (index, page)
            for index, page in enumerate(session.student_pages)
        }
        archived_ids: list[int] = []
        archive_jobs: list[dict] = []
        try:
            for draft in session.grouping_decisions:
                result = draft.get("result") or {}
                if int(result.get("needs_review_count") or 0) > 0:
                    raise HTTPException(409, detail="仍有判分结果需要教师确认，不能归档")
                student = db.query(Student).filter(
                    Student.id == draft.get("student_id"),
                    Student.teacher_id == user.id,
                    Student.class_id == session.class_id,
                ).first()
                if not student:
                    raise HTTPException(404, detail="待归档学生已不存在")

                paper_ids = [str(item) for item in draft.get("paper_ids") or []]
                if not paper_ids or any(paper_id not in pages_by_id for paper_id in paper_ids):
                    raise HTTPException(409, detail="批阅草稿引用的学生原卷已失效")
                page_assets = []
                page_hashes = []
                for paper_id in paper_ids:
                    page_index, page = pages_by_id[paper_id]
                    page_hash = str(page.get("sha256") or "").strip()
                    if not page_hash:
                        raise HTTPException(409, detail="学生原卷缺少内容哈希，不能归档")
                    page_hashes.append(f"{paper_id}:{page_hash}")
                    page_assets.append({
                        "paper_id": paper_id,
                        "page_index": page_index,
                        "filename": str(page.get("filename") or ""),
                    })
                image_hash = hashlib.sha256(
                    (
                        f"{session.session_id}:{session.layout_hash}:"
                        + "|".join(page_hashes)
                    ).encode("utf-8")
                ).hexdigest()
                contract_hash = hashlib.sha256(
                    f"{session.answer_spec_hash}:{session.layout_hash}".encode("utf-8")
                ).hexdigest()
                corrections = list(draft.get("manual_corrections") or [])
                details = copy.deepcopy(result.get("details") or [])
                if session.teaching_context:
                    for detail in details:
                        detail["teaching_context"] = dict(session.teaching_context)
                row, _profile, _created = persist_grading_result(
                    db,
                    teaching_context=session.teaching_context,
                    student=student,
                    teacher_id=user.id,
                    category=result_category,
                    exam_name=result_exam_name,
                    image_path="",
                    image_hash=image_hash,
                    total_questions=int(result.get("total") or 0),
                    correct_count=int(result.get("correct_count") or 0),
                    partial_count=int(result.get("partial_count") or 0),
                    wrong_count=int(result.get("wrong_count") or 0),
                    needs_review_count=int(result.get("needs_review_count") or 0),
                    score=result.get("score"),
                    max_score=result.get("max_score"),
                    correct_rate=result.get("correct_rate"),
                    scoring_version=int(result.get("scoring_version") or 2),
                    grading_contract_hash=contract_hash,
                    details=details,
                    ocr_raw_name=student.name,
                    ocr_confidence=1.0,
                    ocr_raw_json={
                        "source": "answer_sheet",
                        "session_id": session.session_id,
                        "pages": page_assets,
                        "teaching_context": session.teaching_context,
                        **({
                            "batch_label": session.batch_label,
                            "answer_sheet_template_code": session.answer_sheet_template_code,
                            "paper_template_code": session.paper_template_code,
                        } if session.is_batch else {}),
                    },
                    grading_mode="precise",
                    correction_evidence_allowed=not bool(corrections),
                    commit=False,
                    archive_jobs=archive_jobs,
                )
                db.flush()
                for correction in corrections:
                    audit_key = str(correction.get("audit_key") or "").strip()
                    if not audit_key:
                        raise HTTPException(409, detail="人工改判审计键缺失，不能归档")
                    if db.query(GradingCorrection.id).filter(
                        GradingCorrection.audit_key == audit_key
                    ).first():
                        continue
                    db.add(GradingCorrection(
                        grading_result_id=row.id,
                        teacher_id=user.id,
                        paper_id=str(correction.get("paper_id") or ""),
                        question_no=str(correction.get("question_no") or ""),
                        audit_key=audit_key,
                        old_result=str(correction.get("old_result") or ""),
                        new_result=str(correction.get("new_result") or ""),
                        note=str(correction.get("note") or ""),
                    ))
                archived_ids.append(row.id)
            db.commit()
        except Exception:
            db.rollback()
            raise

        write_deferred_filesystem_archives(archive_jobs)
        session.mark_archived(archived_ids)
        session.save()
        return {
            "ok": True,
            "archived": len(archived_ids),
            "idempotent": False,
            "result_ids": archived_ids,
        }


@router.put("/sessions/{session_id}/layout")
@_requires_answer_sheet_writes
@_serialized_session_mutation
def save_layout(
    session_id: str,
    req: LayoutSaveRequest,
    user: User = Depends(_require_user),
):
    session = _load_owned(session_id, user.id)
    try:
        summary = session.save_layout(req.answer_regions, req.identity_regions)
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return {"session": session.to_dict(public=True), "summary": summary}


@router.post("/sessions/{session_id}/layout/freeze")
@_requires_answer_sheet_writes
@_serialized_session_mutation
async def freeze_layout(session_id: str, user: User = Depends(_require_user)):
    session = _load_owned(session_id, user.id)
    try:
        artifacts: dict[str, dict] = {}
        for page_index, page in enumerate(session.answer_sheet_pages):
            page_id = str(page.get("page_id") or "")
            regions = [item for item in session.answer_regions if item["page_id"] == page_id]
            artifact_questions = [{
                "question_number": item["region_id"],
                "question_type": (
                    "choice" if item["region_type"].startswith("choice_") else item["region_type"]
                ),
                "answer_bbox": item["bbox"],
            } for item in regions]
            active_path = page.get("working_path") or page["path"]
            artifacts[page_id] = build_template_artifact(
                active_path, session.session_dir, page_index, artifact_questions,
                blank_detection_enabled=True,
                canonicalization=page.get("canonicalization"),
            )
            expected_question_numbers = [
                str(region.get("question_no") or "").strip()
                for region in session.answer_regions
                if region.get("page_id") == page_id
                and str(region.get("question_no") or "").strip()
            ]
            localization = (
                session.layout_localization
                if isinstance(session.layout_localization, dict)
                else {}
            )
            localized_pages = localization.get("pages")
            localized_page = next((
                item for item in (localized_pages or [])
                if isinstance(item, dict) and item.get("page_id") == page_id
            ), None)
            if (
                localization.get("provider_route") == "qwen3.8max"
                and isinstance(localized_page, dict)
                and isinstance(localized_page.get("column_layout"), dict)
            ):
                # The first-time rough localization already paid for a Qwen
                # page-layout proposal. Reuse that checked proposal when
                # freezing instead of opening a second model request.
                layout_result = {
                    "column_layout": localized_page["column_layout"],
                    "source": {
                        "sha256": artifacts[page_id]["sha256"],
                        "width": artifacts[page_id]["width"],
                        "height": artifacts[page_id]["height"],
                    },
                }
            else:
                layout_result = await recognize_column_layout(
                    Path(session.session_dir)
                    / artifacts[page_id]["template_image"],
                    teacher_id=str(user.id),
                    # Compatibility path for manually bound historical
                    # sessions that predate Qwen answer-sheet localization.
                    column_mode="triple",
                )
            artifacts[page_id]["question_crop_plan"] = build_anchor_crop_plan(
                layout_result,
                artifact_sha256=artifacts[page_id]["sha256"],
                expected_question_numbers=expected_question_numbers,
            )
        # Validate the same immutable contract used when student jobs are
        # enqueued, while the teacher can still correct a crossing region.
        build_answer_sheet_acceleration_plan(
            artifacts,
            session.identity_regions,
            session.answer_regions,
        )
        session.layout_artifacts = artifacts
        session.freeze_layout()
    except TemplateCutError as exc:
        status_code = 503 if exc.retryable or exc.code in {
            "CREDENTIAL_UNAVAILABLE",
            "PROVIDER_BUSY",
        } else 502
        raise HTTPException(
            status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except AnchorCropPlanError as exc:
        raise HTTPException(
            409,
            detail="答题卡题目分栏计划生成失败，请检查模板版面后重试",
        ) from exc
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return session.to_dict(public=True)
