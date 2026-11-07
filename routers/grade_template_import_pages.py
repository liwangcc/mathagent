"""What one recognized template page becomes in the session.

- ``_recognized_template_page`` validates the page's model output, numbers its
  questions within the whole paper and splits its geometry into what is
  trusted and what the teacher must review (``_RecognizedTemplatePage``).
- ``_record_template_import_layout`` adds the page to the import's automatic
  layout run; ``_bind_template_import_artifact`` stores its frozen artifact
  and, when the layout is trusted, its column crop plan.

Moved unchanged from routers/grade_template.py, which still exposes these names.
"""

import copy
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from routers.grade_template_artifacts import _question_numbers
from routers.grade_template_import_form import _TemplateImport
from routers.grade_template_import_steps import (
    _TemplateRecognitionFailure,
    _template_recognition_failure,
)
from services.grading_contract import AnswerSpec
from services.processing_errors import failure_from_public
from services.template_anchor_crops import build_anchor_crop_plan
from services.template_column_layout import layout_needs_review
from services.template_evidence import (
    normalize_template_handwriting_regions,
    verify_template_artifacts,
)
from services.template_page_numbering import globalize_page_numbers

# routers.grade_template's logger, so these records keep their logger name.
logger = logging.getLogger("routers.grade_template")


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
