"""模板接口 — 导入识别、分栏与定位、题号编辑、确认和入题库（/batch-grade/template/*）。"""

import asyncio
import copy
import hashlib
import json as _json
import os
import re
import shutil
import uuid
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from database import get_db
from services.processing_errors import failure_from_public, report_failure
from services.teaching_context import (
    await_teaching,
    make_teaching_context,
    require_teacher_stage,
    stream_teaching,
    use_teaching_context,
    record_teaching_fields,
)
from services.ocr_service import (
    _get_local_ocr_preprocess_semaphore,
    _run_local_ocr_image_component,
    reset_vision_teacher_id,
    set_vision_teacher_id,
)
from services.system_readiness import get_public_hardware_readiness
from services.accelerator_artifacts import CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION
from services.accelerator_jobs import STUDENT_MULTI_EVIDENCE_STAGE
from services.analyzer import reset_grading_teacher_id, set_grading_teacher_id
from services.template_session import (
    CONTENT_CHANGED_REASON,
    GEOMETRY_CHANGED_REASON,
    GRADED_PAGE_ROLES,
    TEMPLATE_MODES,
    TemplateSession,
    delete_question as delete_template_question,
    insert_question as insert_template_question,
    normalize_page_label,
    normalize_page_role,
    normalize_template_mode,
    page_is_graded,
    page_metadata,
)
from services.template_page_identity import compute_visual_fingerprint
from services.template_page_numbering import globalize_page_numbers, validate_global_numbering
from services.template_evidence import (
    CanonicalTemplate,
    TemplateEvidenceError,
    accept_canonical_template_candidate,
    build_template_artifact,
    canonicalize_template_image,
    normalize_question_anchors,
    normalize_template_handwriting_regions,
    materialize_template_difference_artifact,
    parse_canonical_metadata,
    verify_template_artifacts,
)
from services.template_recognition import recognize_template_pipeline
from services.template_column_layout import (
    TemplateCutError,
    get_column_layout_readiness,
    layout_needs_review,
    normalize_column_mode,
    recognize_column_layout,
    revise_layout_draft,
    whole_page_fallback_layout,
)
from services.template_anchor_crops import (
    AnchorCropPlanError,
    build_anchor_crop_plan,
    validate_anchor_crop_plan,
)
from services import template_knowledge
from services.grading_contract import (
    reconcile_drawing_contract,
    AnswerSpec,
    VALID_GRADING_CATEGORIES,
    finalize_answer_spec_for_category,
    merge_directory_leaf_score_layer,
)
from models import QuestionBank
from models_user import User
from routers.auth import _require_user
from config import MAX_TEMPLATE_PAGES, MAX_UPLOAD_TOTAL_SIZE
from routers.grade_common import (
    _sse,
    _settle_template_component,
    _new_template_session,
    _load_owned_template_session,
    _locked_owned_template_session,
    _template_knowledge_classifier,
)
from routers.grade_uploads import (
    _save_batch_uploads,
)
from routers.grade_availability import (
    _batch_grade_service_availability,
)
from routers.grade_contract import (
    _template_handwriting_regions_for_mode,
)
from routers.grade_template_models import (
    _TemplateConfirmQuestion,
    _TemplateConfirmPage,
    TemplateConfirmReq,
    TemplateCorrectionCandidatesReq,
    TemplateCutCandidatesReq,
    TemplateColumnOverride,
    TemplateQuestionAnchorOverride,
    TemplateHandwritingRegionOverride,
    TemplateAnswerRegionOverride,
    TemplateLayoutOverridesReq,
    TemplateQuestionEditReq,
    _contract_rejection_reason,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# A healthy page normally completes in roughly two minutes.  Keep a bounded
# eight-minute ceiling for a slow provider/failover without making teachers
# wait for the old accelerator queue (which could be tens of minutes).
# Leave one minute for admission, validation, and persistence around the
# eight-minute provider attempt.
_TEMPLATE_IMPORT_PAGE_HARD_CAP_S = 9 * 60.0
_TEMPLATE_IMPORT_PER_PAGE_DEADLINE_S = _TEMPLATE_IMPORT_PAGE_HARD_CAP_S
_TEMPLATE_IMPORT_BASE_DEADLINE_S = _TEMPLATE_IMPORT_PER_PAGE_DEADLINE_S
_TEMPLATE_IMPORT_PER_ADDITIONAL_PAGE_S = _TEMPLATE_IMPORT_PER_PAGE_DEADLINE_S
_TEMPLATE_IMPORT_MAX_DEADLINE_S = (
    _TEMPLATE_IMPORT_BASE_DEADLINE_S
    + _TEMPLATE_IMPORT_PER_ADDITIONAL_PAGE_S * (MAX_TEMPLATE_PAGES - 1)
)
_TEMPLATE_COMPONENT_HEARTBEAT_S = 15.0
_TEMPLATE_MODEL_SAVE_RESERVE_S = 30.0
_TEMPLATE_EVENT_BUFFER_SIZE = 64
_TEMPLATE_RECOGNITION_TASKS: set[asyncio.Task] = set()


class _TemplateRecognitionEventBuffer:
    """Bound progress buffering without coupling work to one SSE subscriber."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[str, bool]] = asyncio.Queue(
            maxsize=_TEMPLATE_EVENT_BUFFER_SIZE
        )

    def publish(self, chunk: str) -> None:
        terminal = chunk.startswith(("event: done\n", "event: error\n"))
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self._queue.put_nowait((chunk, terminal))

    async def subscribe(self):
        while True:
            chunk, terminal = await self._queue.get()
            yield chunk
            if terminal:
                return


def _track_template_recognition_task(task: asyncio.Task) -> None:
    _TEMPLATE_RECOGNITION_TASKS.add(task)

    def finished(done: asyncio.Task) -> None:
        _TEMPLATE_RECOGNITION_TASKS.discard(done)
        if done.cancelled():
            return
        error = done.exception()
        if error is not None:
            report_failure(error, stage="template")

    task.add_done_callback(finished)


async def _relay_template_recognition(
    source,
    events: _TemplateRecognitionEventBuffer,
    sess: TemplateSession,
) -> None:
    """Run recognition independently and publish only bounded SSE progress."""
    try:
        async for chunk in source:
            events.publish(chunk)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        sess.error_info = report_failure(exc, stage="template", task_id=sess.template_id)
        public_message = sess.error_info["message"]
        sess.status = "failed"
        sess.error_code = "TEMPLATE_BACKGROUND_TASK_FAILED"
        sess.error = public_message
        try:
            sess.save_meta()
        except Exception:
            logger.exception("模板后台故障状态保存失败 template=%s", sess.template_id)
        events.publish(_sse("error", {
            "code": sess.error_code,
            "message": public_message,
            "error_info": sess.error_info,
        }))


class _TemplateRecognitionFailure(RuntimeError):
    """Expected template OCR failure that must be shown to the teacher."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _TemplateImportDeadlineExceeded(TimeoutError):
    """The whole template import exhausted its page-scaled time budget."""


def _template_import_clock() -> float:
    """Read the clock the import deadline is measured on: the event loop's."""
    return asyncio.get_running_loop().time()


def _template_import_deadline_seconds(page_count: int) -> float:
    pages = max(1, int(page_count))
    return min(
        _TEMPLATE_IMPORT_MAX_DEADLINE_S,
        _TEMPLATE_IMPORT_BASE_DEADLINE_S
        + _TEMPLATE_IMPORT_PER_ADDITIONAL_PAGE_S * (pages - 1),
    )


def _template_page_model_deadline(deadline_at: float, remaining_pages: int) -> float:
    """Share remaining time fairly across pages and reserve artifact/save time.

    ``deadline_at`` is on the import clock; the model stages time out on the
    event loop's clock, so the page's deadline is returned as a loop time.
    """
    now = asyncio.get_running_loop().time()
    per_page = max(0.0, deadline_at - _template_import_clock()) / max(1, remaining_pages)
    return now + max(0.0, per_page - _TEMPLATE_MODEL_SAVE_RESERVE_S)


def _template_model_progress_message(page_index: int, progress: dict) -> str:
    labels = {
        "template-merged-v1": "识别题目、定位作答区并生成标准答案",
        "template-directory-v1": "识别题目目录",
        "template-directory-drawing-v1": "定位作图区域",
    }
    running = []
    for phase, state in (progress.get("stages") or {}).items():
        if phase in labels and state.get("status") == "running":
            running.append(labels[phase])
    description = "、".join(running) or "整理已完成结果"
    return f"第 {page_index + 1} 页正在{description}"


def _template_import_deadline_failure(
    deadline_seconds: float,
) -> _TemplateRecognitionFailure:
    minutes = max(1, (int(deadline_seconds) + 59) // 60)
    return _TemplateRecognitionFailure(
        "TEMPLATE_IMPORT_TIMEOUT",
        f"模板导入已达到本次 {minutes} 分钟总时限，所有在途步骤均已清理；"
        "本次模板不可用，请重新发起导入",
    )


async def _poll_template_component(
    task: asyncio.Task,
    *,
    deadline_at: float,
    cancel_running: bool = True,
) -> tuple[bool, object | None]:
    """Wait for one heartbeat interval while enforcing the absolute deadline."""
    remaining_s = deadline_at - _template_import_clock()
    if remaining_s <= 0:
        await _settle_template_component(task, cancel_running=cancel_running)
        raise _TemplateImportDeadlineExceeded
    try:
        done, _pending = await asyncio.wait(
            {task},
            timeout=min(_TEMPLATE_COMPONENT_HEARTBEAT_S, remaining_s),
        )
        if task in done:
            return True, task.result()
        if _template_import_clock() >= deadline_at:
            await _settle_template_component(task, cancel_running=cancel_running)
            raise _TemplateImportDeadlineExceeded
        return False, None
    except asyncio.CancelledError:
        await _settle_template_component(task, cancel_running=cancel_running)
        raise


async def _run_template_cpu_component(function, /, *args, **kwargs):
    """Run heavy image work behind the shared gate and stall readiness tracker."""
    return await _run_local_ocr_image_component(
        function,
        *args,
        semaphore=_get_local_ocr_preprocess_semaphore(),
        **kwargs,
    )


async def _batch_grade_template_service_availability() -> dict:
    """Return template readiness, including the local two-column fallback."""
    try:
        state = await get_column_layout_readiness()
    except Exception:
        logger.exception("template layout readiness probe failed")
        state = {"status": "fault"}
    status = str(state.get("status") or "fault")
    if status == "available":
        return {
            "status": "available",
            "label": "可用",
            "message": "模板版面识别链路已就绪。",
        }
    if status == "queued":
        return {
            "status": "queued",
            "label": "需等待",
            "message": "模板识别容量繁忙，提交后可能需要等待。",
        }
    # A common landscape two-column template is split locally in a bounded CPU
    # pass, so an unavailable remote layout probe must not disable the upload UI.
    # Ambiguous pages still fail closed when the actual remote layout call is
    # reached; expose the remote status for diagnostics without blocking work.
    return {
        "status": "available",
        "label": "可用",
        "message": (
            "本地双栏版式可直接处理；复杂版式将在识别时检查远程分栏服务。"
        ),
        "remote_layout_status": status,
        "local_layout_fallback": "local_gutter_v1",
    }


@router.get("/batch-grade/template/availability")
async def batch_grade_template_availability(user: User = Depends(_require_user)):
    # Template registration performs page correction and frozen-column montage
    # generation on the server CPU.  The retired canonicalization/OCR evidence
    # accelerator stages must therefore remain informational and must not turn a
    # healthy template route into ``waiting_accelerator``.
    registration_hardware, availability = await asyncio.gather(
        get_public_hardware_readiness(
            user.id,
            stage=STUDENT_MULTI_EVIDENCE_STAGE,
            artifact_contract=CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION,
        ),
        _batch_grade_template_service_availability(),
    )
    result = dict(availability)
    result["service_status"] = str(result.get("status") or "fault")
    result["hardware"] = {
        "status": "ready",
        "message": "模板页面矫正与分栏识别图由服务器本地处理，无需等待加速节点。",
    }
    result["registration_hardware"] = {
        "status": str(registration_hardware.get("status") or "fault"),
        # Student recognition has the verified server CPU matcher fallback.
        # Keep node health visible without imposing a second upload gate.
        "advisory": True,
        "message": str(
            registration_hardware.get("message")
            or "加速节点不可用，请使用客户端"
        ),
    }
    return result


async def _require_template_recognition_chain(user: User) -> dict:
    """Require core grading services while keeping local layout fallback usable.

    The remote layout model is still used for ambiguous page layouts, but the
    common two-column layout is detected locally before that call. Its readiness
    probe is therefore advisory here; the actual remote path remains fail-closed
    if it is needed.
    """
    try:
        availability = await _batch_grade_service_availability(user)
    except Exception:
        logger.exception("template core readiness check failed")
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
    try:
        state = await get_column_layout_readiness()
    except Exception:
        logger.exception("template recognition readiness check failed")
        state = {"status": "fault"}
    status = str(state.get("status") or "fault")
    if status not in {"available", "queued"}:
        logger.warning(
            "template layout readiness is %s; local gutter layout will be tried "
            "before any remote fallback",
            status,
        )
        return {
            **availability,
            "template_layout_status": status,
            "template_layout_fallback": "local_gutter_v1",
            "template_layout_message": (
                "本地双栏版式可直接处理；其他版式将在实际识别时检查远程服务。"
            ),
        }
    return state


def _template_recognition_failure(error: str = "") -> _TemplateRecognitionFailure:
    """Map provider details to safe, actionable teacher-facing messages."""
    normalized = str(error or "").lower()
    if any(marker in normalized for marker in ("401", "unauthorized", "invalid api key", "authentication")):
        return _TemplateRecognitionFailure(
            "VISION_AUTH_FAILED",
            "视觉识别服务认证失败，请联系管理员检查视觉识别服务的密钥配置",
        )
    if any(marker in normalized for marker in (
        "absolute_attempt_ttl",
        "first_sse_timeout",
        "response_header_timeout",
        "sse_idle_timeout",
        "pool_dispatch_deadline",
    )):
        return _TemplateRecognitionFailure(
            "VISION_TIMEOUT",
            "视觉识别服务响应超时，本次未返回完整题目；请重新识别",
        )
    if str(error or "").startswith("模板 OCR "):
        return _TemplateRecognitionFailure(
            "TEMPLATE_OUTPUT_INVALID",
            "模板识别结果格式异常，请重新识别；若仍失败请联系管理员",
        )
    if error:
        return _TemplateRecognitionFailure(
            "TEMPLATE_OCR_FAILED",
            "模板题目识别失败，请稍后重试；若持续发生请联系管理员检查视觉识别服务",
        )
    return _TemplateRecognitionFailure(
        "TEMPLATE_NO_QUESTIONS",
        "模板未识别到任何题目，请检查图片是否完整、清晰后重新上传",
    )


async def _classify_confirmed_template_knowledge(
    template_id: str,
    teacher_id: int,
    expected_contract_hash: str,
) -> None:
    """Resume/reuse early derived work after confirmation, never save meta.json."""
    try:
        sess = _load_owned_template_session(template_id, teacher_id)
        if (
            sess.status not in {"confirmed", "frozen"}
            or sess.confirmed_contract_hash != expected_contract_hash
            or sess.content_hash() != expected_contract_hash
        ):
            return
        await template_knowledge.ensure_session_knowledge(
            sess, classifier=_template_knowledge_classifier,
        )
    except Exception as exc:
        logger.warning("模板知识点后台分类失败 template=%s: %s", template_id, type(exc).__name__)


def _refresh_template_difference_artifact(
    template_dir: str,
    artifact: dict,
    *,
    template_mode: object = "marked",
    raw_regions: object = None,
) -> dict:
    """Keep the reusable whiteout baseline aligned with approved boxes.

    The original registration image remains intact for SIFT. Both template
    modes get a hash-bound difference image whenever their visual contract
    contains handwriting metadata, including the explicit empty-box baseline.
    Legacy blank artifacts without that field remain readable and are not
    retroactively given synthetic boxes.
    """
    updated = dict(artifact)
    expected = [
        str(value).strip()
        for value in (updated.get("question_numbers") or [])
        if str(value).strip()
    ]
    mode = normalize_template_mode(template_mode, default="marked")
    # The unified answer-box collection is the exemption contract for every
    # legacy mode label; never zero it for "blank". When the stored contract
    # is missing or empty, the confirmed answer boxes themselves carry it.
    has_region_contract = (
        raw_regions is not None
        or "template_handwriting_regions" in updated
        or "template_answer_regions" in updated
    )
    if mode == "blank" and not has_region_contract:
        # Old blank artifacts intentionally had no exemption contract. Keep
        # them compatible until a teacher reimports and confirms a new one.
        return updated
    stored_regions = updated.get("template_handwriting_regions")
    if raw_regions is not None:
        contract_regions = raw_regions
    elif stored_regions:
        contract_regions = stored_regions
    else:
        # A stored empty list cannot mean "erase the boxes": fall back to
        # the confirmed answer boxes so one collection serves both roles.
        contract_regions = updated.get("template_answer_regions")
    normalized = _template_handwriting_regions_for_mode(
        template_mode,
        contract_regions,
        expected,
    )
    updated["template_handwriting_regions"] = normalized
    updated.pop("difference_template_image", None)
    updated.pop("difference_template_sha256", None)
    return materialize_template_difference_artifact(template_dir, updated)


# ════════════════════════════════════════════════════════════════════
#  多页一次性模板：recognize（OCR N 页）→ confirm（老师核对即权威 + 沉淀题库）
#  模板是本批次绝对权威；页数即类型数。学生卷匹配复用见 step 3。
# ════════════════════════════════════════════════════════════════════


@router.post("/batch-grade/template/correction-candidates")
def batch_grade_template_correction_candidates(
    req: TemplateCorrectionCandidatesReq,
    user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    """Show current-class pending originals by identity, knowledge point, or similarity."""
    sess = _load_owned_template_session(req.template_id, user.id)
    from services.correction_tracking import template_candidates

    questions = [question.model_dump() for page in req.pages for question in page.questions]
    return {
        "template_id": req.template_id,
        "candidates": template_candidates(
            db,
            teacher_id=user.id,
            class_id=getattr(user, "current_class_id", None),
            questions=questions,
            teaching_context=sess.teaching_context,
        ),
    }


def _synchronized_page_artifact(
    artifact: dict,
    page: dict,
    renames: dict[str, str],
    *,
    added_number: str = "",
    before_number: str = "",
) -> dict:
    """Rebuild one page artifact around an edited question list.

    Every collection in an artifact is keyed by question number and asserted to
    cover the page's questions exactly, so a question edit has to move all of
    them together or the next validation rejects the template.  A renamed
    question keeps its boxes: the ink they mark did not move, only its label.
    """
    numbers = [
        str(
            question.get("question_no")
            or question.get("question_number")
            or ""
        ).strip()
        for question in (page.get("questions") or [])
    ]
    numbers = [number for number in numbers if number]
    updated = dict(artifact)
    updated["question_numbers"] = list(numbers)
    from services.template_evidence import normalize_question_schema
    updated["question_schema"] = normalize_question_schema(page.get("questions") or [])
    if "question_anchors" in artifact:
        existing = normalize_question_anchors(artifact.get("question_anchors") or [])
        renamed = {
            renames.get(anchor["question_number"], anchor["question_number"]): anchor
            for anchor in existing
        }
        anchors: list[dict] = []
        for number in numbers:
            found = renamed.get(number)
            if found is not None:
                # Relabelling a question must not discard the confidence and
                # provenance attached to its server-owned geometry.
                preserved = dict(found)
                preserved["question_number"] = number
                if "question_number_bbox" in found:
                    # Preserve legacy geometry while loading an old artifact;
                    # new model responses do not create this field.
                    preserved["question_number_bbox"] = list(
                        found["question_number_bbox"]
                    )
                anchors.append(preserved)
            elif number == added_number:
                # This conspicuous top-of-page handle is an editable draft,
                # NOT a located printed number. A review gate below prevents
                # freezing until the teacher saves the corrected location.
                new_anchor = {"question_number": number}
                if artifact.get("question_label_policy"):
                    new_anchor["question_number_bbox"] = [80, 20, 120, 45]
                anchors.append(new_anchor)
                updated["geometry_review_pending"] = True
        updated["question_anchors"] = normalize_question_anchors(anchors)
    if "template_handwriting_regions" in artifact:
        existing_regions = normalize_template_handwriting_regions(
            artifact.get("template_handwriting_regions") or []
        )
        renamed_regions = {
            renames.get(item["question_number"], item["question_number"]): item
            for item in existing_regions
        }
        synchronized_regions = []
        for number in numbers:
            found = renamed_regions.get(number)
            if found is None:
                synchronized_regions.append({
                    "question_number": number,
                    "regions": [],
                })
                continue
            # Keep confidence/source/teacher-confirmed markers alongside the
            # moved rectangles; only the question label changes.
            preserved = dict(found)
            preserved["question_number"] = number
            preserved["regions"] = [list(box) for box in (found.get("regions") or [])]
            synchronized_regions.append(preserved)
        updated["template_handwriting_regions"] = normalize_template_handwriting_regions(
            synchronized_regions
        )
    if "template_answer_regions" in artifact:
        answer_by_number = {
            renames.get(item["question_number"], item["question_number"]): item
            for item in normalize_template_handwriting_regions(artifact["template_answer_regions"])
        }
        updated["template_answer_regions"] = [
            {**copy.deepcopy(answer_by_number.get(number) or {"regions": []}), "question_number": number}
            for number in numbers
        ]
    updated.pop("difference_template_image", None)
    updated.pop("difference_template_sha256", None)
    raw_plan = artifact.get("question_crop_plan")
    if isinstance(raw_plan, dict):
        synchronized_plan = dict(raw_plan)
        synchronized_plan["expected_question_numbers"] = list(numbers)
        updated["question_crop_plan"] = validate_anchor_crop_plan(
            synchronized_plan,
            artifact_sha256=str(artifact.get("sha256") or ""),
        )
    return updated


def _validate_template_cut_request(req: TemplateCutCandidatesReq) -> tuple[str, list[str], str]:
    action_id = str(req.action_id or "").strip()
    if not 8 <= len(action_id) <= 64 or any(
        not (char.isalnum() or char in "_-:") for char in action_id
    ):
        raise HTTPException(400, detail="action_id 格式无效")
    cut_types = list(dict.fromkeys(req.cut_types or []))
    if cut_types != ["question"]:
        raise HTTPException(400, detail="当前模板定位只支持分栏裁题")
    return action_id, cut_types, req.image_type


def _template_geometry_review_state(page_run: object) -> tuple[bool, bool]:
    """Normalize current and legacy geometry-review markers for one page."""
    if not isinstance(page_run, dict):
        return False, False
    anchor_diagnostics = page_run.get("anchor_diagnostics")
    template_ocr_diagnostics = page_run.get("template_ocr_diagnostics")
    has_candidate_draft = any(
        isinstance(page_run.get(key), list) and bool(page_run[key])
        for key in (
            "question_anchor_candidates",
            "template_answer_region_candidates",
            "template_handwriting_region_candidates",
        )
    )
    anchor_review_pending = bool(
        page_run.get("anchor_review_pending")
        or (
            isinstance(anchor_diagnostics, dict)
            and anchor_diagnostics.get("needs_review") is True
        )
        or has_candidate_draft
    )
    geometry_review_pending = bool(
        page_run.get("geometry_review_pending")
        or anchor_review_pending
        or (
            isinstance(template_ocr_diagnostics, dict)
            and template_ocr_diagnostics.get(
                "geometry_review_pending", template_ocr_diagnostics.get("needs_review")
            ) is True
        )
    )
    return anchor_review_pending, geometry_review_pending


def _template_cut_response(sess: TemplateSession, run: dict) -> dict:
    totals = {
        "total": 0,
        "original": 0,
        "rectified_untrusted": 0,
        "invalid": 0,
        "missing": 0,
    }
    for page in (run.get("pages") or {}).values():
        for result in (page.get("modes") or {}).values():
            for key in totals:
                totals[key] += int((result.get("summary") or {}).get(key) or 0)
    total_operations = len(sess.pages) * len(run.get("cut_types") or [])
    completed_operations = sum(
        len(page.get("modes") or {})
        for page in (run.get("pages") or {}).values()
    )
    pages = []
    for label, raw_page in sorted((run.get("pages") or {}).items()):
        page = raw_page if isinstance(raw_page, dict) else {}
        raw_artifact = ((sess.page_artifacts or {}).get(label) or {})
        artifact = raw_artifact if isinstance(raw_artifact, dict) else {}
        anchor_review_pending, geometry_review_pending = (
            _template_geometry_review_state(page)
        )
        geometry_review_pending = bool(
            geometry_review_pending or page.get("layout_review_pending")
        )
        pages.append({
            "page_label": label,
            **page,
            "anchor_review_pending": anchor_review_pending,
            "geometry_review_pending": geometry_review_pending,
            # Candidates remain explicitly non-authoritative.  Only geometry
            # present in the verified artifact is returned through the legacy
            # authoritative fields below.
            "question_anchor_candidates": (
                page.get("question_anchor_candidates")
                if isinstance(page.get("question_anchor_candidates"), list)
                else []
            ),
            "template_handwriting_region_candidates": (
                page.get("template_handwriting_region_candidates")
                if isinstance(
                    page.get("template_handwriting_region_candidates"),
                    list,
                )
                else []
            ),
            "template_answer_region_candidates": (
                page.get("template_answer_region_candidates")
                if isinstance(
                    page.get("template_answer_region_candidates"),
                    list,
                )
                else []
            ),
            "anchor_diagnostics": (
                page.get("anchor_diagnostics")
                if isinstance(page.get("anchor_diagnostics"), dict)
                else None
            ),
            "template_ocr_diagnostics": (
                page.get("template_ocr_diagnostics")
                if isinstance(page.get("template_ocr_diagnostics"), dict)
                else None
            ),
            "question_anchors": (
                artifact.get("question_anchors")
                if isinstance(artifact.get("question_anchors"), list)
                else []
            ),
            "question_schema": artifact.get("question_schema") or [],
            "question_schema_candidates": page.get("question_schema_candidates") or [],
            "template_handwriting_regions": (
                artifact.get("template_handwriting_regions")
                if isinstance(
                    artifact.get("template_handwriting_regions"),
                    list,
                )
                else []
            ),
            # Blank templates persist their reusable printed answer areas in
            # the verified artifact.  Keep this authoritative geometry in the
            # cut-preview response alongside question anchors so the teacher
            # confirmation UI can render the answer boxes after a normal
            # (non-candidate) cut response.  Without this field the frontend
            # can only render the column overlays even though the
            # artifact and differential path both have the regions.
            "template_answer_regions": (
                artifact.get("template_answer_regions")
                if isinstance(
                    artifact.get("template_answer_regions"),
                    list,
                )
                else []
            ),
        })
    return {
        "success": run.get("status") == "complete",
        "template_id": sess.template_id,
        "action_id": run.get("action_id"),
        "status": run.get("status"),
        "cut_types": run.get("cut_types") or [],
        "image_type": run.get("image_type") or "photo",
        # Pages whose column draft is still a review candidate; the teacher
        # must save boundaries for each before the template can be confirmed.
        "layout_review_pending_pages": _template_layout_review_pending_pages(sess),
        "anchor_review_pending_pages": _template_anchor_review_pending_pages(sess),
        "geometry_review_pending_pages": _template_geometry_review_pending_pages(sess),
        "pages": pages,
        "summary": totals,
        "progress": {
            "completed": completed_operations,
            "total": total_operations,
            "remaining": max(0, total_operations - completed_operations),
        },
        "candidate_contract": {
            "question": "版面模型只决定单栏、双栏或三栏边界；同模板学生卷复用冻结边界",
            "answer": "版面内容块，仅供评估，禁止直接作为 answer_bbox",
            "anchor_crop_plan": "每栏生成一个全高裁剪块，按从左到右顺序一次发送给视觉模型",
        },
        "error": run.get("error"),
        "created_at": run.get("created_at"),
        "completed_at": run.get("completed_at"),
    }


def _layout_result_from_frozen_question_plan(
    artifact: dict,
    plan: dict,
) -> dict:
    """Build a preview-compatible result without calling the layout model again."""
    columns = list(plan["parameters"]["columns"])
    candidates = []
    for candidate_index, column in enumerate(columns):
        left = int(column["x1_1000"])
        right = int(column["x2_1000"])
        column_name = f"col_{int(column['column_index'])}"
        canonical = {
            "column": column_name,
            "bbox_1000": [left, 0, right, 1000],
        }
        candidates.append({
            "candidate_index": candidate_index,
            "candidate_role": "question_block",
            "question_number_hint": "",
            "question_number_hint_source": "frozen_plan",
            "coordinate_trust": "model_coarse",
            "usable_for_original_overlay": True,
            "template_bbox_1000": [left, 0, right, 1000],
            "raw_sha256": hashlib.sha256(
                _json.dumps(canonical, sort_keys=True).encode("utf-8")
            ).hexdigest(),
        })
    return {
        "provider": "frozen_template_plan",
        "operation": "RecognizeColumnLayout",
        "cut_type": "question",
        "output_oricoord": True,
        "request_id": "frozen-plan",
        "source": {
            "sha256": artifact["sha256"],
            "width": artifact["width"],
            "height": artifact["height"],
        },
        "provider_dimensions": {
            "org_width": artifact["width"],
            "org_height": artifact["height"],
            "rectified_width": artifact["width"],
            "rectified_height": artifact["height"],
        },
        "pages": [{
            "page_index": 0,
            "provider_page_no": 1,
            "provider_dimensions": {},
            "candidates": candidates,
        }],
        "summary": {
            "total": len(candidates),
            "original": len(candidates),
            "rectified_untrusted": 0,
            "invalid": 0,
            "missing": 0,
        },
        "column_layout": {
            "column_count": len(columns),
            "columns": columns,
        },
    }


def _latest_page_layout_draft(
    sess: TemplateSession,
    page_label: str,
    artifact: dict,
) -> dict:
    """Return the newest layout draft bound to this artifact, or a source stub.

    A teacher edit is a revision of the draft the confirmation page displayed,
    so the revision counter and column identities must continue from it. A
    page that predates layout drafts gets a source-only stub; its first edit
    then starts at revision 1 instead of inheriting a synthetic history.
    """
    artifact_sha = str(artifact.get("sha256") or "")
    matching: list[tuple[tuple[int, str], dict]] = []
    for run in (sess.layout_cut_runs or {}).values():
        if not isinstance(run, dict):
            continue
        page_run = (run.get("pages") or {}).get(page_label)
        if not isinstance(page_run, dict):
            continue
        mode = (page_run.get("modes") or {}).get("question")
        if not isinstance(mode, dict) or not isinstance(mode.get("column_layout"), dict):
            continue
        if str((mode.get("source") or {}).get("sha256") or "") != artifact_sha:
            continue
        revision = mode.get("layout_revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            revision = 0
        stamp = str(run.get("completed_at") or run.get("created_at") or "")
        matching.append(((revision, stamp), mode))
    if matching:
        return max(matching, key=lambda item: item[0])[1]
    stub: dict = {
        "source": {
            "sha256": artifact_sha,
            "width": artifact.get("width"),
            "height": artifact.get("height"),
        },
    }
    # No draft survived in the runs, but the artifact may still hold the
    # frozen plan of the previous edit. Rebuild enough of it for the revision
    # counter to continue rather than restarting at 1 on every save.
    plan = artifact.get("question_crop_plan")
    columns = (plan.get("parameters") or {}).get("columns") if isinstance(plan, dict) else None
    if isinstance(columns, list) and columns:
        stub["column_layout"] = {"columns": [dict(column) for column in columns]}
        stub["layout_mode"] = {1: "single", 2: "double", 3: "triple"}.get(len(columns), "")
        revisions = {
            column.get("layout_revision")
            for column in columns
            if isinstance(column, dict) and column.get("layout_revision") is not None
        }
        context_revision = (plan.get("layout_context") or {}).get("layout_revision")
        if context_revision is not None:
            revisions.add(context_revision)
        if len(revisions) == 1:
            stub["layout_revision"] = next(iter(revisions))
    return stub


_TEMPLATE_GEOMETRY_REVIEW_METADATA_KEYS = (
    "anchor_review_pending",
    "geometry_review_pending",
    "question_anchor_candidates",
    "template_answer_region_candidates",
    "template_handwriting_region_candidates",
    "question_schema_candidates",
    "anchor_diagnostics",
    "template_ocr_diagnostics",
)


def _latest_template_geometry_review_metadata(
    sess: TemplateSession,
    page_label: str,
    artifact_sha256: str,
) -> dict:
    """Copy review and bounded-import metadata for one immutable page.

    A new cut action owns a fresh layout-model run, but it does not rerun the
    question-anchor/exemption recognizer.  Keep that review-only draft attached
    to the page until a teacher override clears it from every historical run.
    Completed geometry also retains model-stage metadata: pruning old cut
    runs must not erase the missing-answer/reference confirmation contract.
    The artifact hash prevents data from an older upload revision crossing
    into the new page.
    """
    expected_sha = str(artifact_sha256 or "").strip()
    if not expected_sha:
        return {}
    matching: list[tuple[tuple[str, int], dict]] = []
    for order, run in enumerate((sess.layout_cut_runs or {}).values()):
        if not isinstance(run, dict):
            continue
        page_run = (run.get("pages") or {}).get(page_label)
        if not isinstance(page_run, dict):
            continue
        page_sha = str(page_run.get("artifact_sha256") or "").strip()
        if not page_sha:
            question_mode = (page_run.get("modes") or {}).get("question")
            if isinstance(question_mode, dict):
                page_sha = str(
                    (question_mode.get("source") or {}).get("sha256") or ""
                ).strip()
        anchor_review_pending, geometry_review_pending = (
            _template_geometry_review_state(page_run)
        )
        if page_sha != expected_sha:
            continue
        bounded_diagnostics = page_run.get("template_ocr_diagnostics") or {}
        if not geometry_review_pending and not bounded_diagnostics.get("model_stages"):
            continue
        keys = (_TEMPLATE_GEOMETRY_REVIEW_METADATA_KEYS if geometry_review_pending
                else ("template_ocr_diagnostics",))
        metadata = {
            key: copy.deepcopy(page_run[key])
            for key in keys
            if key in page_run
        }
        if not metadata:
            continue
        if anchor_review_pending:
            metadata["anchor_review_pending"] = True
        metadata["geometry_review_pending"] = geometry_review_pending
        stamp = str(run.get("completed_at") or run.get("created_at") or "")
        matching.append(((stamp, order), metadata))
    if not matching:
        return {}
    return max(matching, key=lambda item: item[0])[1]


def _plan_column_bounds(plan: object) -> list[tuple[int, int]] | None:
    if not isinstance(plan, dict):
        return None
    columns = (plan.get("parameters") or {}).get("columns")
    if not isinstance(columns, list):
        return None
    try:
        return [(int(column["x1_1000"]), int(column["x2_1000"])) for column in columns]
    except (KeyError, TypeError, ValueError):
        return None


def _template_layout_review_pending_pages(sess: TemplateSession) -> list[int]:
    """Pages whose column draft still needs the teacher's boundaries.

    A review draft is resolved only by a saved layout override, which writes a
    frozen ``question_crop_plan`` into the artifact and clears the marker on
    every run. Until then the template must not be confirmed.
    """
    pending: set[int] = set()
    for run in (sess.layout_cut_runs or {}).values():
        if not isinstance(run, dict):
            continue
        for page_label, page_run in (run.get("pages") or {}).items():
            if not isinstance(page_run, dict) or not page_run.get("layout_review_pending"):
                continue
            artifact = (sess.page_artifacts or {}).get(page_label)
            if isinstance(artifact, dict) and isinstance(
                artifact.get("question_crop_plan"), dict
            ):
                continue
            try:
                pending.add(int(str(page_label).removeprefix("page_")) - 1)
            except ValueError:
                continue
    return sorted(pending)


def _template_anchor_review_pending_pages(sess: TemplateSession) -> list[int]:
    """Pages whose model anchor/exemption draft still needs teacher approval.

    Unlike a column-layout review, this marker is independent of the frozen
    column crop plan.  A page can have trusted column boundaries while its
    question-number or exemption boxes remain non-authoritative candidates.
    """
    pending: set[int] = set()
    for run in (sess.layout_cut_runs or {}).values():
        if not isinstance(run, dict):
            continue
        for page_label, page_run in (run.get("pages") or {}).items():
            anchor_review_pending, _ = _template_geometry_review_state(page_run)
            if not anchor_review_pending:
                continue
            try:
                pending.add(int(str(page_label).removeprefix("page_")) - 1)
            except ValueError:
                continue
    return sorted(pending)


def _template_geometry_review_pending_pages(sess: TemplateSession) -> list[int]:
    """Return every page blocked on layout, anchor, or OCR geometry review."""
    pending = set(_template_layout_review_pending_pages(sess))
    pending.update(_template_anchor_review_pending_pages(sess))
    for run in (sess.layout_cut_runs or {}).values():
        if not isinstance(run, dict):
            continue
        for page_label, page_run in (run.get("pages") or {}).items():
            _, geometry_review_pending = _template_geometry_review_state(page_run)
            if not geometry_review_pending:
                continue
            try:
                pending.add(int(str(page_label).removeprefix("page_")) - 1)
            except ValueError:
                continue
    return sorted(pending)


def _frozen_question_layout(
    sess: TemplateSession,
    page_label: str,
    artifact: dict,
) -> tuple[dict, dict] | None:
    """Return the layout and plan already bound to this artifact, if any."""
    raw_plan = artifact.get("question_crop_plan")
    if not isinstance(raw_plan, dict):
        return None
    artifact_sha = str(artifact.get("sha256") or "")
    plan = validate_anchor_crop_plan(raw_plan, artifact_sha256=artifact_sha)
    matching: list[tuple[str, dict]] = []
    for run in (sess.layout_cut_runs or {}).values():
        if not isinstance(run, dict) or run.get("status") != "complete":
            continue
        page_run = (run.get("pages") or {}).get(page_label)
        if not isinstance(page_run, dict) or page_run.get("artifact_sha256") != artifact_sha:
            continue
        mode = (page_run.get("modes") or {}).get("question")
        if not isinstance(mode, dict):
            continue
        if str((mode.get("source") or {}).get("sha256") or "") != artifact_sha:
            continue
        stored_plan = page_run.get("anchor_crop_plan")
        if isinstance(stored_plan, dict):
            try:
                stored_plan = validate_anchor_crop_plan(
                    stored_plan,
                    artifact_sha256=artifact_sha,
                )
            except (AnchorCropPlanError, TypeError, ValueError):
                continue
            if stored_plan != plan:
                continue
        matching.append((
            str(run.get("completed_at") or run.get("created_at") or ""),
            mode,
        ))
    if matching:
        _timestamp, mode = max(matching, key=lambda item: item[0])
        return mode, plan
    # Preserve older sessions that persisted the plan before layout metadata.
    return _layout_result_from_frozen_question_plan(artifact, plan), plan


def _template_recognition_status_payload(sess: TemplateSession) -> dict:
    status = str(sess.status or "pending")
    error_code = str(sess.error_code or "")
    error = str(sess.error or "")
    if status == "recognizing" and sess.recognition_deadline_at:
        try:
            deadline = datetime.fromisoformat(sess.recognition_deadline_at)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= deadline.astimezone(timezone.utc):
                status = "failed"
                error_code = "TEMPLATE_IMPORT_TIMEOUT"
                error = "模板导入已超过服务器总时限，本次模板不可用；请重新发起导入"
        except (TypeError, ValueError):
            status = "failed"
            error_code = "TEMPLATE_SESSION_INVALID"
            error = "模板任务状态损坏，本次模板不可用；请重新发起导入"

    ready = status in {"ocr_done", "confirmed", "frozen"}
    terminal = ready or status == "failed"
    return {
        "success": ready,
        "template_id": sess.template_id,
        "teaching_context": copy.deepcopy(sess.teaching_context),
        "template_mode": sess.template_mode,
        "column_mode": getattr(sess, "column_mode", "double") or "double",
        "status": status,
        "terminal": terminal,
        "page_count": len(sess.pages),
        "pages": sess.pages if ready else [],
        "category": sess.category,
        "exam_name": sess.exam_name,
        "error_code": error_code,
        "error": error,
        "error_info": getattr(sess, "error_info", {}),
        "message": (
            "模板识别已完成"
            if ready
            else error
            if terminal
            else "模板仍在服务器处理中，请勿重复提交"
        ),
        "recognition_started_at": sess.recognition_started_at,
        "recognition_deadline_at": sess.recognition_deadline_at,
        "retry_after_ms": 1000 if not terminal else 0,
    }


@router.get("/batch-grade/template/{template_id}/recognition")
def batch_grade_template_recognition_status(
    template_id: str,
    user: User = Depends(_require_user),
):
    """Read a persisted template result without requiring model readiness."""
    sess = _load_owned_template_session(template_id, user.id)
    if not os.path.isfile(sess.meta_path):
        raise HTTPException(404, detail="模板任务不存在")
    return _template_recognition_status_payload(sess)


def _parse_template_page_roles(
    page_roles: object,
    page_labels: object,
    page_count: int,
) -> tuple[list[str], list[str]]:
    """Validate the teacher's per-page role and printed-label choice at upload.

    Both fields are optional JSON arrays with one entry per uploaded page. An
    omitted role list means every page is an ordinary ``answer_page``.
    """
    def parse(raw: object, name: str) -> list | None:
        # Direct Python callers receive FastAPI's ``Form`` marker when omitted.
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            value = _json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"{name} 必须是 JSON 数组") from exc
        if not isinstance(value, list) or len(value) != page_count:
            raise ValueError(f"{name} 数量必须与模板页数一致")
        return value

    raw_roles = parse(page_roles, "page_roles")
    raw_labels = parse(page_labels, "page_labels")
    roles = (
        [normalize_page_role(value) for value in raw_roles]
        if raw_roles is not None else [normalize_page_role("")] * page_count
    )
    labels = (
        [normalize_page_label(value) for value in raw_labels]
        if raw_labels is not None else [""] * page_count
    )
    if not any(role in GRADED_PAGE_ROLES for role in roles):
        raise ValueError("模板至少需要一页答题页或答题卡")
    return roles, labels


@dataclass(frozen=True)
class _TemplateImportForm:
    """Validated fields of a template upload form."""

    template_mode: str
    column_mode: str
    category: str
    exam_name: str
    page_roles: list[str]
    page_labels: list[str]
    # Parsed metadata of client canonical candidates, or None without them.
    canonical_metadata: list | None


def _parse_template_import_form(
    files: list[UploadFile],
    canonical_files: list[UploadFile] | None,
    canonical_metadata: str | None,
    *,
    has_answers: bool,
    template_mode: str,
    column_mode: str,
    layout_mode: str,
    category: str,
    exam_name: str,
    page_roles: str,
    page_labels: str,
) -> _TemplateImportForm:
    """Validate the upload form before anything is stored."""
    if not files:
        raise HTTPException(400, detail="未上传模板图片")
    supplied_column_modes = [
        value for value in (column_mode, layout_mode)
        if isinstance(value, str) and value.strip()
    ]
    try:
        normalized_column_modes = [
            normalize_column_mode(value) for value in supplied_column_modes
        ]
    except ValueError as exc:
        raise HTTPException(400, detail="column_mode 只能是 single、double 或 triple") from exc
    if len(set(normalized_column_modes)) > 1:
        raise HTTPException(400, detail="column_mode 与 layout_mode 参数冲突")
    resolved_column_mode = normalized_column_modes[0] if normalized_column_modes else "double"
    # Direct Python callers (tests, task replayers and older integrations)
    # receive FastAPI's ``Form`` marker when they omit this argument.  Treat
    # that marker as absent instead of serializing it into an invalid mode.
    raw_template_mode = (
        template_mode.strip().lower()
        if isinstance(template_mode, str)
        else ""
    )
    if raw_template_mode:
        if raw_template_mode not in TEMPLATE_MODES:
            raise HTTPException(400, detail="template_mode 只能是 blank 或 marked")
        resolved_template_mode = raw_template_mode
    else:
        # Older clients sent only has_answers.  Preserve their visual intent
        # while requiring all new clients to send the explicit mode.
        legacy_has_answers = (
            has_answers if isinstance(has_answers, bool) else True
        )
        resolved_template_mode = "marked" if legacy_has_answers else "blank"
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
            detail=f"单次最多上传 {MAX_TEMPLATE_PAGES} 页模板原图",
        )
    submitted_metadata = None
    if has_canonical_files:
        if len(canonical_files) != len(files):
            raise HTTPException(422, detail="标准模板候选数量必须与原图一致")
        if any(
            os.path.splitext(upload.filename or "")[1].lower() != ".png"
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
    if category not in VALID_GRADING_CATEGORIES:
        raise HTTPException(400, detail="category 必须是 daily 或 exam")
    exam_name = str(exam_name or "").strip()
    if category == "exam" and not exam_name:
        raise HTTPException(400, detail="考试模板必须填写考试名称")
    try:
        resolved_page_roles, resolved_page_labels = _parse_template_page_roles(
            page_roles, page_labels, len(files),
        )
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    return _TemplateImportForm(
        template_mode=resolved_template_mode,
        column_mode=resolved_column_mode,
        category=category,
        exam_name=exam_name,
        page_roles=resolved_page_roles,
        page_labels=resolved_page_labels,
        canonical_metadata=submitted_metadata,
    )


@dataclass
class _TemplateImport:
    """One running template import: its session, saved pages and deadline."""

    sess: TemplateSession
    teacher_id: int
    column_mode: str
    page_roles: list[str]
    page_labels: list[str]
    paths: list[str]
    # (original, client canonical candidate, metadata) per page when the
    # desktop client submitted canonical candidates, otherwise None.
    canonical_inputs: list[tuple[str, str, dict]] | None
    candidate_dir: str
    deadline_s: float
    deadline_at: float

    @property
    def total(self) -> int:
        return len(self.paths)

    @property
    def graded_page_count(self) -> int:
        # Only graded pages run layout/model/artifact work and join the import run.
        return sum(role in GRADED_PAGE_ROLES for role in self.page_roles)

    @property
    def layout_action_id(self) -> str:
        return f"template-import-{self.sess.template_id}"


async def _save_template_import(
    sess: TemplateSession,
    teacher_id: int,
    form: _TemplateImportForm,
    files: list[UploadFile],
    canonical_files: list[UploadFile] | None,
    deadline_s: float,
) -> _TemplateImport:
    """Store the uploaded pages; any failure removes the whole template dir."""
    candidate_dir = os.path.join(sess.template_dir, "_submitted_canonical")
    canonical_inputs = None
    try:
        saved_uploads = await _save_batch_uploads(files, sess.template_dir)
        deadline_at = _template_import_clock() + deadline_s
        if form.canonical_metadata is not None:
            os.makedirs(candidate_dir, exist_ok=True)
            saved_candidates = await _save_batch_uploads(
                canonical_files,
                candidate_dir,
                total_byte_budget=(
                    MAX_UPLOAD_TOTAL_SIZE
                    - sum(os.path.getsize(path) for _, path in saved_uploads)
                ),
            )
            canonical_inputs = [
                (source_path, candidate_path, metadata)
                for ((_, source_path), (_, candidate_path), metadata) in zip(
                    saved_uploads,
                    saved_candidates,
                    form.canonical_metadata,
                )
            ]
    except ValueError as exc:
        shutil.rmtree(sess.template_dir, ignore_errors=True)
        raise HTTPException(
            422,
            detail="客户端标准模板候选校验失败",
        ) from exc
    except (asyncio.CancelledError, Exception):
        shutil.rmtree(sess.template_dir, ignore_errors=True)
        raise
    return _TemplateImport(
        sess=sess,
        teacher_id=teacher_id,
        column_mode=form.column_mode,
        page_roles=form.page_roles,
        page_labels=form.page_labels,
        paths=[path for _, path in saved_uploads],
        canonical_inputs=canonical_inputs,
        candidate_dir=candidate_dir,
        deadline_s=deadline_s,
        deadline_at=deadline_at,
    )


@router.post("/batch-grade/template/recognize")
async def batch_grade_template_recognize(
    files: list[UploadFile] = File(...),
    canonical_files: list[UploadFile] | None = File(None),
    canonical_metadata: str | None = Form(None),
    has_answers: bool = Form(True),
    template_mode: str = Form(""),
    column_mode: str = Form("double"),
    layout_mode: str = Form(""),
    category: str = Form(...),
    exam_name: str = Form(""),
    page_roles: str = Form(""),
    page_labels: str = Form(""),
    user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    """模板阶段一：OCR N 张模板图（N=试卷页数），返回逐页题目供老师确认（SSE）。

    ``template_mode`` 是模板区域合同的一部分：blank 由视觉模型定位空白
    模板的印刷作答区域，marked 定位已有手写豁免区域；两种模式都复用
    同一份题号位置。两种模式都由独立数学模型生成标准答案；``has_answers``
    仅为旧客户端保留，不决定标准答案。

    SSE events:
      - progress: {message, current, total}
      - page:     {page_index, question_count}
      - done:     {template_id, page_count, pages:[...]}
      - error:    {message}
    """
    form = _parse_template_import_form(
        files,
        canonical_files,
        canonical_metadata,
        has_answers=has_answers,
        template_mode=template_mode,
        column_mode=column_mode,
        layout_mode=layout_mode,
        category=category,
        exam_name=exam_name,
        page_roles=page_roles,
        page_labels=page_labels,
    )
    teaching_context = make_teaching_context(require_teacher_stage(user))
    await _require_template_recognition_chain(user)

    teacher_id = int(user.id)
    template_id = uuid.uuid4().hex[:12]
    sess = _new_template_session(template_id, teacher_id, teaching_context)
    # Keep the legacy field readable for old clients, but normalize new sessions
    # to the generated-answer semantics. No new code may use it to decide how
    # template OCR or differential evidence is produced.
    sess.has_answers = False
    sess.template_mode = normalize_template_mode(
        form.template_mode,
        default="marked",
    )
    sess.column_mode = form.column_mode
    sess.category = form.category
    sess.exam_name = form.exam_name
    os.makedirs(sess.template_dir, exist_ok=True)

    job = await _save_template_import(
        sess,
        teacher_id,
        form,
        files,
        canonical_files,
        _template_import_deadline_seconds(len(files)),
    )
    recognition_started = datetime.now(timezone.utc)
    sess.status = "recognizing"
    sess.recognition_started_at = recognition_started.isoformat()
    sess.recognition_deadline_at = (
        recognition_started + timedelta(seconds=job.deadline_s)
    ).isoformat()
    try:
        sess.save_meta()
    except Exception:
        shutil.rmtree(sess.template_dir, ignore_errors=True)
        raise

    events = _TemplateRecognitionEventBuffer()
    recognition_task = asyncio.create_task(
        _relay_template_recognition(
            stream_teaching(sess.teaching_context, _template_import_events(job)),
            events,
            sess,
        ),
        name=f"template-recognition-{template_id}",
    )
    _track_template_recognition_task(recognition_task)
    return StreamingResponse(
        events.subscribe(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Template-ID": template_id,
        },
    )


class _TemplateImportSteps:
    """The one slow step a template import is waiting on.

    ``start`` registers the step's task. ``poll`` waits one heartbeat interval,
    raising once the import deadline has passed, and returns True when the step
    is done, leaving its value in ``result``. ``settle`` stops a step that is
    still running when the import ends early.
    """

    def __init__(self, deadline_at: float) -> None:
        self.deadline_at = deadline_at
        self.task: asyncio.Task | None = None
        self.result = None

    def start(self, task: asyncio.Task) -> None:
        self.task = task

    async def poll(self) -> bool:
        complete, result = await _poll_template_component(
            self.task,
            deadline_at=self.deadline_at,
        )
        if complete:
            self.task = None
            self.result = result
        return complete

    async def settle(self) -> None:
        if self.task is not None:
            await _settle_template_component(self.task, cancel_running=True)


def _template_heartbeat(index: int, total: int, activity: str) -> dict:
    return {
        "message": f"第 {index + 1} 页正在{activity}",
        "current": index + 1,
        "total": total,
        "heartbeat": True,
    }


@dataclass
class _RecognizedTemplatePage:
    """One page's model output, split into trusted geometry and candidates."""

    questions: list[dict]
    layout_result: dict | None
    anchor_diagnostics: dict
    template_ocr_diagnostics: dict
    anchor_review_pending: bool
    geometry_review_pending: bool
    # Geometry frozen into the artifact; None while the page awaits review.
    question_anchors: list | None
    template_answer_regions: list | None
    template_handwriting_regions: list | None
    # Model geometry shown to the teacher as editable candidates.
    question_anchor_candidates: list
    template_answer_region_candidates: list
    template_handwriting_region_candidates: list

    def artifact_questions(self) -> list[dict]:
        questions = copy.deepcopy(self.questions)
        if self.geometry_review_pending:
            # Keep incomplete drawing geometry available to the editor,
            # but never treat it as a frozen/renderable drawing schema.
            for question in questions:
                if question.get("answer_kind") in {"drawing", "mixed"} and (
                    not question.get("drawing_regions") or not question.get("label_anchor")
                ):
                    question.update(answer_kind="non_graphic", drawing_regions=[], label_anchor=None)
        return questions


def _recognized_template_page(
    ocr: dict,
    *,
    template_id: str,
    page_index: int,
    question_offset: int,
    layout_result: dict | None,
    layout_review_pending: bool,
) -> tuple[dict, _RecognizedTemplatePage]:
    """Validate one page's model output and number it within the whole paper.

    Returns the page result with merged question numbers and the split of its
    geometry. A failed or empty page raises a teacher-facing failure.
    """
    raw_error = str(ocr.get("error") or "").strip()
    questions = ocr.get("questions") or []
    anchor_diagnostics = (
        ocr.get("anchor_diagnostics")
        if isinstance(ocr.get("anchor_diagnostics"), dict)
        else {}
    )
    template_ocr_diagnostics = (
        ocr.get("template_ocr_diagnostics")
        if isinstance(ocr.get("template_ocr_diagnostics"), dict)
        else {}
    )
    anchor_review_pending = (
        anchor_diagnostics.get("needs_review") is True
    )
    template_ocr_review_pending = (
        template_ocr_diagnostics.get("needs_review") is True
    )
    geometry_review_pending = bool(
        anchor_review_pending or template_ocr_diagnostics.get(
            "geometry_review_pending", template_ocr_review_pending
        ) is True
    )
    # A semantic anchor conflict is recoverable only because the
    # transcription and generated answers are still complete.  An
    # empty page or any other provider error remains fatal.
    if not questions or (
        raw_error
        and not (anchor_review_pending or template_ocr_review_pending)
    ):
        if ocr.get("error_info"):
            raise failure_from_public(ocr["error_info"])
        raise _template_recognition_failure(raw_error)
    # Merge step: the model numbered this page from 1 on its own.
    # Shift it past every earlier page so question numbers, slot
    # ids and directory nodes are unique across the whole paper.
    try:
        ocr = globalize_page_numbers(
            ocr,
            question_offset,
            page_index=page_index,
        )
    except ValueError as exc:
        raise _TemplateRecognitionFailure(
            "TEMPLATE_OUTPUT_INVALID",
            "模板题号无法合并为整卷连续编号，请重新导入模板",
        ) from exc
    questions = ocr["questions"]
    # Their pending-review lists now name the merged numbers.
    if isinstance(ocr.get("anchor_diagnostics"), dict):
        anchor_diagnostics = ocr["anchor_diagnostics"]
    if isinstance(ocr.get("template_ocr_diagnostics"), dict):
        template_ocr_diagnostics = ocr["template_ocr_diagnostics"]
    if layout_review_pending:
        # The whole-page fallback only organised this page's model
        # requests. The teacher sees and edits the original review
        # draft; no column plan is frozen until they save one.
        layout_result = dict(layout_result or {})
        layout_result["layout_review_pending"] = True
        layout_result["ocr_input_strategy"] = "whole_page_fallback"
    else:
        layout_result = ocr.get("layout_result")
    if not geometry_review_pending:
        # A model box the teacher can fix, such as one larger than
        # a quarter of the page, must not fail the import: the page
        # opens for teacher review with the model geometry as
        # editable candidates, and the editor names the problem.
        trusted_regions = ocr.get("template_answer_regions")
        if not isinstance(trusted_regions, list):
            trusted_regions = ocr.get("template_handwriting_regions")
        if isinstance(trusted_regions, list):
            try:
                normalize_template_handwriting_regions(trusted_regions)
            except ValueError as exc:
                logger.warning(
                    "模板第 %s 页模型框未通过校验，转教师复核 template=%s: %s",
                    page_index + 1, template_id, exc,
                )
                geometry_review_pending = True
    raw_anchor_candidates = ocr.get("question_anchor_candidates")
    if not isinstance(raw_anchor_candidates, list):
        raw_anchor_candidates = (
            ocr.get("question_anchors")
            if geometry_review_pending
            and isinstance(ocr.get("question_anchors"), list)
            else []
        )
    raw_handwriting_candidates = ocr.get(
        "template_handwriting_region_candidates"
    )
    if not isinstance(raw_handwriting_candidates, list):
        raw_handwriting_candidates = (
            ocr.get("template_handwriting_regions")
            if geometry_review_pending
            and isinstance(
                ocr.get("template_handwriting_regions"),
                list,
            )
            else []
        )
    raw_answer_candidates = ocr.get(
        "template_answer_region_candidates"
    )
    if not isinstance(raw_answer_candidates, list):
        raw_answer_candidates = (
            ocr.get("template_answer_regions")
            if geometry_review_pending
            and isinstance(ocr.get("template_answer_regions"), list)
            else []
        )
    if geometry_review_pending:
        # Candidates are intentionally excluded from the artifact:
        # it is consumed by student OCR and may contain only trusted
        # model geometry or server-stamped teacher overrides.
        question_anchors = None
        template_answer_regions = None
        template_handwriting_regions = None
    else:
        question_anchors = ocr.get("question_anchors")
        if question_anchors is not None:
            question_anchors = question_anchors or []
        raw_answer_regions = ocr.get("template_answer_regions")
        if not isinstance(raw_answer_regions, list):
            # Migrate one legacy response at the boundary; its
            # handwriting boxes were the only region contract.
            raw_answer_regions = ocr.get("template_handwriting_regions")
        template_answer_regions = (
            normalize_template_handwriting_regions(raw_answer_regions)
            if isinstance(raw_answer_regions, list) else None
        )
        # One unified answer-box collection serves both roles:
        # the student side marks its left Q and locates the
        # answer, and the diff whitens the same boxes on the
        # registered template baseline. Never drop this half for
        # the legacy blank label; templates that carry
        # handwriting would diff the student answers away.
        template_handwriting_regions = (
            copy.deepcopy(template_answer_regions)
            if template_answer_regions is not None else None
        )
    return ocr, _RecognizedTemplatePage(
        questions=questions,
        layout_result=layout_result,
        anchor_diagnostics=anchor_diagnostics,
        template_ocr_diagnostics=template_ocr_diagnostics,
        anchor_review_pending=anchor_review_pending,
        geometry_review_pending=geometry_review_pending,
        question_anchors=question_anchors,
        template_answer_regions=template_answer_regions,
        template_handwriting_regions=template_handwriting_regions,
        question_anchor_candidates=list(raw_anchor_candidates),
        template_answer_region_candidates=list(raw_answer_candidates),
        template_handwriting_region_candidates=list(raw_handwriting_candidates),
    )


def _record_template_import_layout(
    job: _TemplateImport,
    page_index: int,
    page: _RecognizedTemplatePage,
) -> None:
    """Add this page's column layout to the import's automatic layout run."""
    sess = job.sess
    page_label = f"page_{page_index + 1}"
    sess.layout_candidates[page_label] = page.layout_result
    auto_run = sess.layout_cut_runs.setdefault(job.layout_action_id, {
        "action_id": job.layout_action_id,
        "teacher_id": job.teacher_id,
        "status": "running",
        "cut_types": ["question"],
        "image_type": "template",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "pages": {},
        "error": None,
    })
    page_run = {
        "page_index": page_index,
        "modes": {"question": page.layout_result},
    }
    if page.anchor_diagnostics:
        page_run["anchor_diagnostics"] = page.anchor_diagnostics
    if page.template_ocr_diagnostics:
        page_run["template_ocr_diagnostics"] = (
            page.template_ocr_diagnostics
        )
    if page.geometry_review_pending:
        page_run["geometry_review_pending"] = True
        page_run["question_schema_candidates"] = [
            {"question_number": str(question.get("question_number") or question.get("question_no")),
             "question_type": question.get("question_type", "solve"),
             "choice_mode": question.get("choice_mode", "unknown"),
             **AnswerSpec.model_validate(question).directory_metadata()}
            for question in page.questions
        ]
        page_run["question_anchor_candidates"] = (
            page.question_anchor_candidates
        )
        page_run["template_answer_region_candidates"] = (
            page.template_answer_region_candidates
        )
        page_run["template_handwriting_region_candidates"] = (
            page.template_handwriting_region_candidates
        )
    if page.anchor_review_pending:
        page_run["anchor_review_pending"] = True
    auto_run["pages"][page_label] = page_run


def _bind_template_import_artifact(
    job: _TemplateImport,
    page_index: int,
    questions: list[dict],
    artifact: dict,
) -> None:
    """Store a page's frozen artifact and its crop plan, when the layout allows."""
    sess = job.sess
    page_label = f"page_{page_index + 1}"
    sess.page_artifacts[page_label] = artifact
    auto_run = sess.layout_cut_runs.get(job.layout_action_id)
    page_run = (auto_run or {}).get("pages", {}).get(page_label)
    layout_result = (page_run or {}).get("modes", {}).get("question")
    if isinstance(layout_result, dict) and layout_needs_review(layout_result):
        # A review draft has no trusted columns, so no crop plan is
        # frozen for it. Bind the artifact so the confirmation page
        # can show the draft; the teacher's saved boundaries create
        # the first plan through the layout-overrides endpoint.
        page_run["artifact_sha256"] = artifact["sha256"]
        page_run["anchor_crop_plan"] = None
        page_run["layout_review_pending"] = True
    elif isinstance(layout_result, dict):
        expected_numbers = _question_numbers(questions)
        plan = build_anchor_crop_plan(
            layout_result,
            artifact_sha256=artifact["sha256"],
            expected_question_numbers=expected_numbers,
        )
        artifact_with_plan = dict(artifact)
        artifact_with_plan["question_crop_plan"] = plan
        artifact = verify_template_artifacts(
            sess.template_dir,
            {page_label: artifact_with_plan},
        )[page_label]
        sess.page_artifacts[page_label] = artifact
        page_run["artifact_sha256"] = artifact["sha256"]
        page_run["anchor_crop_plan"] = plan
    else:
        return
    if len(auto_run["pages"]) == job.graded_page_count:
        auto_run["status"] = "complete"
        auto_run["completed_at"] = datetime.now(timezone.utc).isoformat()
        sess.layout_candidates = {
            "active_action_id": job.layout_action_id,
            "updated_at": auto_run["completed_at"],
        }


async def _fail_template_import(
    sess: TemplateSession,
    error: BaseException,
    *,
    code: str,
    message: str = "",
    kind: str,
) -> str:
    """Persist a failed import and return the teacher-facing SSE error."""
    sess.error_info = report_failure(error, stage="template", task_id=sess.template_id)
    message = message or sess.error_info["message"]
    sess.status = "failed"
    sess.error_code = code
    sess.error = message
    try:
        await asyncio.to_thread(sess.save_meta)
    except Exception:
        logger.exception("模板%s状态保存失败 template=%s", kind, sess.template_id)
    return _sse("error", {"code": code, "message": message, "error_info": sess.error_info})


@dataclass
class _TemplatePageImport:
    """One uploaded template page as the import works through its phases."""

    index: int
    path: str
    role: str
    identity: dict | None = None
    canonical: CanonicalTemplate | None = None
    layout_result: dict | None = None
    layout_review_pending: bool = False
    # What the model stages read: a whole-page fallback while the proposed
    # column layout waits for the teacher's review.
    model_layout: dict | None = None
    model_output: dict | None = None
    recognized: _RecognizedTemplatePage | None = None
    artifact_questions: list[dict] | None = None


async def _correct_template_page_events(
    job: _TemplateImport,
    steps: _TemplateImportSteps,
    page: _TemplatePageImport,
):
    """Accept the client's canonical candidate or rectify the photo locally."""
    idx = page.index
    if job.canonical_inputs is not None:
        source_path, candidate_path, metadata = job.canonical_inputs[idx]
        try:
            steps.start(asyncio.create_task(_run_template_cpu_component(
                accept_canonical_template_candidate,
                source_path,
                candidate_path,
                metadata,
                job.sess.template_dir,
                idx,
            )))
            while not await steps.poll():
                yield _sse("progress", _template_heartbeat(idx, job.total, "校验标准模板候选"))
        except ValueError as exc:
            raise _TemplateRecognitionFailure(
                "TEMPLATE_CANONICAL_CANDIDATE_INVALID",
                "客户端标准模板候选校验失败；本次模板不可用，请重新导入",
            ) from exc
        page.canonical = steps.result
        return
    try:
        steps.start(asyncio.create_task(_run_template_cpu_component(
            canonicalize_template_image,
            page.path,
            job.sess.template_dir,
            idx,
        )))
        while not await steps.poll():
            yield _sse("progress", _template_heartbeat(idx, job.total, "本地完成页面矫正"))
        canonical = steps.result
        if (
            not isinstance(canonical, CanonicalTemplate)
            or not canonical.image_path
            or not isinstance(canonical.canonicalization, dict)
        ):
            raise ValueError("local canonicalization result is invalid")
    except _TemplateImportDeadlineExceeded:
        raise
    except Exception as exc:
        raise _TemplateRecognitionFailure(
            "TEMPLATE_CANONICALIZATION_FAILED",
            "模板页面矫正失败，请确认图片边界完整、方向正确且清晰后重新导入",
        ) from exc
    page.canonical = canonical


async def _template_page_identity(job: _TemplateImport, page: _TemplatePageImport) -> dict:
    """The printed label the teacher gave the page, plus its visual fingerprint."""
    identity = {"printed_page_label": job.page_labels[page.index]}
    try:
        identity["visual_fingerprint"] = await asyncio.to_thread(
            compute_visual_fingerprint, page.canonical.image_path,
        )
    except (OSError, ValueError):
        # Routing keeps its number/label gates without this hint.
        logger.warning(
            "模板页视觉指纹生成失败 template=%s page=%s", job.sess.template_id, page.index + 1,
        )
    return identity


async def _template_layout_events(
    job: _TemplateImport,
    steps: _TemplateImportSteps,
    page: _TemplatePageImport,
):
    """Propose the page's column layout.

    A low-confidence or rejected proposal stays a teacher-review draft: the
    model stages fall back to one whole-page input instead of unverified
    crops, and confirmation is refused until the teacher saves the boundaries.
    The template is not failed.
    """
    try:
        steps.start(asyncio.create_task(recognize_column_layout(
            page.canonical.image_path,
            teacher_id=str(job.teacher_id),
            column_mode=job.column_mode,
        )))
        while not await steps.poll():
            yield _sse("progress", _template_heartbeat(page.index, job.total, "识别题目布局"))
        page.layout_result = steps.result
        page.layout_review_pending = layout_needs_review(page.layout_result)
        page.model_layout = (
            whole_page_fallback_layout(page.layout_result)
            if page.layout_review_pending
            else page.layout_result
        )
    except (asyncio.CancelledError, _TemplateImportDeadlineExceeded):
        # The deadline is a TimeoutError, i.e. an OSError: report it
        # as the import timeout, not as a crop failure.
        raise
    except (TemplateCutError, AnchorCropPlanError, TemplateEvidenceError, OSError, ValueError) as exc:
        raise _TemplateRecognitionFailure(
            "TEMPLATE_CROP_FAILED",
            "模板分栏裁剪失败，已停止整页 OCR，请重新导入清晰完整的模板",
        ) from exc


async def _template_model_events(
    job: _TemplateImport,
    steps: _TemplateImportSteps,
    page: _TemplatePageImport,
):
    """Run the model stages under this page's share of the import deadline.

    Transcription, directory, generated answers and drawing positions; each
    heartbeat reports the stages' progress.
    """
    from services.template_directory_pipeline import template_import_context

    sess = job.sess
    idx = page.index
    model_progress = {
        "trace_id": hashlib.sha256(sess.template_id.encode()).hexdigest()[:12] + f"p{idx + 1}",
        "stages": {},
    }
    operation = recognize_template_pipeline(
        page.canonical.image_path,
        template_mode=sess.template_mode,
        # Review-pending pages send one whole-page input; the
        # teacher-facing review draft is persisted separately.
        layout_result=page.model_layout,
        on_text_ready=lambda transcription: template_knowledge.start_page_classification(
            sess, idx, transcription, classifier=_template_knowledge_classifier,
        ),
    )
    # The task copies the teacher ids and the deadline context when created.
    vision_token = set_vision_teacher_id(str(job.teacher_id))
    grading_token = set_grading_teacher_id(str(job.teacher_id))
    try:
        with template_import_context(
            deadline_at=_template_page_model_deadline(job.deadline_at, job.total - idx),
            progress=model_progress,
        ):
            steps.start(asyncio.create_task(
                await_teaching(sess.teaching_context, operation)
            ))
    finally:
        reset_grading_teacher_id(grading_token)
        reset_vision_teacher_id(vision_token)
    while not await steps.poll():
        yield _sse("progress", {
            "message": _template_model_progress_message(idx, model_progress),
            "current": idx + 1,
            "total": job.total,
            "heartbeat": True,
            "model_stages": copy.deepcopy(model_progress["stages"]),
        })
    page.model_output = steps.result
    yield _sse("progress", {
        "message": f"第 {idx + 1} 页模型阶段已结束，正在保存识别草稿",
        "current": idx + 1, "total": job.total,
        "model_stages": copy.deepcopy(model_progress["stages"]),
    })


async def _save_recognized_template_page(job: _TemplateImport, page: _TemplatePageImport) -> None:
    """Save the recognized page before a later geometry stage or the connection can fail."""
    sess = job.sess
    idx = page.index
    model_output, recognized = _recognized_template_page(
        page.model_output,
        template_id=sess.template_id,
        page_index=idx,
        question_offset=sum(len(saved.get("questions") or []) for saved in sess.pages),
        layout_result=page.layout_result,
        layout_review_pending=page.layout_review_pending,
    )
    page.identity["printed_question_labels"] = [
        str((question.get("printed_path") or [""])[-1] or "")
        for question in recognized.questions
    ]
    sess.add_page(idx, recognized.questions, page_role=page.role, page_identity=page.identity)
    try:
        template_knowledge.bind_page_knowledge(sess, idx, recognized.questions)
    except Exception as exc:
        logger.warning("模板知识点绑定暂缓 template=%s: %s", sess.template_id, type(exc).__name__)
    if isinstance(model_output.get("question_directory"), dict):
        sess.question_directories[f"page_{idx + 1}"] = model_output["question_directory"]
    await asyncio.to_thread(sess.save_meta)
    page.recognized = recognized
    page.artifact_questions = recognized.artifact_questions()
    if recognized.layout_result is not None:
        _record_template_import_layout(job, idx, recognized)
        await asyncio.to_thread(sess.save_meta)


async def _template_page_events(
    job: _TemplateImport,
    steps: _TemplateImportSteps,
    page: _TemplatePageImport,
):
    """Correct, lay out, recognize and save one template page.

    A page the teacher declared ungraded is only a routing target: it is
    saved after correction, with no layout, model or artifact work.
    """
    sess = job.sess
    idx = page.index
    yield _sse("progress", {"message": f"识别第 {idx + 1} 页", "current": idx + 1, "total": job.total})
    async for event in _correct_template_page_events(job, steps, page):
        yield event
    page.identity = await _template_page_identity(job, page)
    if page.role not in GRADED_PAGE_ROLES:
        sess.add_page(idx, [], page_role=page.role, page_identity=page.identity)
        await asyncio.to_thread(sess.save_meta)
        yield _sse("page", {"page_index": idx, "question_count": 0, "page_role": page.role})
        return
    async for event in _template_layout_events(job, steps, page):
        yield event
    async for event in _template_model_events(job, steps, page):
        yield event
    await _save_recognized_template_page(job, page)
    yield _sse("page", {"page_index": idx, "question_count": len(page.recognized.questions)})


async def _freeze_template_page_events(
    job: _TemplateImport,
    steps: _TemplateImportSteps,
    page: _TemplatePageImport,
):
    """Build the page's template artifact from its recognized geometry.

    Blank templates store model-marked printed answer areas; marked templates
    store explicit handwriting exemptions. Neither mode reuses another
    batch's coordinates or treats student pixels as template authority.
    """
    sess = job.sess
    recognized = page.recognized
    try:
        steps.start(asyncio.create_task(_run_template_cpu_component(
            build_template_artifact,
            page.canonical.image_path,
            sess.template_dir,
            page.index,
            page.artifact_questions,
            blank_detection_enabled=False,
            canonicalization=page.canonical.canonicalization,
            question_anchors=recognized.question_anchors,
            template_answer_regions=recognized.template_answer_regions,
            template_mode=sess.template_mode,
            # Both legacy modes persist the model's complete
            # question-to-exemption contract. An empty regions
            # list is the explicit no-handwriting result.
            template_handwriting_regions=recognized.template_handwriting_regions,
        )))
        while not await steps.poll():
            yield _sse("progress", _template_heartbeat(page.index, job.total, "生成矫正与裁剪基准"))
    except _TemplateImportDeadlineExceeded:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise _TemplateRecognitionFailure(
            "TEMPLATE_LAYOUT_FAILED",
            "本次模板的答案区域定位失败，请确认图片完整清晰后重新导入",
        ) from exc
    _bind_template_import_artifact(job, page.index, page.artifact_questions, steps.result)
    await asyncio.to_thread(sess.save_meta)


async def _finish_template_import(job: _TemplateImport) -> None:
    """Check whole-paper numbering, then mark the template recognized."""
    sess = job.sess
    if _template_import_clock() >= job.deadline_at:
        raise _TemplateImportDeadlineExceeded
    try:
        validate_global_numbering(sess.pages)
    except ValueError as exc:
        raise _TemplateRecognitionFailure(
            "TEMPLATE_OUTPUT_INVALID",
            "模板题号无法合并为整卷连续编号，请重新导入模板",
        ) from exc
    sess.status = "ocr_done"
    sess.error = None
    sess.error_code = ""
    sess.error_info = {}
    await asyncio.to_thread(sess.save_meta)


async def _template_import_events(job: _TemplateImport):
    """Recognize every uploaded template page, then freeze each page's geometry.

    Every slow step runs as a task tracked by ``steps``: the stream keeps sending
    heartbeats while it works, the whole import stays inside its deadline, and
    whatever is still running is stopped before the session is marked failed.
    Each completed stage is checkpointed off the event loop. The phase helpers
    hold no cleanup of their own, so they are iterated directly.
    """
    sess = job.sess
    template_id = sess.template_id
    steps = _TemplateImportSteps(job.deadline_at)
    try:
        deadline_minutes = max(1, (int(job.deadline_s) + 59) // 60)
        yield _sse("progress", {
            "message": (
                f"正在识别 {job.total} 页模板；本次总时限为 {deadline_minutes} 分钟，"
                "处理中会持续显示进度"
            ),
            "current": 0,
            "total": job.total,
            "template_id": template_id,
            "teaching_context": copy.deepcopy(sess.teaching_context),
        })
        graded_pages: list[_TemplatePageImport] = []
        for idx, path in enumerate(job.paths):
            page = _TemplatePageImport(index=idx, path=path, role=job.page_roles[idx])
            async for event in _template_page_events(job, steps, page):
                yield event
            if page.recognized is not None:
                graded_pages.append(page)
        # Every import owns and freezes its geometry, after all pages are saved.
        for page in graded_pages:
            async for event in _freeze_template_page_events(job, steps, page):
                yield event
        await _finish_template_import(job)
        yield _sse("done", {
            "template_id": template_id,
            "teaching_context": copy.deepcopy(sess.teaching_context),
            "page_count": len(sess.pages),
            "template_mode": sess.template_mode,
            "column_mode": getattr(sess, "column_mode", "double") or "double",
            "pages": sess.pages,
        })
    except _TemplateImportDeadlineExceeded:
        failure = _template_import_deadline_failure(job.deadline_s)
        yield await _fail_template_import(
            sess, failure, code=failure.code, message=failure.message, kind="超时",
        )
    except asyncio.CancelledError:
        logger.info("模板导入连接中断 template=%s", template_id)
        raise
    except _TemplateRecognitionFailure as exc:
        yield await _fail_template_import(
            sess, exc, code=exc.code, message=exc.message, kind="失败",
        )
    except Exception as exc:
        yield await _fail_template_import(sess, exc, code="TEMPLATE_IMPORT_FAILED", kind="故障")
    finally:
        await steps.settle()
        shutil.rmtree(job.candidate_dir, ignore_errors=True)
        if sess.status not in {"ocr_done", "failed"}:
            sess.status = "failed"
            sess.error_code = "TEMPLATE_IMPORT_INTERRUPTED"
            sess.error = (
                "模板导入连接已中断，所有在途步骤均已清理；"
                "本次模板不可用，请重新发起导入"
            )
            try:
                await asyncio.to_thread(sess.save_meta)
            except Exception:
                logger.exception("模板中断状态保存失败 template=%s", template_id)


@router.get("/batch-grade/template/{template_id}/cut-candidates")
def batch_grade_template_cut_candidates_get(
    template_id: str,
    user: User = Depends(_require_user),
):
    """Read the last complete, server-owned layout-candidate run."""
    sess = _load_owned_template_session(template_id, user.id)
    if sess.status == "pending":
        raise HTTPException(404, detail="模板会话不存在或已过期")
    active_action_id = str((sess.layout_candidates or {}).get("active_action_id") or "")
    run = (sess.layout_cut_runs or {}).get(active_action_id)
    latest = max(
        (
            candidate for candidate in (sess.layout_cut_runs or {}).values()
            if isinstance(candidate, dict)
        ),
        key=lambda candidate: str(candidate.get("created_at") or ""),
        default=None,
    )
    if isinstance(latest, dict) and latest.get("status") in {"running", "failed"}:
        run = latest
    if not isinstance(run, dict):
        return {
            "success": False,
            "template_id": template_id,
            "status": "not_run",
            "layout_review_pending_pages": (
                _template_layout_review_pending_pages(sess)
            ),
            "anchor_review_pending_pages": (
                _template_anchor_review_pending_pages(sess)
            ),
            "geometry_review_pending_pages": (
                _template_geometry_review_pending_pages(sess)
            ),
            "pages": [],
            "summary": {
                "total": 0,
                "original": 0,
                "rectified_untrusted": 0,
                "invalid": 0,
                "missing": 0,
            },
            "progress": {"completed": 0, "total": 0, "remaining": 0},
        }
    return _template_cut_response(sess, run)


@router.get("/batch-grade/template/{template_id}/pages/{page_index}/image")
def batch_grade_template_page_image(
    template_id: str,
    page_index: int,
    user: User = Depends(_require_user),
):
    """Return one immutable, teacher-owned canonical template page for crop review."""
    sess = _load_owned_template_session(template_id, user.id)
    if page_index < 0 or page_index >= len(sess.pages):
        raise HTTPException(404, detail="模板页面不存在")
    page_label = f"page_{page_index + 1}"
    try:
        artifact = verify_template_artifacts(
            sess.template_dir,
            {page_label: sess.page_artifacts.get(page_label)},
        )[page_label]
    except (TemplateEvidenceError, ValueError, TypeError) as exc:
        raise HTTPException(404, detail="模板页面不存在") from exc
    image_path = os.path.realpath(os.path.join(
        sess.template_dir,
        artifact["template_image"].replace("/", os.sep),
    ))
    template_root = os.path.realpath(sess.template_dir)
    if os.path.commonpath([template_root, image_path]) != template_root or not os.path.isfile(image_path):
        raise HTTPException(404, detail="模板页面不存在")
    return FileResponse(
        image_path,
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/batch-grade/template/{template_id}/cut-candidates")
async def batch_grade_template_cut_candidates(
    template_id: str,
    req: TemplateCutCandidatesReq,
    user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    """Recognize layout candidates from immutable teacher-template artifacts."""
    action_id, cut_types, image_type = _validate_template_cut_request(req)
    force = bool(req.force)
    with _locked_owned_template_session(template_id, user.id) as sess:
        if sess.status == "pending":
            raise HTTPException(404, detail="模板会话不存在或已过期")
        if sess.status == "failed":
            raise HTTPException(409, detail="模板识别失败，不能执行裁题")
        if sess.status == "frozen":
            raise HTTPException(409, detail="学生试卷已开始识别，模板已冻结，不能重新裁题")
        if sess.status not in {"ocr_done", "confirmed"} or not sess.pages:
            raise HTTPException(409, detail="请先完成教师模板识别")

        # Snapshot unresolved anchor/exemption drafts before a force retry can
        # clear the current action's pages. A layout retry does not rerun that
        # recognizer, so losing these candidates would leave a server-side
        # confirmation block with nothing the teacher can edit.
        inherited_review_metadata = {
            page_label: _latest_template_geometry_review_metadata(
                sess,
                page_label,
                str(artifact.get("sha256") or ""),
            )
            for page_label, artifact in (sess.page_artifacts or {}).items()
            if isinstance(artifact, dict)
        }

        existing = (sess.layout_cut_runs or {}).get(action_id)
        if isinstance(existing, dict):
            if existing.get("cut_types") != cut_types or existing.get("image_type") != image_type:
                raise HTTPException(409, detail="同一 action_id 的裁题参数不一致")
            if existing.get("status") == "complete" and not force:
                existing_pages = existing.get("pages") or {}
                question_plans_ready = (
                    "question" not in cut_types
                    or (
                        len(existing_pages) == len(sess.pages)
                        and all(
                            isinstance(page, dict)
                            and isinstance(page.get("anchor_crop_plan"), dict)
                            for page in existing_pages.values()
                        )
                    )
                )
                if question_plans_ready:
                    return _template_cut_response(sess, existing)
            run = existing
            run["status"] = "running"
            run["error"] = None
            if force:
                run["completed_at"] = None
                run["pages"] = {}
        else:
            run = {
                "action_id": action_id,
                "teacher_id": user.id,
                "status": "running",
                "cut_types": cut_types,
                "image_type": image_type,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "completed_at": None,
                "pages": {},
                "error": None,
            }
            sess.layout_cut_runs[action_id] = run

        try:
            artifacts = verify_template_artifacts(
                sess.template_dir,
                sess.page_artifacts,
            )
        except (TemplateEvidenceError, ValueError) as exc:
            raise HTTPException(409, detail=f"模板视觉资产无效：{exc}") from exc
        if len(artifacts) != len(sess.pages):
            raise HTTPException(409, detail="模板视觉资产页数不完整，请重新导入模板")

        sess.save_meta()
        for page_label, artifact in sorted(artifacts.items()):
            page_index = int(page_label.removeprefix("page_")) - 1
            page_run = run["pages"].get(page_label)
            if not isinstance(page_run, dict):
                page_run = {
                    "page_index": page_index,
                    "artifact_sha256": artifact["sha256"],
                    "modes": {},
                    **copy.deepcopy(inherited_review_metadata.get(page_label) or {}),
                }
                run["pages"][page_label] = page_run
            else:
                for key, value in (
                    inherited_review_metadata.get(page_label) or {}
                ).items():
                    page_run.setdefault(key, copy.deepcopy(value))
            if page_run.get("artifact_sha256") != artifact["sha256"]:
                raise HTTPException(409, detail="模板视觉资产已变化，请使用新的 action_id")
            artifact_path = os.path.realpath(os.path.join(
                sess.template_dir,
                artifact["template_image"].replace("/", os.sep),
            ))

            for cut_type in cut_types:
                stored = (page_run.get("modes") or {}).get(cut_type)
                frozen_question_plan = None
                if cut_type == "question" and not force:
                    frozen = _frozen_question_layout(sess, page_label, artifact)
                    if frozen is not None:
                        stored, frozen_question_plan = frozen
                if not (
                    isinstance(stored, dict)
                    and (stored.get("source") or {}).get("sha256") == artifact["sha256"]
                ):
                    try:
                        stored = await recognize_column_layout(
                            artifact_path,
                            teacher_id=str(user.id),
                            column_mode=normalize_column_mode(
                                getattr(sess, "column_mode", "double")
                            ),
                        )
                    except TemplateCutError as exc:
                        run["status"] = "failed"
                        run["error"] = {
                            "code": exc.code,
                            "message": exc.message,
                            "retryable": exc.retryable,
                        }
                        sess.save_meta()
                        status_code = 503 if exc.code == "CREDENTIAL_UNAVAILABLE" else 502
                        raise HTTPException(status_code, detail=run["error"]) from exc
                    page_run.setdefault("modes", {})[cut_type] = stored
                    sess.save_meta()
                elif frozen_question_plan is not None:
                    page_run.setdefault("modes", {})[cut_type] = stored
                    sess.save_meta()

                if cut_type == "question":
                    page_questions = (
                        (sess.pages[page_index] or {}).get("questions", [])
                        if page_index < len(sess.pages)
                        and isinstance(sess.pages[page_index], dict)
                        else []
                    )
                    expected_numbers = [
                        str(
                            question.get("question_no")
                            or question.get("question_number")
                            or ""
                        ).strip()
                        for question in page_questions
                        if isinstance(question, dict)
                        and str(
                            question.get("question_no")
                            or question.get("question_number")
                            or ""
                        ).strip()
                    ]
                    if frozen_question_plan is None and layout_needs_review(stored):
                        # A review draft has no trusted columns. Keep the
                        # draft visible for the teacher and leave any earlier
                        # frozen plan untouched; saving boundaries through the
                        # layout-overrides endpoint creates the plan.
                        page_run["anchor_crop_plan"] = None
                        page_run["layout_review_pending"] = True
                        sess.save_meta()
                        continue
                    page_run.pop("layout_review_pending", None)
                    try:
                        anchor_crop_plan = frozen_question_plan or build_anchor_crop_plan(
                            stored,
                            artifact_sha256=artifact["sha256"],
                            expected_question_numbers=expected_numbers,
                        )
                        artifact_with_plan = dict(sess.page_artifacts[page_label])
                        artifact_with_plan["question_crop_plan"] = anchor_crop_plan
                        verified_with_plan = verify_template_artifacts(
                            sess.template_dir,
                            {page_label: artifact_with_plan},
                        )[page_label]
                    except (AnchorCropPlanError, TemplateEvidenceError, ValueError) as exc:
                        run["status"] = "failed"
                        run["error"] = {
                            "code": "ANCHOR_CROP_PLAN_INVALID",
                            "message": "题目连续裁剪计划生成失败",
                            "retryable": False,
                        }
                        sess.save_meta()
                        raise HTTPException(500, detail=run["error"]) from exc
                    page_run["anchor_crop_plan"] = anchor_crop_plan
                    sess.page_artifacts[page_label] = verified_with_plan
                    sess.save_meta()

        run["status"] = "complete"
        run["error"] = None
        run["completed_at"] = datetime.now(timezone.utc).isoformat()
        sess.layout_candidates = {
            "active_action_id": action_id,
            "updated_at": run["completed_at"],
        }
        if len(sess.layout_cut_runs) > 5:
            removable = sorted(
                (
                    key for key in sess.layout_cut_runs
                    if key != action_id
                ),
                key=lambda key: str(sess.layout_cut_runs[key].get("created_at") or ""),
            )
            for old_action_id in removable[:-4]:
                sess.layout_cut_runs.pop(old_action_id, None)
        sess.save_meta()
        return _template_cut_response(sess, run)


def _validate_template_drawing_geometry(artifact: dict, template_mode: str) -> None:
    from services.template_evidence import validate_drawing_label_geometry
    validate_drawing_label_geometry(
        artifact.get("question_schema") or [], artifact.get("question_anchors") or [],
        artifact.get("template_answer_regions") or [], artifact.get("template_handwriting_regions") or [],
        page_width=artifact["width"], page_height=artifact["height"],
        question_crop_plan=artifact.get("question_crop_plan"), template_mode=template_mode,
    )


def _question_numbers(questions: list[dict]) -> list[str]:
    """Each question's number (question_no, else question_number); blanks dropped."""
    numbers = (
        str(question.get("question_no") or question.get("question_number") or "").strip()
        for question in questions
    )
    return [number for number in numbers if number]


def _drawing_geometry_unresolved(sess: TemplateSession, page_label: str) -> bool:
    """A layout run still has this page's drawing stage unfinished and unconfirmed."""
    return any(
        (page_run.get("template_ocr_diagnostics") or {}).get("teacher_geometry_confirmed") is not True
        and (page_run.get("template_ocr_diagnostics") or {}).get("model_stages", {}).get(
            "template-directory-drawing-v1", {}
        ).get("status") not in (None, "completed")
        for run in (sess.layout_cut_runs or {}).values()
        if isinstance(run, dict)
        for page_run in [(run.get("pages") or {}).get(page_label) or {}]
    )


def _drawing_schema_complete(question_schema: list[dict] | None, expected_numbers: list[str]) -> bool:
    """The teacher reviewed every question's drawing kind, regions and Q position."""
    return not (
        question_schema is None
        or len(question_schema) != len(expected_numbers)
        or {str(item.get("question_number") or "") for item in question_schema} != set(expected_numbers)
        or any(not {"answer_kind", "drawing_regions", "label_anchor"}.issubset(item)
               for item in question_schema)
    )


def _teacher_question_anchors(
    question_anchors: list[TemplateQuestionAnchorOverride],
    expected_numbers: list[str],
) -> list[dict]:
    """The teacher's question-number boxes, one per question, in page order.

    The request is a teacher action, so provenance is assigned by the server.
    Client-supplied confidence or confirmation flags cannot downgrade or
    impersonate this ownership decision.
    """
    anchors = normalize_question_anchors([
        {
            **anchor.model_dump(exclude_none=True),
            "confidence": 1.0,
            "human_confirmed": True,
            "confidence_source": "teacher",
        }
        for anchor in question_anchors
    ])
    by_number = {anchor["question_number"]: anchor for anchor in anchors}
    if set(by_number) != set(expected_numbers) or len(anchors) != len(expected_numbers):
        raise ValueError("题号框必须与本页题目逐一对应")
    return [by_number[number] for number in expected_numbers]


def _layout_answer_regions(
    template_mode: str,
    artifact: dict,
    requested: list[TemplateAnswerRegionOverride] | None,
    expected_numbers: list[str],
) -> list[dict]:
    """The teacher's answer regions when sent, else the artifact's.

    Blank templates and teacher edits must cover every question exactly once.
    """
    teacher_override = requested is not None
    raw_regions = (
        artifact.get("template_answer_regions")
        if not teacher_override
        else [item.model_dump(exclude_none=True) for item in requested]
    )
    regions = normalize_template_handwriting_regions(raw_regions or [])
    if template_mode == "blank" or teacher_override:
        by_number = {item["question_number"]: item for item in regions}
        if set(by_number) != set(expected_numbers) or len(regions) != len(expected_numbers):
            raise ValueError("模板作答区域必须与本页题目逐一对应")
        regions = [by_number[number] for number in expected_numbers]
    return regions


def _layout_handwriting_regions(
    template_mode: str,
    artifact: dict,
    requested: list[TemplateHandwritingRegionOverride] | None,
    expected_numbers: list[str],
) -> list[dict]:
    """The teacher's handwriting exemptions when sent, else the artifact's."""
    teacher_override = requested is not None
    raw_regions = (
        artifact.get("template_handwriting_regions")
        if not teacher_override
        else [item.model_dump(exclude_none=True) for item in requested]
    )
    return _template_handwriting_regions_for_mode(
        template_mode,
        raw_regions,
        expected_numbers,
        teacher_confirmed=teacher_override,
    )


def _layout_question_schema(
    artifact: dict,
    page_questions: list[dict],
    question_schema: list[dict],
    expected_numbers: list[str],
) -> list[dict]:
    """The artifact's question schema with the teacher's drawing geometry applied."""
    from services.template_evidence import normalize_question_schema

    submitted = {str(item.get("question_number") or ""): item for item in question_schema}
    if set(submitted) != set(expected_numbers) or len(submitted) != len(question_schema):
        raise ValueError("作图定位必须与本页题目逐一对应")
    updated = []
    for item in normalize_question_schema(artifact.get("question_schema") or page_questions):
        changed = dict(item)
        proposed = submitted[item["question_number"]]
        # Geometry edits do not reclassify the printed question or
        # its score policy. Drawing is independent of choice/fill/solve.
        for key in ("answer_kind", "drawing_regions", "label_anchor", "slot_ids", "region_kinds"):
            if key in proposed:
                changed[key] = proposed[key]
        updated.append(changed)
    return normalize_question_schema(updated)


def _layout_slot_questions(
    page_questions: list[dict],
    question_schema: list[dict] | None,
) -> list[dict]:
    """Copies of the page's questions carrying the teacher's slot and drawing edits.

    The copies are checked before anything is saved.
    """
    slot_questions = copy.deepcopy(page_questions)
    if question_schema is not None:
        by_number = {str(q.get("question_number") or ""): q for q in question_schema}
        for question in slot_questions:
            proposed = by_number.get(str(question.get("question_no") or question.get("question_number")), {})
            for key in ("slot_ids", "region_kinds", "answer_kind", "drawing_regions", "label_anchor",
                        "answer_slots", "blank_count", "blank_scores",
                        "subquestion_count", "subquestion_scores", "max_score"):
                if key in proposed:
                    question[key] = copy.deepcopy(proposed[key])
            if "slot_ids" in proposed and proposed["slot_ids"] is None:
                # The teacher deleted every position of this question:
                # its slot numbers and kinds go with the boxes.
                question.pop("slot_ids", None)
                question.pop("region_kinds", None)
    # A stale subquestion score layer (half-flattened imports or
    # old client echoes) folds into the root total here so a
    # teacher save can never repersist a contract-breaking row.
    # Genuinely invalid edits fail closed at save time with the
    # question named instead of blocking later at confirm.
    slot_questions = [
        reconcile_drawing_contract(merge_directory_leaf_score_layer(question))
        for question in slot_questions
    ]
    # Deleting a question's last drawing position leaves its standard
    # figure nothing to describe. Drop it, as 作图属性 → 非作图 does,
    # rather than reject the teacher's deletion against that figure.
    for question in slot_questions:
        if question.get("answer_kind") == "non_graphic":
            question.pop("drawing_reference", None)
    return slot_questions


def _layout_contract_rejection(slot_questions: list[dict], question_schema: list[dict]) -> str | None:
    """Why the first edited question fails its answer contract, if one does."""
    touched_numbers = {str(q.get("question_number") or "") for q in question_schema}
    for question in slot_questions:
        number = str(question.get("question_no") or question.get("question_number") or "")
        if number not in touched_numbers:
            continue
        try:
            AnswerSpec.model_validate(question)
        except ValueError as exc:
            return f"第 {number} 题的修改未通过合同校验，本次未保存：{_contract_rejection_reason(exc)}"
    return None


def _save_layout_to_page_questions(
    page_questions: list[dict],
    slot_questions: list[dict],
    updated_artifact: dict,
) -> None:
    """Copy the saved geometry into the page's editable leaf contract.

    The final confirm endpoint does not trust geometry sent along with answers.
    """
    saved_slots = {str(q.get("question_no") or q.get("question_number")): q for q in slot_questions}
    for question in page_questions:
        saved = saved_slots.get(str(question.get("question_no") or question.get("question_number")), {})
        for key in ("slot_ids", "region_kinds", "answer_slots", "blank_count", "blank_scores",
                    "subquestion_count", "subquestion_scores", "max_score",
                    "answer_kind", "drawing_regions", "label_anchor"):
            if key in saved:
                question[key] = copy.deepcopy(saved[key])
        if saved:
            # Keys the teacher's edit removed must not survive on the row.
            for key in ("slot_ids", "region_kinds", "drawing_reference"):
                if key not in saved:
                    question.pop(key, None)
    updated_by_number = {item["question_number"]: item for item in updated_artifact.get("question_schema") or []}
    for question in page_questions:
        item = updated_by_number.get(str(question.get("question_no") or question.get("question_number"))) or {}
        if isinstance(question.get("drawing_reference"), dict):
            question["drawing_reference"]["teacher_confirmed"] = False
        for key in ("question_type", "choice_mode", "answer_kind", "drawing_regions", "label_anchor"):
            if key in item:
                question[key] = copy.deepcopy(item[key])


def _approve_page_layout_runs(
    sess: TemplateSession,
    page_label: str,
    updated_artifact: dict,
    plan: dict,
    layout_result: dict,
) -> None:
    """Point every layout run at the saved page and drop its review leftovers.

    Saving the page is the teacher's explicit approval of its full geometry
    contract, so every non-authoritative candidate and unresolved review
    marker goes from all historical runs for the page.
    """
    for layout_run in (sess.layout_cut_runs or {}).values():
        if not isinstance(layout_run, dict):
            continue
        page_run = (layout_run.get("pages") or {}).get(page_label)
        if not isinstance(page_run, dict):
            continue
        page_run["artifact_sha256"] = updated_artifact["sha256"]
        page_run["anchor_crop_plan"] = plan
        for key in (
            "layout_review_pending",
            "anchor_review_pending",
            "geometry_review_pending",
            "question_anchor_candidates",
            "template_answer_region_candidates",
            "template_handwriting_region_candidates",
            "question_schema_candidates",
        ):
            page_run.pop(key, None)
        for key in ("anchor_diagnostics", "template_ocr_diagnostics"):
            diagnostic = page_run.get(key)
            if isinstance(diagnostic, dict) and (
                diagnostic.get("needs_review") is True
                or diagnostic.get("geometry_review_pending") is True
                or (isinstance(diagnostic.get("image_association"), dict) and (
                    diagnostic["image_association"].get("needs_review") is True
                    or diagnostic["image_association"].get("status") == "needs_review"
                ))
            ):
                if key == "template_ocr_diagnostics" and diagnostic.get("model_stages"):
                    # Keep the bounded-import contract after geometry is
                    # approved; missing answers/references remain gated.
                    page_run[key] = {
                        "model_stages": diagnostic["model_stages"],
                        "needs_review": False, "geometry_review_pending": False,
                        "teacher_geometry_confirmed": True,
                    }
                else:
                    page_run.pop(key, None)
        # Persist the revised draft itself (identities, revision, pixel
        # endpoints) rather than a metadata-free reconstruction, so the
        # next import/re-cut and the anchor OCR see the same contract.
        page_run.setdefault("modes", {})["question"] = layout_result


@router.put("/batch-grade/template/{template_id}/layout-overrides")
def batch_grade_template_layout_overrides(
    template_id: str,
    req: TemplateLayoutOverridesReq,
    user: User = Depends(_require_user),
):
    """Persist teacher-confirmed column boundaries and question-number boxes."""
    with _locked_owned_template_session(template_id, user.id) as sess:
        if sess.status == "frozen":
            raise HTTPException(409, detail="学生试卷已开始识别，模板定位已冻结")
        if req.page_index < 0 or req.page_index >= len(sess.pages):
            raise HTTPException(404, detail="模板页面不存在")
        page_label = f"page_{req.page_index + 1}"
        artifact = (sess.page_artifacts or {}).get(page_label)
        if not isinstance(artifact, dict):
            raise HTTPException(409, detail="模板页面尚未生成可编辑定位数据")
        page_questions = next(
            (
                page.get("questions") or []
                for page in (sess.pages or [])
                if int(page.get("page_index", -1)) == req.page_index
            ),
            [],
        )
        expected_numbers = _question_numbers(page_questions)
        previous_bounds = _plan_column_bounds(artifact.get("question_crop_plan"))
        previous_answers_hash = sess.answers_hash()
        layout_validation_error = None
        try:
            if _drawing_geometry_unresolved(sess, page_label) and not _drawing_schema_complete(
                req.question_schema, expected_numbers,
            ):
                raise ValueError("作图题定位尚未完成，请逐题核对作图属性、区域和 Q 位置后再保存定位；非作图题请移除作图标记")
            columns = [column.model_dump() for column in req.columns]
            column_mode = {1: "single", 2: "double", 3: "triple"}.get(len(columns))
            if column_mode is None:
                raise ValueError("分栏数量必须在 1 到 3 之间")
            # The teacher edit is a new revision of the draft shown on the
            # confirmation page: every column gets a server-bound column_id,
            # layout_revision, pixel endpoints, and image hash before any
            # downstream request can reuse it. Unchanged boundaries keep their
            # revision so cached column inputs remain identifiable.
            layout_result = revise_layout_draft(
                _latest_page_layout_draft(sess, page_label, artifact),
                column_mode=column_mode,
                columns=columns,
            )
            plan = build_anchor_crop_plan(
                layout_result,
                artifact_sha256=str(artifact.get("sha256") or ""),
                expected_question_numbers=expected_numbers,
            )
            anchors = _teacher_question_anchors(req.question_anchors, expected_numbers)
            answer_regions = _layout_answer_regions(
                sess.template_mode, artifact, req.template_answer_regions, expected_numbers,
            )
            handwriting_regions = _layout_handwriting_regions(
                sess.template_mode, artifact, req.template_handwriting_regions, expected_numbers,
            )
            updated_artifact = dict(artifact)
            updated_artifact["question_crop_plan"] = plan
            updated_artifact["question_anchors"] = anchors
            if (
                sess.template_mode == "blank"
                or req.template_answer_regions is not None
                or "template_answer_regions" in artifact
            ):
                updated_artifact["template_answer_regions"] = answer_regions
            if req.question_schema is not None:
                updated_artifact["question_schema"] = _layout_question_schema(
                    artifact, page_questions, req.question_schema, expected_numbers,
                )
            slot_questions = _layout_slot_questions(page_questions, req.question_schema)
            if req.question_schema is not None:
                rejection = _layout_contract_rejection(slot_questions, req.question_schema)
                # Raise outside any except block with no causal chain: the
                # diagnostics boundary must classify a teacher-input contract
                # rejection as a business code (FORMAT_INVALID), never as
                # OUTPUT_PARSE_FAILED with the misleading "模型返回结果不符合格式
                # 要求" copy (incident dfe229faafbd).
                if rejection is not None:
                    raise HTTPException(422, detail=rejection)
            from services.template_slots import sync_artifact_slots
            updated_artifact = sync_artifact_slots(updated_artifact, slot_questions, materialize=True)
            updated_artifact["template_handwriting_regions"] = handwriting_regions
            _validate_template_drawing_geometry(updated_artifact, sess.template_mode)
            updated_artifact.pop("geometry_review_pending", None)
            updated_artifact = _refresh_template_difference_artifact(
                sess.template_dir,
                updated_artifact,
                template_mode=sess.template_mode,
                raw_regions=handwriting_regions,
            )
            updated_artifact = verify_template_artifacts(
                sess.template_dir,
                {page_label: updated_artifact},
            )[page_label]
        except (AnchorCropPlanError, TemplateEvidenceError, ValueError) as exc:
            # Leave the exception block before constructing HTTPException.
            # Python keeps an implicit __context__ for raises inside except,
            # which would make the diagnostics boundary classify this expected
            # teacher-input conflict as OUTPUT_PARSE_FAILED.
            layout_validation_error = str(exc)
        if layout_validation_error is not None:
            raise HTTPException(422, detail=layout_validation_error)

        sess.page_artifacts[page_label] = updated_artifact
        _save_layout_to_page_questions(page_questions, slot_questions, updated_artifact)
        _approve_page_layout_runs(sess, page_label, updated_artifact, plan, layout_result)
        # Only boundaries of an already approved plan can count as "changed".
        # The first approved plan — or a stored plan that carries no trusted
        # columns, such as a review shell — merely records the teacher's layout:
        # answers may already have been solved from the whole-page fallback and
        # there is no prior column input to invalidate. A stored plan with real
        # boundaries still invalidates the cache when those boundaries move.
        layout_changed = bool(previous_bounds) and previous_bounds != _plan_column_bounds(plan)
        if sess.status == "confirmed":
            sess.requires_reconfirmation = True
            # Only an answer-side edit costs the teacher the standard answers; a
            # moved box is geometry and asks for a geometry review with the answers
            # left intact.
            sess.reconfirmation_reason = (
                CONTENT_CHANGED_REASON if sess.answers_hash() != previous_answers_hash
                else GEOMETRY_CHANGED_REASON)
        sess.save_meta()
        return {
            "success": True,
            "template_id": template_id,
            "template_mode": normalize_template_mode(sess.template_mode, default="marked"),
            "page_index": req.page_index,
            "columns": plan["parameters"]["columns"],
            "column_mode": column_mode,
            "layout_revision": int(layout_result["layout_revision"]),
            "layout_changed": layout_changed,
            "question_anchors": anchors,
            "question_schema": updated_artifact.get("question_schema") or [],
            "template_answer_regions": answer_regions,
            "template_handwriting_regions": handwriting_regions,
            "requires_template_confirmation": sess.status == "confirmed",
        }


def _page_question_numbers(page: dict) -> list[str]:
    """The page's question numbers in order, blanks kept in place."""
    return [
        str(question.get("question_no") or question.get("question_number") or "").strip()
        for question in (page.get("questions") or [])
    ]


def _rebuild_renumbered_artifacts(
    sess: TemplateSession,
    new_pages: list[dict],
    before_pages: dict[int, list[str]],
    renames: dict[str, str],
    *,
    added_number: str,
    edited_page_index: int,
    before_number: str,
) -> dict[str, dict]:
    """Rebuild and verify the artifact of every page whose numbers changed.

    A shift crosses page boundaries, so this is not only the edited page.
    """
    updated_artifacts: dict[str, dict] = {}
    for page in new_pages:
        page_index = int(page.get("page_index", -1))
        label = f"page_{page_index + 1}"
        artifact = (sess.page_artifacts or {}).get(label)
        if not isinstance(artifact, dict):
            continue
        if _page_question_numbers(page) == before_pages.get(page_index):
            continue
        updated_artifact = _synchronized_page_artifact(
            artifact,
            page,
            renames,
            added_number=added_number if page_index == edited_page_index else "",
            before_number=before_number,
        )
        updated_artifacts[label] = _refresh_template_difference_artifact(
            sess.template_dir,
            updated_artifact,
            template_mode=sess.template_mode,
        )
    if updated_artifacts:
        verified = verify_template_artifacts(
            sess.template_dir,
            {**(sess.page_artifacts or {}), **updated_artifacts},
        )
        for label in updated_artifacts:
            updated_artifacts[label] = verified[label]
    return updated_artifacts


def _bind_renumbered_artifacts(
    sess: TemplateSession,
    updated_artifacts: dict[str, dict],
    added_label: str,
) -> None:
    """Store the rebuilt artifacts and point every layout run at them.

    The page that gained a question waits for the teacher to place its boxes.
    """
    for label, artifact in updated_artifacts.items():
        sess.page_artifacts[label] = artifact
        for layout_run in (sess.layout_cut_runs or {}).values():
            if not isinstance(layout_run, dict):
                continue
            page_run = (layout_run.get("pages") or {}).get(label)
            if not isinstance(page_run, dict):
                continue
            plan = artifact.get("question_crop_plan")
            if not isinstance(plan, dict):
                continue
            page_run["artifact_sha256"] = artifact["sha256"]
            page_run["anchor_crop_plan"] = plan
            if label == added_label:
                page_run["geometry_review_pending"] = True
                page_run["anchor_diagnostics"] = {
                    "needs_review": True, "review_reason": "teacher_added_question_placeholder",
                }
            page_run.setdefault("modes", {})["question"] = (
                _layout_result_from_frozen_question_plan(artifact, plan)
            )


@router.put("/batch-grade/template/{template_id}/questions")
def batch_grade_template_question_edit(
    template_id: str,
    req: TemplateQuestionEditReq,
    user: User = Depends(_require_user),
):
    """Add or remove one question number the recognition model got wrong.

    Adding shifts every later question up by one, across page boundaries: a
    missed question makes the whole tail of the paper read one low.  Removing
    does not renumber, because a spurious box leaves its neighbours correct.

    The locating artifacts move with the questions here.  Their answer content
    stays teacher-owned and is edited through the confirm endpoint as before.
    """
    action = str(req.action or "").strip().lower()
    if action not in {"insert", "delete"}:
        raise HTTPException(400, detail="action 必须是 insert 或 delete")
    with _locked_owned_template_session(template_id, user.id) as sess:
        if sess.status == "frozen":
            raise HTTPException(409, detail="学生试卷已开始识别，模板定位已冻结")
        if sess.status not in ("ocr_done", "confirmed") or not sess.pages:
            raise HTTPException(409, detail="模板尚未完成识别，不能编辑题号")
        if req.page_index < 0 or req.page_index >= len(sess.pages):
            raise HTTPException(404, detail="模板页面不存在")
        before_pages = {
            int(page.get("page_index", -1)): _page_question_numbers(page)
            for page in sess.pages
        }
        try:
            if action == "insert":
                new_pages, added_number, renames = insert_template_question(
                    sess.pages,
                    page_index=req.page_index,
                    before_question_number=req.before_question_number,
                    new_question_number=req.new_question_number,
                )
            else:
                new_pages = delete_template_question(
                    sess.pages,
                    page_index=req.page_index,
                    question_number=req.question_number,
                )
                added_number, renames = "", {}
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc

        try:
            updated_artifacts = _rebuild_renumbered_artifacts(
                sess,
                new_pages,
                before_pages,
                renames,
                added_number=added_number,
                edited_page_index=req.page_index,
                before_number=req.before_question_number,
            )
        except (AnchorCropPlanError, TemplateEvidenceError, ValueError) as exc:
            raise HTTPException(422, detail=str(exc)) from exc

        sess.pages = new_pages
        added_label = f"page_{req.page_index + 1}" if added_number else ""
        _bind_renumbered_artifacts(sess, updated_artifacts, added_label)
        # Question numbers are part of the graded contract, so a template that
        # was already confirmed has to be confirmed again.
        if sess.status == "confirmed":
            sess.requires_reconfirmation = True
            sess.reconfirmation_reason = "模板题号已调整，请重新确认模板"
        sess.save_meta()
        return {
            "success": True,
            "template_id": template_id,
            "page_index": req.page_index,
            "action": action,
            "question_number": added_number or req.question_number,
            "added_question_number": added_number,
            "renamed_question_numbers": renames,
            "geometry_review_pending_pages": [req.page_index] if added_number else [],
            "pages": sess.pages,
            # The locating panel keys its boxes by question number, so it needs
            # the rebuilt artifacts to follow a shift rather than re-deriving
            # them from numbers that have already moved.
            "page_artifacts": {
                label: {
                    "question_numbers": artifact.get("question_numbers") or [],
                    "geometry_review_pending": label == added_label,
                    "question_schema": artifact.get("question_schema") or [],
                    "template_answer_regions": artifact.get("template_answer_regions") or [],
                    "question_anchors": artifact.get("question_anchors") or [],
                    "template_handwriting_regions": artifact.get(
                        "template_handwriting_regions"
                    ) or [],
                }
                for label, artifact in updated_artifacts.items()
            },
            "requires_template_confirmation": sess.status == "confirmed",
        }


def _saved_question_metadata(template: TemplateSession) -> dict[tuple[int, str], dict]:
    """Server-owned directory metadata of the saved questions, by (page, number)."""
    try:
        return {
            (int(page["page_index"]), str(question["question_no"])):
                AnswerSpec.model_validate(reconcile_drawing_contract(question)).directory_metadata()
            for page in template.pages for question in page.get("questions") or []
        }
    except ValueError as exc:
        error = f"已保存模板的题目合同无效，无法确认：{exc}"
    # Raise outside the except block with no causal chain so the diagnostics
    # boundary reports a business code instead of a model-output parse failure.
    raise HTTPException(400, detail=error)


def _submitted_template_pages(
    req: TemplateConfirmReq,
    saved_metadata: dict[tuple[int, str], dict],
) -> list[dict]:
    """Validate the teacher's edited pages into frozen answer contracts."""
    try:
        new_pages = []
        for page in req.pages:
            if page.page_index < 0:
                raise ValueError("page_index must be non-negative")
            questions = []
            for q in page.questions:
                try:
                    questions.append(finalize_answer_spec_for_category(reconcile_drawing_contract(merge_directory_leaf_score_layer({
                        **q.model_dump(),
                        **saved_metadata.get((page.page_index, q.question_no), {}),
                    })), req.category).model_dump())
                except ValueError as exc:
                    raise ValueError(f"第 {page.page_index + 1} 页第 {q.question_no} 题未通过答案合同校验，本次未保存："
                                     + _contract_rejection_reason(exc)) from None
            new_pages.append(TemplateSession.make_page(page.page_index, questions))
        page_indexes = [page["page_index"] for page in new_pages]
        if not new_pages or len(page_indexes) != len(set(page_indexes)):
            raise ValueError("template pages must be non-empty with unique page_index")
        all_numbers = [question["question_no"] for page in new_pages for question in page["questions"]]
        if len(all_numbers) != len(set(all_numbers)):
            raise ValueError("整卷存在重复题号，请核对各页层级归属后再确认")
        return new_pages
    except ValueError as exc:
        error = str(exc)
    # Same boundary rule as the saved-contract guard: a teacher-input
    # rejection must leave the except block without a causal chain, or the
    # diagnostics boundary reports OUTPUT_PARSE_FAILED (incident 2b5e7b50).
    raise HTTPException(400, detail=error)


def _require_template_confirmable(sess: TemplateSession) -> None:
    if sess.status == "pending":
        raise HTTPException(404, detail="模板会话不存在或已过期")
    if sess.status == "failed":
        raise HTTPException(409, detail="模板识别失败，不能确认；请重新导入模板")
    if sess.status == "frozen":
        raise HTTPException(409, detail="学生试卷已开始识别，本次模板已冻结，不能再编辑")
    if sess.status not in ("ocr_done", "confirmed") or not sess.pages:
        raise HTTPException(409, detail="模板尚未完成识别，不能确认")


def _merge_saved_question_fields(
    sess: TemplateSession,
    req: TemplateConfirmReq,
    new_pages: list[dict],
) -> None:
    """Restore server-owned fields on the submitted questions and check them.

    Raises ValueError naming the first question the teacher still has to fix:
    a Max report not deleted or kept, a missing standard answer or drawing
    region, or an invalid standard figure.
    """
    # Directory ownership and drawing geometry must come from the saved
    # server-side layout, never from a stale/forged answer payload.
    authoritative = {
        (int(page["page_index"]), str(question["question_no"])): question
        for page in sess.pages for question in page.get("questions") or []
    }
    # Older drafts lack the bounded-stage contract. Do not reinterpret
    # their existing empty-answer/manual drawing behavior on upgrade.
    bounded_model_pages = {
        int(page_run["page_index"])
        for run in (sess.layout_cut_runs or {}).values() if isinstance(run, dict)
        for page_run in (run.get("pages") or {}).values()
        if isinstance(page_run, dict) and "page_index" in page_run
        and (page_run.get("template_ocr_diagnostics") or {}).get("model_stages")
    }
    kept_max_flags = {(page.page_index, q.question_no): set(q.max_flags_kept)
                      for page in req.pages for q in page.questions}
    missing_drawing_region = []
    for page in new_pages:
        for question in page["questions"]:
            original = authoritative.get((page["page_index"], question["question_no"]))
            if not original:
                continue
            metadata = AnswerSpec.model_validate(original).directory_metadata()
            question.update(metadata)
            # An explicit Max extra/refused report blocks until the
            # teacher deletes that position or explicitly keeps it.
            kept = kept_max_flags.get((page["page_index"], question["question_no"]), set())
            for flag in original.get("max_flags") or []:
                if flag.get("slot_id") and flag["slot_id"] not in (question.get("slot_ids") or []):
                    continue
                if (flag.get("slot_id") or "*") not in kept:
                    from services.template_max_owned import max_flag_message
                    raise ValueError(f"第 {page['page_index'] + 1} 页："
                                     + max_flag_message(question["question_no"], flag))
            if (original.get("solve_visual_context") is not None
                    and question.get("question_text") == original.get("question_text")):
                question["solve_visual_context"] = copy.deepcopy(original["solve_visual_context"])
            if metadata.get("directory_node_id") and page["page_index"] in bounded_model_pages:
                if question.get("answer_kind") != "drawing" and not str(question.get("standard_answer") or "").strip():
                    raise ValueError(f"第 {page['page_index'] + 1} 页第 {question['question_no']} 题的标准答案尚未填写，请补齐后再保存模板")
                if (question.get("answer_kind") in {"drawing", "mixed"}
                        and not question.get("drawing_regions")):
                    missing_drawing_region.append(f"第 {page['page_index'] + 1} 页 {question['question_no']}")
            try:
                if question.get("drawing_reference") is not None:
                    from services.drawing_reference import normalize_reference
                    if question.get("answer_kind") not in {"drawing", "mixed"}:
                        raise ValueError("只有画图/图文混合题可以设置标准图")
                    raw_reference = question["drawing_reference"]
                    # 保存模板同时确认固定参考或自定义作图的判分方式。
                    question["drawing_reference"] = normalize_reference(
                        raw_reference,
                        trusted_confirmed=True,
                    )
                    from services.drawing_grading import validate_reference_geometry
                    validate_reference_geometry(question["drawing_reference"], question.get("drawing_regions") or [])
                question.update(finalize_answer_spec_for_category(question, req.category).model_dump())
            except ValueError as exc:
                raise ValueError(f"第 {page['page_index'] + 1} 页第 {question['question_no']} 题："
                                 + _contract_rejection_reason(exc)) from None
    if missing_drawing_region:
        raise ValueError(
            f"{'、'.join(missing_drawing_region)} 是作图题，但还没有画图区。"
            "请用“绿色画图区”框出作图位置，或把该题“作图属性”改为非作图")


def _require_template_scope_unchanged(
    sess: TemplateSession,
    req: TemplateConfirmReq,
    exam_name: str,
) -> None:
    if sess.category and sess.category != req.category:
        raise HTTPException(409, detail="模板批次类型已冻结，请重新导入模板")
    if sess.exam_name and sess.exam_name != exam_name:
        raise HTTPException(409, detail="模板考试名称已冻结，请重新导入模板")
    if req.column_mode is not None:
        requested_mode = normalize_column_mode(req.column_mode)
        current_mode = normalize_column_mode(
            getattr(sess, "column_mode", "double")
        )
        if requested_mode != current_mode:
            raise HTTPException(
                409,
                detail="栏数已在模板识别草稿中确定；请先重新生成布局候选后再保存",
            )


def _require_template_geometry_reviewed(sess: TemplateSession) -> None:
    """Refuse while any page still shows model candidates instead of saved geometry."""
    layout_review_pending = _template_layout_review_pending_pages(sess)
    anchor_review_pending = _template_anchor_review_pending_pages(sess)
    geometry_review_pending = _template_geometry_review_pending_pages(sess)
    if not geometry_review_pending:
        return
    # Model candidates are previews, never approved geometry.  This
    # server-side gate is independent of the browser's disabled state
    # and remains active even when a column crop plan already exists.
    pages_text = "、".join(
        str(index + 1) for index in geometry_review_pending
    )
    if anchor_review_pending:
        detail = (
            f"第 {pages_text} 页的题号定位或模板豁免框仍需人工确认，"
            "请先在确认页保存定位结果再确认模板"
        )
    elif layout_review_pending:
        detail = (
            f"第 {pages_text} 页的分栏仍需人工确认，"
            "请先在确认页保存分栏边界再确认模板"
        )
    else:
        detail = (
            f"第 {pages_text} 页的模板识别关联仍需人工确认，"
            "请先在确认页保存定位结果再确认模板"
        )
    raise HTTPException(
        409,
        detail=detail,
    )


def _pages_by_index(pages: list[dict]) -> dict[int, dict]:
    return {int(page.get("page_index", -1)): page for page in pages}


def _sync_confirmed_artifacts(
    sess: TemplateSession,
    existing_by_index: dict[int, dict],
    proposed_by_index: dict[int, dict],
) -> None:
    """Freeze each page artifact's question schema to the confirmed questions."""
    for page_index, existing_page in existing_by_index.items():
        existing_numbers = [
            str(question.get("question_no") or "").strip()
            for question in existing_page.get("questions", [])
        ]
        proposed_page = proposed_by_index[page_index]
        proposed_numbers = [
            str(question.get("question_no") or "").strip()
            for question in proposed_page.get("questions", [])
        ]
        if existing_numbers != proposed_numbers:
            raise HTTPException(
                409,
                detail="模板题号或顺序不能修改，请重新导入模板",
            )
        if not page_is_graded(existing_page):
            # An ungraded page has no questions and no answer geometry.
            continue

        label = f"page_{page_index + 1}"
        raw_artifact = sess.page_artifacts.get(label)
        if not isinstance(raw_artifact, dict):
            raise HTTPException(
                409,
                detail="模板缺少视觉基准，请重新导入模板",
            )
        updated_artifact = dict(raw_artifact)
        from services.template_evidence import normalize_question_schema
        updated_artifact["question_schema"] = normalize_question_schema(proposed_page.get("questions") or [])
        updated_artifact["question_numbers"] = [
            item["question_number"]
            for item in updated_artifact["question_schema"]
        ]
        try:
            from services.template_slots import sync_artifact_slots
            updated_artifact = sync_artifact_slots(updated_artifact, proposed_page.get("questions") or [], materialize=True)
            _validate_template_drawing_geometry(updated_artifact, sess.template_mode)
            # The teacher edits answers here, but the approved visual
            # boxes belong to the page geometry and must survive the
            # confirmation snapshot.
            updated_artifact = _refresh_template_difference_artifact(
                sess.template_dir,
                updated_artifact,
                template_mode=sess.template_mode,
            )
        except ValueError as exc:
            raise HTTPException(409, detail=str(exc)) from exc
        sess.page_artifacts[label] = updated_artifact


def _sync_confirmed_crop_plans(sess: TemplateSession, new_pages: list[dict]) -> None:
    """Point each frozen column crop plan at the confirmed question numbers."""
    for page in new_pages:
        label = f"page_{int(page['page_index']) + 1}"
        raw_artifact = sess.page_artifacts.get(label)
        if not isinstance(raw_artifact, dict):
            continue
        raw_plan = raw_artifact.get("question_crop_plan")
        if raw_plan is None:
            continue
        confirmed_numbers = [
            str(
                question.get("question_no")
                or question.get("question_number")
                or ""
            ).strip()
            for question in (page.get("questions") or [])
            if isinstance(question, dict)
            and str(
                question.get("question_no")
                or question.get("question_number")
                or ""
            ).strip()
        ]
        synchronized_plan = dict(raw_plan)
        synchronized_plan["expected_question_numbers"] = confirmed_numbers
        try:
            synchronized_plan = validate_anchor_crop_plan(
                synchronized_plan,
                artifact_sha256=str(raw_artifact.get("sha256") or ""),
            )
        except AnchorCropPlanError as exc:
            raise HTTPException(
                409,
                detail="题目连续裁剪计划已损坏，请重新执行模板裁题",
            ) from exc
        updated_artifact = dict(raw_artifact)
        updated_artifact["question_crop_plan"] = synchronized_plan
        sess.page_artifacts[label] = updated_artifact
        for layout_run in (sess.layout_cut_runs or {}).values():
            if not isinstance(layout_run, dict):
                continue
            page_run = (layout_run.get("pages") or {}).get(label)
            if (
                isinstance(page_run, dict)
                and page_run.get("artifact_sha256") == raw_artifact.get("sha256")
                and isinstance(page_run.get("anchor_crop_plan"), dict)
            ):
                page_run["anchor_crop_plan"] = synchronized_plan


def _kept_knowledge_classifications(sess: TemplateSession, new_pages: list[dict]) -> dict[str, dict]:
    """Keep a question's knowledge label only while its stem text is unchanged."""
    valid_classifications: dict[str, dict] = {}
    for page in new_pages:
        page_index = int(page.get("page_index", 0))
        for question in page.get("questions") or []:
            question_no = str(question.get("question_no") or "").strip()
            key = sess.knowledge_classification_key(page_index, question_no)
            existing = sess.knowledge_classifications.get(key) or {}
            if (
                question_no
                and existing.get("question_text_hash")
                    == sess.question_text_hash(question.get("question_text"))
            ):
                valid_classifications[key] = existing
    return valid_classifications


def _requested_correction_relations(
    db,
    user: User,
    req: TemplateConfirmReq,
    sess: TemplateSession,
    class_id,
) -> list[tuple]:
    """(question, candidate) pairs the teacher saw, kept, and that still qualify."""
    from services.correction_tracking import template_candidates

    submitted_questions = [
        question
        for page in req.pages
        for question in page.questions
    ]
    shown_cycle_ids = {
        int(value)
        for question in submitted_questions
        if not question.correction_excluded
        for value in question.authorized_wrong_cycle_ids or []
    }
    if not shown_cycle_ids:
        return []
    # Re-check only the records the teacher saw, not the whole class history.
    discovered = template_candidates(
        db,
        teacher_id=user.id,
        class_id=class_id,
        questions=[question.model_dump() for question in submitted_questions],
        limit=5000,
        teaching_context=sess.teaching_context,
        cycle_ids=shown_cycle_ids,
    )
    requested_relations = []
    for question, discovery in zip(submitted_questions, discovered):
        if question.correction_excluded:
            continue
        displayed_cycle_ids = {
            int(value) for value in question.authorized_wrong_cycle_ids or []
        }
        # Save approves only the relations the teacher actually saw.
        # New discoveries and expired candidates must not require a
        # second confirmation or block the template itself.
        requested_relations.extend(
            (question, candidate)
            for candidate in discovery.get("candidates", [])
            if int(candidate["cycle_id"]) in displayed_cycle_ids
        )
    return requested_relations


# Discovery relation -> stored relation type and the note shown in history.
_AUTHORIZED_RELATIONS = {
    "same_original": ("teacher_authorized_same_original", "教师确认当前题与历史原题一致"),
    "similar_candidate": ("teacher_authorized_similar", "教师确认当前题可检验相似历史错题"),
}


def _authorize_corrections(
    db,
    user: User,
    req: TemplateConfirmReq,
    sess: TemplateSession,
    requested: list[tuple],
    class_id,
) -> set[tuple]:
    """Let the kept template questions correct their pending wrong-question cycles.

    Returns the (source fingerprint, cycle id) keys now authorized.
    """
    from models_correction import (
        QuestionCorrectionRelation,
        TemplateQuestionAuthorization,
        WrongQuestionCycle,
    )
    from services.correction_tracking import question_fingerprint

    if not requested:
        return set()
    fingerprints = {
        id(question): question_fingerprint({
            **question.model_dump(), "teaching_context": sess.teaching_context,
        })
        for question, _candidate in requested
    }
    cycle_ids = sorted({int(candidate["cycle_id"]) for _question, candidate in requested})
    targets = {
        cycle.id: cycle
        for start in range(0, len(cycle_ids), 500)
        for cycle in db.query(WrongQuestionCycle).filter(
            WrongQuestionCycle.id.in_(cycle_ids[start:start + 500]),
            WrongQuestionCycle.teacher_id == user.id,
            WrongQuestionCycle.class_id == class_id,
            WrongQuestionCycle.status == "pending",
        )
    }
    authorizations = {
        (row.source_question_fingerprint, row.target_cycle_id): row
        for row in db.query(TemplateQuestionAuthorization).filter(
            TemplateQuestionAuthorization.teacher_id == user.id,
            TemplateQuestionAuthorization.template_id == req.template_id,
        )
    }
    links = {
        (row.source_question_fingerprint, row.target_cycle_id): row
        for row in db.query(QuestionCorrectionRelation).filter(
            QuestionCorrectionRelation.teacher_id == user.id,
            QuestionCorrectionRelation.source_question_fingerprint.in_(set(fingerprints.values())),
        )
    }
    kept: set[tuple] = set()
    for question, candidate in requested:
        fingerprint = fingerprints[id(question)]
        cycle_id = int(candidate["cycle_id"])
        target = targets.get(cycle_id)
        if target is None:
            raise HTTPException(400, detail=f"待纠正题目 {cycle_id} 无效或已关闭")
        key = (fingerprint, target.id)
        kept.add(key)
        authorization = authorizations.get(key)
        if authorization is None:
            authorizations[key] = TemplateQuestionAuthorization(
                teacher_id=user.id,
                template_id=req.template_id,
                target_cycle_id=target.id,
                source_question_fingerprint=fingerprint,
                authorized_by=user.id,
            )
            db.add(authorizations[key])
        else:
            authorization.revoked_at = None
        relation_type, difference_summary = _AUTHORIZED_RELATIONS[candidate["relation"]]
        linked = links.get(key)
        if linked is None:
            links[key] = QuestionCorrectionRelation(
                teacher_id=user.id,
                target_cycle_id=target.id,
                source_question_identity=f"template:{req.template_id}:{question.question_no}",
                source_question_fingerprint=fingerprint,
                relation_type=relation_type,
                similarity_score=candidate["similarity_score"],
                difference_summary=difference_summary,
                authorized_by=user.id,
            )
            db.add(links[key])
        else:
            linked.relation_type = relation_type
            linked.similarity_score = candidate["similarity_score"]
            linked.difference_summary = difference_summary
            linked.revoked_at = None
    return kept

def _revoke_dropped_authorizations(
    db,
    user: User,
    template_id: str,
    kept_keys: set[tuple],
) -> None:
    """Revoke this template's authorizations the teacher no longer keeps.

    The correction relation goes too, unless another template still
    authorizes the same question for the same cycle.
    """
    from models_correction import QuestionCorrectionRelation, TemplateQuestionAuthorization

    existing_authorizations = db.query(TemplateQuestionAuthorization).filter(
        TemplateQuestionAuthorization.teacher_id == user.id,
        TemplateQuestionAuthorization.template_id == template_id,
        TemplateQuestionAuthorization.revoked_at.is_(None),
    ).all()
    for authorization in existing_authorizations:
        key = (authorization.source_question_fingerprint, authorization.target_cycle_id)
        if key in kept_keys:
            continue
        authorization.revoked_at = datetime.now()
        other_active_authorization = db.query(TemplateQuestionAuthorization.id).filter(
            TemplateQuestionAuthorization.teacher_id == user.id,
            TemplateQuestionAuthorization.template_id != template_id,
            TemplateQuestionAuthorization.source_question_fingerprint == authorization.source_question_fingerprint,
            TemplateQuestionAuthorization.target_cycle_id == authorization.target_cycle_id,
            TemplateQuestionAuthorization.revoked_at.is_(None),
        ).first()
        if other_active_authorization is None:
            linked = db.query(QuestionCorrectionRelation).filter(
                QuestionCorrectionRelation.teacher_id == user.id,
                QuestionCorrectionRelation.source_question_fingerprint == authorization.source_question_fingerprint,
                QuestionCorrectionRelation.target_cycle_id == authorization.target_cycle_id,
                QuestionCorrectionRelation.revoked_at.is_(None),
            ).first()
            if linked is not None:
                linked.revoked_at = datetime.now()


def _save_template_correction_authorizations(
    db,
    user: User,
    req: TemplateConfirmReq,
    sess: TemplateSession,
) -> None:
    """Record which historical wrong questions this template may correct.

    Any rejection rolls the whole transaction back.
    """
    from models_correction import TemplateQuestionAuthorization
    from sqlalchemy import inspect as sqlalchemy_inspect

    owns_db_session = not hasattr(db, "query")
    if owns_db_session:
        from database import SessionLocal
        db = SessionLocal()

    try:
        current_class_id = getattr(user, "current_class_id", None)
        requested_relations = _requested_correction_relations(
            db, user, req, sess, current_class_id,
        )
        authorization_table_exists = sqlalchemy_inspect(db.get_bind()).has_table(
            TemplateQuestionAuthorization.__tablename__
        )
        if requested_relations and not authorization_table_exists:
            raise HTTPException(503, detail="纠错授权表尚未初始化，请重启服务后重试")
        if requested_relations and not current_class_id:
            raise HTTPException(400, detail="请先选择当前班级，再确认历史错题纠错关系")
        kept_keys = _authorize_corrections(
            db, user, req, sess, requested_relations, current_class_id,
        )
        if authorization_table_exists:
            _revoke_dropped_authorizations(db, user, req.template_id, kept_keys)
            db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        if owns_db_session:
            db.close()


@router.post("/batch-grade/template/confirm")
def batch_grade_template_confirm(
    req: TemplateConfirmReq,
    background_tasks: BackgroundTasks = None,
    user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    """Save the teacher-edited template as this batch's authoritative draft.

    This operation is repeatable until student recognition starts. Private-bank
    persistence is a separate idempotent action.
    """
    if req.category not in VALID_GRADING_CATEGORIES:
        raise HTTPException(400, detail="category 必须是 daily 或 exam")
    req_exam_name = str(req.exam_name or "").strip()
    if req.category == "exam" and not req_exam_name:
        raise HTTPException(400, detail="考试模板必须填写考试名称")

    # Validate identifier/ownership before reporting payload-shape errors.  In
    # particular, a traversal-like identifier must never be treated as a
    # merely empty template submission.
    owned_template = _load_owned_template_session(req.template_id, user.id)
    new_pages = _submitted_template_pages(req, _saved_question_metadata(owned_template))

    with _locked_owned_template_session(req.template_id, user.id) as sess:
        _require_template_confirmable(sess)
        try:
            _merge_saved_question_fields(sess, req, new_pages)
        except ValueError as exc:
            confirm_rejection = str(exc)
        else:
            confirm_rejection = None
        if confirm_rejection is not None:
            raise HTTPException(422, detail=confirm_rejection)
        _require_template_scope_unchanged(sess, req, req_exam_name)
        _require_template_geometry_reviewed(sess)

        existing_by_index = _pages_by_index(sess.pages)
        proposed_by_index = _pages_by_index(new_pages)
        if set(existing_by_index) != set(proposed_by_index):
            raise HTTPException(
                409,
                detail="模板页序不能修改，请重新导入模板",
            )
        # Page role and routing identity are server-owned upload decisions;
        # the client payload never carries them. Legacy pages have neither.
        for page_index, proposed_page in proposed_by_index.items():
            proposed_page.update(page_metadata(existing_by_index[page_index]))
            if not page_is_graded(proposed_page) and proposed_page.get("questions"):
                raise HTTPException(409, detail="该页已标为不计分页，不能包含题目")

        if (
            normalize_template_mode(sess.template_mode, default="marked") == "blank"
            and not sess.page_artifacts
        ):
            raise HTTPException(
                409,
                detail="空白模板缺少视觉基准，请重新导入模板",
            )

        # New template sessions always carry server-owned visual artifacts,
        # regardless of whether the uploaded template happened to contain
        # handwriting. Keep the old no-artifact branch readable for legacy
        # sessions, but synchronize the confirmed question schema for every
        # artifact-backed template.
        if sess.page_artifacts or not sess.has_answers:
            _sync_confirmed_artifacts(sess, existing_by_index, proposed_by_index)
        _sync_confirmed_crop_plans(sess, new_pages)

        sess.pages = new_pages
        sess.knowledge_classifications = _kept_knowledge_classifications(sess, new_pages)
        sess.category = req.category
        sess.exam_name = req_exam_name
        sess.status = "confirmed"
        sess.confirm_current_contract(user.id)
        bank_imported = bool(sess.bank_imported_at)
        _save_template_correction_authorizations(db, user, req, sess)
        # Do not persist a confirmed template until every requested correction
        # authorization has passed ownership/state validation and the database
        # transaction has committed.  A 4xx must leave the on-disk session at
        # its previous authoritative state.
        sess.save_meta()
        confirmed_contract_hash = sess.confirmed_contract_hash
    if background_tasks is not None:
        background_tasks.add_task(
            _classify_confirmed_template_knowledge,
            req.template_id,
            user.id,
            confirmed_contract_hash,
        )
    return {
        "success": True,
        "template_id": req.template_id,
        "template_mode": normalize_template_mode(sess.template_mode, default="marked"),
        "column_mode": getattr(sess, "column_mode", "double") or "double",
        "page_count": len(new_pages),
        "bank_imported": bank_imported,
    }


# Bounded so the client's 60 s request timeout is never at risk.
TEMPLATE_BANK_IMPORT_KNOWLEDGE_WAIT_S = 25.0


@router.post("/batch-grade/template/{template_id}/bank-import")
async def batch_grade_template_bank_import(
    template_id: str,
    user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    """Persist a confirmed template to the private bank exactly once."""
    sess = await asyncio.to_thread(_load_owned_template_session, template_id, user.id)
    if sess.status in ("confirmed", "frozen") and sess.pages and not sess.bank_imported_at:
        # Saving the template already started its Flash classification. Let
        # in-flight labels land so the bank reuses them instead of queueing
        # the same text again; past the bound the bank worker takes over.
        try:
            await template_knowledge.ensure_session_knowledge(
                sess, classifier=_template_knowledge_classifier,
                timeout=TEMPLATE_BANK_IMPORT_KNOWLEDGE_WAIT_S,
            )
        except Exception as exc:
            logger.warning("模板入库前知识点分类暂缓 template=%s: %s", template_id, type(exc).__name__)
    return await asyncio.to_thread(_template_bank_import, template_id, user, db)


def _template_bank_import(template_id: str, user: User, db: Session):
    from services.answer_import import find_fuzzy_match

    sess = _load_owned_template_session(template_id, user.id)
    if sess.status not in ("confirmed", "frozen") or not sess.pages:
        raise HTTPException(409, detail="请先保存本次模板，再执行入库")
    current_hash = sess.content_hash()
    if sess.bank_imported_at:
        return {
            "success": True,
            "template_id": template_id,
            "added_to_bank": 0,
            "already_imported": True,
            "imported_count": sess.bank_imported_count,
            "template_changed_after_import": sess.bank_import_hash != current_hash,
        }

    added_to_bank = 0
    processed = 0
    try:
        effective_map = sess.build_answer_map()
        for page in sess.pages:
            for raw_question in page.get("questions", []):
                question_no = str(raw_question.get("question_no") or "").strip()
                effective = dict(raw_question)
                classified = effective_map.get(question_no) or {}
                if classified.get("chapter") and classified.get("topic"):
                    effective["chapter"] = classified["chapter"]
                    effective["topic"] = classified["topic"]
                spec = AnswerSpec.model_validate(effective)
                content = spec.question_text
                answer = spec.standard_answer
                if not content or not answer:
                    continue
                processed += 1
                with use_teaching_context(sess.teaching_context):
                    dup = find_fuzzy_match(db, content, user.id)
                if dup is not None:
                    dup.question = content
                    dup.answer = answer
                    dup.grading_method = spec.grading_method
                    dup.set_variants(spec.answer_variants)
                    dup.set_solution_steps(spec.solution_steps)
                    if spec.chapter:
                        dup.chapter = spec.chapter
                    if spec.topic:
                        dup.topic = spec.topic
                else:
                    qb = QuestionBank(
                        **record_teaching_fields(sess.teaching_context, chapter=spec.chapter, topic=spec.topic),
                        teacher_id=user.id,
                        is_public=False,
                        question=content,
                        answer=answer,
                        chapter=spec.chapter,
                        topic=spec.topic,
                        grading_method=spec.grading_method,
                        source="template_confirmed",
                    )
                    qb.set_variants(spec.answer_variants)
                    qb.set_solution_steps(spec.solution_steps)
                    db.add(qb)
                    added_to_bank += 1
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, detail=str(exc)) from exc
    except Exception as exc:
        db.rollback()
        logger.error("模板入库失败 template=%s: %s", template_id, exc, exc_info=True)
        raise HTTPException(500, detail="模板入库失败，请稍后重试") from exc

    # Do not let this endpoint's pre-import snapshot restore confirmed after a
    # concurrent recognition request has frozen the template.
    with _locked_owned_template_session(template_id, user.id) as latest_sess:
        latest_sess.bank_imported_at = datetime.now(timezone.utc).isoformat()
        latest_sess.bank_import_hash = current_hash
        latest_sess.bank_imported_count = processed
        latest_sess.save_meta()
    return {
        "success": True,
        "template_id": template_id,
        "added_to_bank": added_to_bank,
        "already_imported": False,
        "imported_count": processed,
        "template_changed_after_import": False,
    }
