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
from routers.grade_template_layout_runs import (
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
from routers.grade_template_artifacts import (
    _refresh_template_difference_artifact,
    _synchronized_page_artifact,
    _validate_template_drawing_geometry,
    _question_numbers,
)
from routers.grade_template_availability import (
    _batch_grade_template_service_availability,
    batch_grade_template_availability,
    _require_template_recognition_chain,
)
from routers.grade_template_availability import router as _availability_router
from routers.grade_template_corrections import (
    batch_grade_template_correction_candidates,
    _requested_correction_relations,
    _AUTHORIZED_RELATIONS,
    _authorize_corrections,
    _revoke_dropped_authorizations,
    _save_template_correction_authorizations,
)
from routers.grade_template_corrections import router as _corrections_router
from routers.grade_template_import_steps import (
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
from routers.grade_template_import_form import (
    _parse_template_page_roles,
    _TemplateImportForm,
    _parse_template_import_form,
    _TemplateImport,
    _save_template_import,
)
from routers.grade_template_import_pages import (
    _RecognizedTemplatePage,
    _recognized_template_page,
    _record_template_import_layout,
    _bind_template_import_artifact,
)
from routers.grade_template_import import (
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
from routers.grade_template_import import router as _import_router
from routers.grade_template_cut import (
    _validate_template_cut_request,
    _template_cut_response,
    batch_grade_template_cut_candidates_get,
    batch_grade_template_page_image,
    batch_grade_template_cut_candidates,
)
from routers.grade_template_cut import router as _cut_router

logger = logging.getLogger(__name__)
router = APIRouter()
router.include_router(_availability_router)
router.include_router(_corrections_router)
router.include_router(_import_router)
router.include_router(_cut_router)


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


# ════════════════════════════════════════════════════════════════════
#  多页一次性模板：recognize（OCR N 页）→ confirm（老师核对即权威 + 沉淀题库）
#  模板是本批次绝对权威；页数即类型数。学生卷匹配复用见 step 3。
# ════════════════════════════════════════════════════════════════════


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
