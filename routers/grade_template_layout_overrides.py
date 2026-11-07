"""Save one template page's locating edits: PUT /batch-grade/template/{template_id}/layout-overrides.

``batch_grade_template_layout_overrides`` turns the teacher's columns into a new
revision of the page's layout draft and freezes its crop plan, then replaces
the page's question-number boxes (``_teacher_question_anchors``), answer and
handwriting regions (``_layout_answer_regions``, ``_layout_handwriting_regions``)
and drawing geometry (``_layout_question_schema``; required while the drawing
stage is still open, ``_drawing_schema_complete``).  The edited questions must
pass their answer contract (``_layout_slot_questions``,
``_layout_contract_rejection``) before anything is saved; then the geometry is
copied into the page's questions (``_save_layout_to_page_questions``) and every
layout run of the page drops its review leftovers (``_approve_page_layout_runs``).

Moved unchanged from routers/grade_template.py, which still exposes these names
and includes this module's router before its own routes.
"""

import copy

from fastapi import APIRouter, Depends, HTTPException

from models_user import User
from routers.auth import _require_user
from routers.grade_common import _locked_owned_template_session
from routers.grade_contract import _template_handwriting_regions_for_mode
from routers.grade_template_artifacts import (
    _question_numbers,
    _refresh_template_difference_artifact,
    _validate_template_drawing_geometry,
)
from routers.grade_template_layout_runs import (
    _drawing_geometry_unresolved,
    _latest_page_layout_draft,
    _plan_column_bounds,
)
from routers.grade_template_models import (
    TemplateAnswerRegionOverride,
    TemplateHandwritingRegionOverride,
    TemplateLayoutOverridesReq,
    TemplateQuestionAnchorOverride,
    _contract_rejection_reason,
)
from services.grading_contract import (
    AnswerSpec,
    merge_directory_leaf_score_layer,
    reconcile_drawing_contract,
)
from services.template_anchor_crops import AnchorCropPlanError, build_anchor_crop_plan
from services.template_column_layout import revise_layout_draft
from services.template_evidence import (
    TemplateEvidenceError,
    normalize_question_anchors,
    normalize_template_handwriting_regions,
    verify_template_artifacts,
)
from services.template_session import (
    CONTENT_CHANGED_REASON,
    GEOMETRY_CHANGED_REASON,
    TemplateSession,
    normalize_template_mode,
)

router = APIRouter()


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
