"""Save the teacher-checked template as the batch's authority: POST /batch-grade/template/confirm.

``batch_grade_template_confirm`` can be repeated until student recognition
starts.  In order:

- the submitted pages become frozen answer contracts
  (``_submitted_template_pages``, keeping server-owned directory metadata from
  ``_saved_question_metadata``), and the server-owned fields are put back and
  checked question by question (``_merge_saved_question_fields``);
- the template must still be confirmable, in the same scope, and fully
  reviewed (``_require_template_confirmable``,
  ``_require_template_scope_unchanged``, ``_require_template_geometry_reviewed``);
- each page artifact and column crop plan is frozen to the confirmed questions
  (``_sync_confirmed_artifacts``, ``_sync_confirmed_crop_plans``, by
  ``_pages_by_index``), and knowledge labels survive only for unchanged stems
  (``_kept_knowledge_classifications``);
- the correction authorizations are saved before the session is, and the
  knowledge classification resumes in the background
  (``_classify_confirmed_template_knowledge``).

Moved unchanged from routers/grade_template.py, which still exposes these names
and includes this module's router before its own routes.
"""

import copy
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models_user import User
from routers.auth import _require_user
from routers.grade_common import (
    _load_owned_template_session,
    _locked_owned_template_session,
    _template_knowledge_classifier,
)
from routers.grade_template_artifacts import (
    _refresh_template_difference_artifact,
    _validate_template_drawing_geometry,
)
from routers.grade_template_corrections import _save_template_correction_authorizations
from routers.grade_template_layout_runs import (
    _template_anchor_review_pending_pages,
    _template_geometry_review_pending_pages,
    _template_layout_review_pending_pages,
)
from routers.grade_template_models import TemplateConfirmReq, _contract_rejection_reason
from services import template_knowledge
from services.grading_contract import (
    AnswerSpec,
    VALID_GRADING_CATEGORIES,
    finalize_answer_spec_for_category,
    merge_directory_leaf_score_layer,
    reconcile_drawing_contract,
)
from services.template_anchor_crops import (
    AnchorCropPlanError,
    validate_anchor_crop_plan,
)
from services.template_column_layout import normalize_column_mode
from services.template_session import (
    TemplateSession,
    normalize_template_mode,
    page_is_graded,
    page_metadata,
)

# routers.grade_template's logger, so these records keep their logger name.
logger = logging.getLogger("routers.grade_template")
router = APIRouter()


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
