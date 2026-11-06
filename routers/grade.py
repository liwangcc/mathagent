"""批改接口 — 支持日常作业/考试两类导入 + 批量批改"""

import logging

from fastapi import APIRouter

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
from routers.grade_papers import (
    _IdentityResolution,
    _prepare_anonymous_identity_plan,
    _prepare_identity_plan,
)
from routers.grade_recognition import router as _recognition_router
from routers.grade_recognition import (
    _checkpoint_recognition_cleanup,
    _ensure_batch_upload_inspected,
    _bounded_batch_upload_inspections,
    _discard_unstarted_batch,
    _RECOGNITION_DISCONNECTED,
    _RecognitionLease,
    _RecognitionFlavor,
    _INITIAL_RECOGNITION,
    _RESUMED_RECOGNITION,
    _RecognitionRun,
    _inspected_upload_count,
    _grading_drain_events,
    _complete_recognition,
    _sse_response,
    batch_grade_recognize,
    _parse_recognition_request,
    _template_for_new_batch,
    _check_requested_image_allowance,
    _new_template_batch,
    _save_new_batch_uploads,
    _register_batch_uploads,
    _freeze_template_for_batch,
    _reserve_batch_images,
    _fail_unrecognized_batch,
    _initial_recognition_paper_payload,
    _initial_recognition_events,
    _OCR_RESUMABLE_BATCH_STATES,
    _ocr_failure_payload,
    _recognition_paper_payload,
    _pause_batch_for_accelerator,
    batch_grade_resume_recognition,
    _require_resumable_recognition,
    _resume_refusal,
    _ocr_attempts_exhausted,
    _stop_resume_without_work,
    _restart_recognition,
    _redispatch_checkpointed_papers,
    _fail_unrecognized_resume,
    _resumed_recognition_events,
    _recognize_upload,
    _ocr_outcome_message,
    _ocr_and_grading_events,
    _ocr_job_worker,
    _ocr_phase_events,
)
from routers.grade_pipeline import router as _pipeline_router
from routers.grade_pipeline import (
    ConfirmReq,
    _REVIEW_FINISHED_STATUSES,
    batch_grade_confirm,
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

logger = logging.getLogger(__name__)
router = APIRouter()
# GET /batch-grade/availability stays this router's first route.
router.include_router(_availability_router)
# POST /batch-grade/recognize and .../recognize/resume come next.
router.include_router(_recognition_router)


# Identity options, orientation, exclusion and paper images keep their place
# after the recognition routes.
router.include_router(_paper_checks_router)
# POST /batch-grade/confirm follows them.
router.include_router(_pipeline_router)


# GET /batch-grade/report/{batch_id} stays this router's last route.
router.include_router(_report_router)
