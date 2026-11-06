"""Batch grading routes (/api/batch-grade/...), assembled from routers/grade_*.py.

Each module owns one part of the flow and imports only the ones above it:

    grade_common        SSE events, task settling, batch checkpoints, owner checks
    grade_uploads       saving uploads, the .ocr-ready-v1.jpg contract, daily allowance
    grade_availability  GET  /batch-grade/availability, gates before grading work
    grade_report        GET  /batch-grade/report/{batch_id}, background report
    grade_contract      the confirmed template as the batch's answer authority
    grade_papers        one OCR result into a paper, identity plan, paper images
    grade_pipeline      per-paper grading while recognition runs, POST /batch-grade/confirm
    grade_paper_checks  identity options, orientation, exclusion, paper image routes
    grade_recognition   POST /batch-grade/recognize and .../{batch_id}/recognize/resume

Templates (routers/grade_template.py) and review after grading
(routers/grade_review.py) have their own routers, included by app.py.

This module only puts the routers above together in their original route
order.  The names imported below stay importable from routers.grade for older
callers and tests; new code imports them from the module that defines them.
"""

import logging

from fastapi import APIRouter

from routers.grade_availability import router as _availability_router
from routers.grade_recognition import router as _recognition_router
from routers.grade_paper_checks import router as _paper_checks_router
from routers.grade_pipeline import router as _pipeline_router
from routers.grade_report import router as _report_router

# Kept importable: shared plumbing (grade_common.py).
from routers.grade_common import (  # noqa: F401
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
# Kept importable: uploads (grade_uploads.py).
from routers.grade_uploads import (  # noqa: F401
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
# Kept importable: chain availability (grade_availability.py).
from routers.grade_availability import (  # noqa: F401
    _with_grading_hardware,
    _batch_grade_service_availability,
    batch_grade_availability,
    _require_batch_grade_chain,
    _batch_grade_scoring_availability,
    _require_batch_scoring_chain,
)
# Kept importable: batch report (grade_report.py).
from routers.grade_report import (  # noqa: F401
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
# Kept importable: template answer authority and grading contract (grade_contract.py).
from routers.grade_contract import (  # noqa: F401
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
# Kept importable: papers of a batch (grade_papers.py).
from routers.grade_papers import (  # noqa: F401
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
    _IdentityResolution,
    _prepare_anonymous_identity_plan,
    _prepare_identity_plan,
)
# Kept importable: grading and POST /batch-grade/confirm (grade_pipeline.py).
from routers.grade_pipeline import (  # noqa: F401
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
# Kept importable: checks before confirming (grade_paper_checks.py).
from routers.grade_paper_checks import (  # noqa: F401
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
# Kept importable: student recognition (grade_recognition.py).
from routers.grade_recognition import (  # noqa: F401
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

logger = logging.getLogger(__name__)
router = APIRouter()
# Route order as before the split: availability, recognition, the checks
# before confirming, confirm, and the report last.
router.include_router(_availability_router)
router.include_router(_recognition_router)
router.include_router(_paper_checks_router)
router.include_router(_pipeline_router)
router.include_router(_report_router)
