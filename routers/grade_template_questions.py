"""Add or remove one question number on a template page: PUT /batch-grade/template/{template_id}/questions.

``batch_grade_template_question_edit`` inserts a number, shifting every later
question across pages, or deletes one without renumbering.  Every page whose
numbers changed (``_page_question_numbers``) gets its artifact rebuilt and
verified (``_rebuild_renumbered_artifacts``), and the layout runs point at the
rebuilt artifacts (``_bind_renumbered_artifacts``); the page that gained a
question waits for the teacher to place its boxes.

Moved unchanged from routers/grade_template.py, which still exposes these names
and includes this module's router before its own routes.
"""

from fastapi import APIRouter, Depends, HTTPException

from models_user import User
from routers.auth import _require_user
from routers.grade_common import _locked_owned_template_session
from routers.grade_template_artifacts import (
    _refresh_template_difference_artifact,
    _synchronized_page_artifact,
)
from routers.grade_template_layout_runs import _layout_result_from_frozen_question_plan
from routers.grade_template_models import TemplateQuestionEditReq
from services.template_anchor_crops import AnchorCropPlanError
from services.template_evidence import TemplateEvidenceError, verify_template_artifacts
from services.template_session import (
    TemplateSession,
    delete_question as delete_template_question,
    insert_question as insert_template_question,
)

router = APIRouter()


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
