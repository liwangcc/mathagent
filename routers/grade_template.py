"""Template routes (/api/batch-grade/template/...), assembled from routers/grade_template_*.py.

A template goes import -> column layout -> locating edits -> confirm -> bank.
Each module owns one part and imports only the ones above it:

    grade_template_models            request bodies; why a submitted question is refused
    grade_template_layout_runs       what the session's layout runs say about each page
    grade_template_artifacts         page artifacts follow the page's questions
    grade_template_availability      GET  .../template/availability, the gate before an import
    grade_template_corrections       POST .../template/correction-candidates, correction authorizations
    grade_template_import_steps      import deadline, heartbeats, teacher-facing failures
    grade_template_import_form       the upload form, stored pages, the import job
    grade_template_import_pages      one recognized page into the session
    grade_template_import            POST .../template/recognize, GET .../{template_id}/recognition
    grade_template_cut               GET/POST .../{template_id}/cut-candidates, page image
    grade_template_layout_overrides  PUT  .../{template_id}/layout-overrides
    grade_template_questions         PUT  .../{template_id}/questions
    grade_template_confirm           POST .../template/confirm
    grade_template_bank              POST .../{template_id}/bank-import

They also use the shared layers of the batch-grading family (grade_common,
grade_uploads, grade_availability, grade_contract); routers/README.md is the
reading map.

This module only puts the routers above together in their original route
order.  The names imported below stay importable from routers.grade_template
for older callers and tests; new code imports them from the module that
defines them.
"""

from routers.grade_template_availability import router as _availability_router
from routers.grade_template_corrections import router as _corrections_router
from routers.grade_template_import import router as _import_router
from routers.grade_template_cut import router as _cut_router
from routers.grade_template_layout_overrides import router as _layout_overrides_router
from routers.grade_template_questions import router as _questions_router
from routers.grade_template_confirm import router as _confirm_router
from routers.grade_template_bank import router as _bank_router

# Kept importable: request bodies (grade_template_models.py).
from routers.grade_template_models import (  # noqa: F401
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
# Kept importable: layout-run queries (grade_template_layout_runs.py).
from routers.grade_template_layout_runs import (  # noqa: F401
    _template_geometry_review_state,
    _layout_result_from_frozen_question_plan,
    _latest_page_layout_draft,
    _TEMPLATE_GEOMETRY_REVIEW_METADATA_KEYS,
    _latest_template_geometry_review_metadata,
    _plan_column_bounds,
    _template_layout_review_pending_pages,
    _template_anchor_review_pending_pages,
    _template_geometry_review_pending_pages,
    _frozen_question_layout,
    _drawing_geometry_unresolved,
)
# Kept importable: artifacts and questions in step (grade_template_artifacts.py).
from routers.grade_template_artifacts import (  # noqa: F401
    _refresh_template_difference_artifact,
    _synchronized_page_artifact,
    _validate_template_drawing_geometry,
    _question_numbers,
)
# Kept importable: availability and the import gate (grade_template_availability.py).
from routers.grade_template_availability import (  # noqa: F401
    _batch_grade_template_service_availability,
    batch_grade_template_availability,
    _require_template_recognition_chain,
)
# Kept importable: historical correction authorizations (grade_template_corrections.py).
from routers.grade_template_corrections import (  # noqa: F401
    batch_grade_template_correction_candidates,
    _requested_correction_relations,
    _AUTHORIZED_RELATIONS,
    _authorize_corrections,
    _revoke_dropped_authorizations,
    _save_template_correction_authorizations,
)
# Kept importable: import pacing and failures (grade_template_import_steps.py).
from routers.grade_template_import_steps import (  # noqa: F401
    _TEMPLATE_IMPORT_PAGE_HARD_CAP_S,
    _TEMPLATE_IMPORT_PER_PAGE_DEADLINE_S,
    _TEMPLATE_IMPORT_BASE_DEADLINE_S,
    _TEMPLATE_IMPORT_PER_ADDITIONAL_PAGE_S,
    _TEMPLATE_IMPORT_MAX_DEADLINE_S,
    _TEMPLATE_COMPONENT_HEARTBEAT_S,
    _TEMPLATE_MODEL_SAVE_RESERVE_S,
    _TemplateRecognitionFailure,
    _TemplateImportDeadlineExceeded,
    _template_import_clock,
    _template_import_deadline_seconds,
    _template_page_model_deadline,
    _template_model_progress_message,
    _template_import_deadline_failure,
    _poll_template_component,
    _run_template_cpu_component,
    _template_recognition_failure,
    _TemplateImportSteps,
    _template_heartbeat,
)
# Kept importable: upload form and the import job (grade_template_import_form.py).
from routers.grade_template_import_form import (  # noqa: F401
    _parse_template_page_roles,
    _TemplateImportForm,
    _parse_template_import_form,
    _TemplateImport,
    _save_template_import,
)
# Kept importable: recognized pages (grade_template_import_pages.py).
from routers.grade_template_import_pages import (  # noqa: F401
    _RecognizedTemplatePage,
    _recognized_template_page,
    _record_template_import_layout,
    _bind_template_import_artifact,
)
# Kept importable: the import stream (grade_template_import.py).
from routers.grade_template_import import (  # noqa: F401
    _TEMPLATE_EVENT_BUFFER_SIZE,
    _TEMPLATE_RECOGNITION_TASKS,
    _TemplateRecognitionEventBuffer,
    _track_template_recognition_task,
    _relay_template_recognition,
    _template_recognition_status_payload,
    batch_grade_template_recognition_status,
    batch_grade_template_recognize,
    _fail_template_import,
    _TemplatePageImport,
    _correct_template_page_events,
    _template_page_identity,
    _template_layout_events,
    _template_model_events,
    _save_recognized_template_page,
    _template_page_events,
    _freeze_template_page_events,
    _finish_template_import,
    _template_import_events,
)
# Kept importable: column layout candidates (grade_template_cut.py).
from routers.grade_template_cut import (  # noqa: F401
    _validate_template_cut_request,
    _template_cut_response,
    batch_grade_template_cut_candidates_get,
    batch_grade_template_page_image,
    batch_grade_template_cut_candidates,
)
# Kept importable: saving a page's locating edits (grade_template_layout_overrides.py).
from routers.grade_template_layout_overrides import (  # noqa: F401
    _drawing_schema_complete,
    _teacher_question_anchors,
    _layout_answer_regions,
    _layout_handwriting_regions,
    _layout_question_schema,
    _layout_slot_questions,
    _layout_contract_rejection,
    _save_layout_to_page_questions,
    _approve_page_layout_runs,
    batch_grade_template_layout_overrides,
)
# Kept importable: adding and removing question numbers (grade_template_questions.py).
from routers.grade_template_questions import (  # noqa: F401
    _page_question_numbers,
    _rebuild_renumbered_artifacts,
    _bind_renumbered_artifacts,
    batch_grade_template_question_edit,
)
# Kept importable: confirming the template (grade_template_confirm.py).
from routers.grade_template_confirm import (  # noqa: F401
    _classify_confirmed_template_knowledge,
    _saved_question_metadata,
    _submitted_template_pages,
    _require_template_confirmable,
    _merge_saved_question_fields,
    _require_template_scope_unchanged,
    _require_template_geometry_reviewed,
    _pages_by_index,
    _sync_confirmed_artifacts,
    _sync_confirmed_crop_plans,
    _kept_knowledge_classifications,
    batch_grade_template_confirm,
)
# Kept importable: the private bank import (grade_template_bank.py).
from routers.grade_template_bank import (  # noqa: F401
    TEMPLATE_BANK_IMPORT_KNOWLEDGE_WAIT_S,
    batch_grade_template_bank_import,
    _template_bank_import,
)
# Kept importable: everything this module imported before the split;
# tests and older callers read some of these names through it.
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

logger = logging.getLogger(__name__)
router = APIRouter()
router.include_router(_availability_router)
router.include_router(_corrections_router)
router.include_router(_import_router)
router.include_router(_cut_router)
router.include_router(_layout_overrides_router)
router.include_router(_questions_router)
router.include_router(_confirm_router)
router.include_router(_bank_router)
