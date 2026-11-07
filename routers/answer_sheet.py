"""Independent answer-sheet grading workflow API."""

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
from routers.answer_sheet_common import (
    _ANSWER_SHEET_HEARTBEAT_SECONDS,
    _answer_sheet_sse,
    _answer_sheet_stream,
    _with_answer_sheet_heartbeats,
    _load_owned,
    _locked_owned,
    _serialized_session_mutation,
    _answer_sheet_writes_enabled,
    _requires_answer_sheet_writes,
    _run_answer_sheet_image_work,
)
from routers.answer_sheet_availability import (
    _with_answer_sheet_hardware,
    _answer_sheet_service_availability,
    answer_sheet_availability,
    _require_answer_sheet_chain,
)
from routers.answer_sheet_availability import router as _availability_router
from routers.answer_sheet_assets import (
    _ALLOWED_EXTENSIONS,
    _CLIENT_OCR_READY_SUFFIX,
    _CLIENT_OCR_READY_MAX_EDGE,
    _RAW_PAPER_EXPIRED_MESSAGE,
    _remove_upload_path,
    _save_images,
    _session_asset_path,
    _require_raw_assets_available,
    _layout_asset_paths,
    _remove_layout_assets,
)
from routers.answer_sheet_student_state import (
    _GRADING_DRAFT_CHECKPOINT_KEY,
    _STUDENT_UPLOAD_ACTIVE_STATES,
    _valid_student_upload_id,
    _student_upload_is_active,
    _require_no_student_upload,
    _student_recognition_source_path,
)
from routers.answer_sheet_pairing import (
    _ORDERED_DUPLEX_PAIRING,
    _ORDERED_PAIRING_REASONS,
    _add_pairing_reason,
    _clear_pairing_reasons,
    _ordered_duplex_layout_ids,
    _apply_student_page_pairing,
    _apply_ordered_duplex_identity,
)
from routers.answer_sheet_sessions import (
    SessionCreateRequest,
    PaperTemplateRegisterRequest,
    TemplateAliasRequest,
    _owned_class,
    _inherited_class_id,
    _batch_label_binding,
    create_session,
    list_sessions,
    list_paper_templates,
    list_paper_template_sources,
    register_paper_template,
    get_paper_template,
    set_paper_template_alias,
    get_session,
    template_code_history,
    delete_session,
)
from routers.answer_sheet_sessions import router as _sessions_router
from routers.answer_sheet_source import (
    AnswerConfirmRequest,
    upload_source,
    _source_recognition_is_resume,
    _source_recognition_phase,
    recognize_source,
    _source_page_checkpoint,
    _recognize_source_stream_events,
    recognize_source_stream,
    confirm_answers,
)
from routers.answer_sheet_source import router as _source_router
from routers.answer_sheet_layout import (
    TemplateCodeRequest,
    BubbleGridCell,
    BubbleGridKey,
    BubbleGridConfirmation,
    BubbleGridConfirmRequest,
    BubbleGridDiscardRequest,
    upload_layout,
    process_layout_acceleration,
    layout_image,
    locate_layout_regions,
    use_layout_template_code,
    publish_layout_template_code,
    confirm_layout_bubble_grids,
    discard_layout_bubble_grids,
)
from routers.answer_sheet_layout import router as _layout_router
from routers.answer_sheet_student_upload import (
    StudentUploadStartRequest,
    _accelerator_managed_orientation,
    _student_upload_pages,
    _require_student_upload,
    _student_upload_checkpoint_complete,
    _registration_source,
    _batch_page_registration_current,
    _register_batch_page,
    _remove_superseded_registration,
    start_student_upload,
    discard_student_upload,
    upload_students,
    process_student_upload_stream,
)
from routers.answer_sheet_student_pages import (
    OrientationConfirmRequest,
    student_page_image,
    _EVIDENCE_MEDIA_TYPES,
    student_page_evidence_index,
    student_page_evidence_file,
    confirm_student_orientation,
)
from routers.answer_sheet_student_upload import router as _student_upload_router
from routers.answer_sheet_student_pages import router as _student_pages_router
from routers.answer_sheet_student_files import (
    _ALIGNED_EVIDENCE_RE,
    _ANSWER_CROP_RE,
    _STUDENT_RECOGNITION_ATTEMPT_RE,
    _cleanup_replaced_student_assets,
    _cleanup_failed_aligned_asset,
    _cleanup_student_recognition_attempt,
    _restore_student_page,
    _discard_student_asset_replacement,
    _commit_student_asset_replacement,
    _finish_student_accelerator_replacement,
)
from routers.answer_sheet_recognize_page import (
    _ANSWER_SHEET_ACCELERATOR_WAIT_SECONDS,
    load_rgb_image,
    crop_normalized_rgb,
    decode_machine_code,
    read_exam_no_marks,
    read_choice_marks,
    AnswerSheetAcceleratorState,
    _unique_student_for_code,
    _extract_student_page_regions,
    _student_recognition_signature,
    _student_page_checkpoint_complete,
    _student_accelerator_state,
    _student_accelerator_message,
    _identity_row_for_transcription,
    _answer_row_for_transcription,
    _required_answer_source_complete,
    _recognize_one_student_page_accelerated,
    _recognize_one_student_page,
)
from routers.answer_sheet_recognition import (
    _ANSWER_SHEET_OCR_BATCH_DEADLINE_SECONDS,
    ReviewConfirmRequest,
    _StudentRecognitionWorkerError,
    _persist_student_recognition_result,
    _student_recognition_completion_stream,
    recognize_students,
    recognize_students_stream,
    confirm_student_review,
)
from routers.answer_sheet_recognition import router as _recognition_router
from routers.answer_sheet_grading import (
    _IDENTITY_READY_STATUSES,
    GradingDecision,
    GradingDecisionRequest,
    _verified_paper_template,
    _result_scoring,
    _verified_bubble_grids,
    build_grading_draft,
    _grading_groups,
    _grade_answer_sheet_group,
    _grading_checkpoint_drafts,
    build_grading_draft_stream,
    save_grading_decision,
    archive_grading_drafts,
)
from routers.answer_sheet_grading import router as _grading_router

router = APIRouter(prefix="/answer-sheet")
router.include_router(_availability_router)
router.include_router(_sessions_router)
router.include_router(_source_router)
router.include_router(_layout_router)
router.include_router(_student_upload_router)
router.include_router(_student_pages_router)
router.include_router(_recognition_router)
router.include_router(_grading_router)
logger = logging.getLogger(__name__)


class LayoutSaveRequest(BaseModel):
    answer_regions: list[dict] = Field(default_factory=list, max_length=200)
    identity_regions: list[dict] = Field(default_factory=list, max_length=100)


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
