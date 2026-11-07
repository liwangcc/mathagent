"""Column layout candidates of a template: the last run, a page image, and a new or reused column plan.

- GET /batch-grade/template/{template_id}/cut-candidates
  (``batch_grade_template_cut_candidates_get``) returns the last complete
  server-owned layout run, or the one still running or failed.
- GET /batch-grade/template/{template_id}/pages/{page_index}/image
  (``batch_grade_template_page_image``) serves one verified canonical page for
  crop review.
- POST /batch-grade/template/{template_id}/cut-candidates
  (``batch_grade_template_cut_candidates``) checks the request
  (``_validate_template_cut_request``), reuses each page's frozen column plan
  unless the teacher forces a new one, otherwise asks the layout model, and
  binds the crop plan to the page artifact.
- ``_template_cut_response`` is the run as both cut-candidates routes return it.

Moved unchanged from routers/grade_template.py, which still exposes these names
and includes this module's router before its own routes.
"""

import copy
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from database import get_db
from models_user import User
from routers.auth import _require_user
from routers.grade_common import (
    _load_owned_template_session,
    _locked_owned_template_session,
)
from routers.grade_template_layout_runs import (
    _frozen_question_layout,
    _latest_template_geometry_review_metadata,
    _template_anchor_review_pending_pages,
    _template_geometry_review_pending_pages,
    _template_geometry_review_state,
    _template_layout_review_pending_pages,
)
from routers.grade_template_models import TemplateCutCandidatesReq
from services.template_anchor_crops import AnchorCropPlanError, build_anchor_crop_plan
from services.template_column_layout import (
    TemplateCutError,
    layout_needs_review,
    normalize_column_mode,
    recognize_column_layout,
)
from services.template_evidence import TemplateEvidenceError, verify_template_artifacts
from services.template_session import TemplateSession

router = APIRouter()


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
