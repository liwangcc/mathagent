"""Before confirming a batch, the teacher checks each recognized paper.

- GET /batch-grade/{batch_id}/identity-options: the roster students a paper
  can be assigned to.
- GET /batch-grade/{batch_id}/orientation (``_orientation_state_payload``):
  every paper's orientation, OCR and exclusion state.
- POST .../paper/{paper_id}/exclude: drop a paper whose OCR failed.
- POST .../paper/{paper_id}/orientation-confirm: accept the page as it is, or
  take the teacher's rotated upload (``_save_client_orientation_upload``) and
  recognize it again (``_begin_orientation_retry`` ->
  ``_recognize_reoriented_upload`` -> ``_finish_orientation_retry``), once per
  action id.
- GET .../paper/{paper_id}/image: the paper image, plain or with review marks.

Moved unchanged from routers/grade.py, which still exposes these names and
includes this module's router after the recognition routes.
"""

import asyncio
import copy
import hashlib
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from config import MAX_UPLOAD_FILE_SIZE, UPLOAD_DIR
from database import get_db
from models import Student
from models_user import User
from routers.auth import _require_user
from routers.grade_common import (
    _load_owned_template_session,
    _require_owned_batch,
    _save_quick_batch_meta,
)
from routers.grade_contract import (
    _build_template_paper_cache,
    _confirmation_questions,
    _initialize_batch_answer_map,
    _single_page_template_failure_questions,
)
from routers.grade_papers import (
    _batch_paper_display_path,
    _batch_review_image_path,
    _class_roster_names,
    _ingest_recognized_paper,
    _recognize_batch_upload,
    _refresh_identity_inheritance,
)
from routers.grade_uploads import (
    _client_ocr_ready_orientation,
    _orientation_diagnostics,
    _stream_to_disk_capped_with_sha256,
    _validate_client_ocr_ready_upload,
)
from services.ocr_service import (
    ocr_failure_result,
    reset_vision_phase_key,
    set_vision_phase_key,
)
from services.paper_cache import PaperCache
from services.paper_orientation import orientation_is_ready, sha256_file
from services.quick_grader import (
    BatchOperationBusy,
    OCR_MAX_JOB_ATTEMPTS,
    QuickBatch,
    batch_ocr_summary,
    batch_operation_lock,
    mark_ocr_outcome,
    ocr_job_attempts,
)
from services.template_session import TemplateSession

# routers.grade's logger, so these records keep their logger name.
logger = logging.getLogger("routers.grade")
router = APIRouter()


@router.get("/batch-grade/{batch_id}/identity-options")
def batch_grade_identity_options(
    batch_id: str,
    user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    """Return the live roster for the class frozen into an owned batch."""
    batch = QuickBatch.load_meta(batch_id, UPLOAD_DIR)
    _require_owned_batch(batch, user)
    if batch.identity_mode == "anonymous":
        raise HTTPException(409, detail="无身份批阅不使用学生名单")
    students = db.query(Student).filter(
        Student.teacher_id == user.id,
        Student.class_id == batch.class_id,
    ).order_by(Student.id.asc()).all()
    return {
        "batch_id": batch.batch_id,
        "class_id": batch.class_id,
        "students": [
            {
                "student_id": student.id,
                "name": student.name,
                "student_no": student.student_no or "",
                "class_id": student.class_id,
            }
            for student in students
        ],
    }


def _orientation_state_payload(batch: QuickBatch) -> dict:
    papers_by_id = {
        str(item.get("paper_id") or ""): item for item in batch.papers
    }
    jobs_by_id = {
        str(item.get("paper_id") or ""): item for item in batch.ocr_jobs
        if item.get("paper_id")
    }
    frozen_identity_plan = (
        batch.grading_contract.get("identity_plan") or {}
        if isinstance(batch.grading_contract, dict)
        else {}
    )
    rows = []
    for upload in batch.uploads:
        paper_id = str(upload.get("paper_id") or "")
        paper = papers_by_id.get(paper_id, {})
        job = jobs_by_id.get(paper_id)
        if job is None:
            active_path = str(upload.get("image_path") or "")
            job = next(
                (
                    item for item in batch.ocr_jobs
                    if str(item.get("image_path") or "") == active_path
                ),
                {},
            )
        merged = {**paper, **upload}
        assignment = frozen_identity_plan.get(paper_id) or {}
        questions = paper.get("questions") or []
        matched = sum(
            1 for question in questions
            if batch.answer_map.get(str(question.get("question_no") or ""), {}).get("matched")
        )
        row = {
            "batch_id": batch.batch_id,
            "paper_id": paper_id,
            "filename": str(upload.get("filename") or paper.get("filename") or "unknown"),
            "student_name": str(paper.get("student_name") or ""),
            "student_no": str(paper.get("student_no") or ""),
            "identity": paper.get("identity") or {},
            # 让老师看得见身份线索是"纸上 OCR"还是"文件名"，便于核对贴错图的情况。
            "identity_clue_source": str(paper.get("identity_clue_source") or ""),
            "identity_resolution": (
                {
                    "paper_id": paper_id,
                    "action": str(assignment.get("action") or ""),
                    "student_id": assignment.get("student_id"),
                    "confirmed_name": (
                        str(assignment.get("name") or "")
                        if assignment.get("action") == "create" else ""
                    ),
                    "confirmed_student_no": (
                        str(assignment.get("student_no") or "")
                        if assignment.get("action") == "create" else ""
                    ),
                }
                if assignment else None
            ),
            "questions_count": len(questions),
            "matched_count": matched,
            "ocr_status": str(job.get("status") or "queued"),
            "error": (
                _EXCLUDED_PAPER_MESSAGE
                if job.get("status") == "excluded"
                else str(paper.get("ocr_error") or job.get("last_error") or "")
            ),
            "requires_manual_review": (
                not orientation_is_ready(merged)
                or str(job.get("status") or "") == "needs_review"
            ),
            "manual_quarter_turns": int(upload.get("manual_quarter_turns") or 0),
            "orientation_confirmed_at": str(upload.get("orientation_confirmed_at") or ""),
            "original_preserved": bool(
                upload.get("original_image_path") and upload.get("source_sha256")
            ),
            **_orientation_diagnostics(merged),
        }
        rows.append(row)

    summary = batch_ocr_summary(batch)
    orientation_ready = bool(rows) and all(orientation_is_ready(row) for row in rows)
    summary["can_grade"] = bool(summary["can_grade"] and orientation_ready)
    return {
        "batch_id": batch.batch_id,
        "teaching_context": copy.deepcopy(batch.teaching_context),
        "status": batch.status,
        "identity_mode": batch.identity_mode,
        "identity_inheritance": batch.identity_inheritance,
        "category": batch.category,
        "exam_name": batch.exam_name,
        "mode": batch.grade_mode,
        "source_mode": batch.source_mode,
        "template_id": batch.template_id,
        "template_mode": batch.template_mode,
        "papers": rows,
        "questions": batch.confirmation_questions if summary["can_grade"] else [],
        **summary,
    }


@router.get("/batch-grade/{batch_id}/orientation")
def batch_grade_orientation_state(
    batch_id: str,
    user: User = Depends(_require_user),
):
    """Hydrate an owned orientation review after refresh or navigation."""
    batch = QuickBatch.load_meta(batch_id, UPLOAD_DIR)
    _require_owned_batch(batch, user)
    return _orientation_state_payload(batch)


_EXCLUDED_PAPER_MESSAGE = "已由教师排除，不参与本批批阅"


@router.post("/batch-grade/{batch_id}/paper/{paper_id}/exclude")
async def batch_grade_exclude_failed_paper(
    batch_id: str,
    paper_id: str,
    user: User = Depends(_require_user),
):
    """Let the teacher remove one unrecoverable OCR failure from this batch.

    Without this, a single page whose OCR failed for good (for example a photo
    that matches no page of a multi-page template) blocks grading for every
    other paper. Only an explicit teacher action resolves it: the page is never
    graded, never written to records, and stays listed as excluded.
    """
    try:
        with batch_operation_lock(batch_id, UPLOAD_DIR):
            batch = QuickBatch.load_meta(batch_id, UPLOAD_DIR)
            _require_owned_batch(batch, user)
            if str(batch.source_mode or "").strip().lower() != "template" or not batch.template_id:
                raise HTTPException(409, detail="只有模板批次可以排除识别失败的试卷")
            job = next(
                (
                    item for item in batch.ocr_jobs
                    if str(item.get("paper_id") or "") == paper_id
                ),
                None,
            )
            if job is None:
                raise HTTPException(404, detail="试卷不存在或已被清理")
            if job.get("status") == "excluded":
                # A repeated click (or a lost response) returns the durable state.
                return _orientation_state_payload(batch)
            if batch.status not in {"ocr_partial_failed", "failed"}:
                raise HTTPException(409, detail="只有识别未完成的批次可以排除失败试卷")
            if job.get("status") != "failed":
                raise HTTPException(409, detail="只能排除识别失败的试卷")
            if bool(job.get("retryable")) and ocr_job_attempts(job) < OCR_MAX_JOB_ATTEMPTS:
                raise HTTPException(409, detail="该试卷仍可自动重试，请先继续处理未完成试卷")
            if any(str(paper.get("paper_id") or "") == paper_id for paper in batch.papers):
                raise HTTPException(409, detail="该试卷已有识别结果，不能排除")

            job.update({
                "status": "excluded",
                "retryable": False,
                "excluded_by": user.id,
                "excluded_at": datetime.now(timezone.utc).isoformat(),
            })
            # An excluded page no longer breaks the inheritance chain.
            _refresh_identity_inheritance(batch)
            summary = batch_ocr_summary(batch)
            if summary["can_grade"] and all(
                orientation_is_ready(paper) for paper in batch.papers
            ):
                template_session = _load_owned_template_session(batch.template_id, user.id)
                _initialize_batch_answer_map(batch, user.id, template_session)
                batch.confirmation_questions = _confirmation_questions(batch)
                batch.status = "ocr_done"
                batch.error = None
            batch.pending_uploads = []
            await _save_quick_batch_meta(batch)
            logger.info(
                "教师排除失败试卷 batch=%s paper=%s excluded=%d can_grade=%s",
                batch.batch_id, paper_id, summary["excluded_papers"], summary["can_grade"],
            )
            return _orientation_state_payload(batch)
    except BatchOperationBusy as exc:
        raise HTTPException(409, detail="该批次正在执行另一项操作，请稍后重试") from exc


async def _save_client_orientation_upload(
    upload: UploadFile,
    *,
    batch_dir: str,
    paper_id: str,
    action_id: str,
    source_sha256: str,
    quarter_turns_clockwise: int,
) -> tuple[str, str]:
    """Persist a browser-rendered rotation without decoding image pixels."""
    filename = os.path.basename(str(upload.filename or ""))
    if not filename.lower().endswith(".ocr-ready-v1.jpg"):
        raise HTTPException(
            400,
            detail={
                "error": "CLIENT_IMAGE_PREPROCESS_REQUIRED",
                "message": "方向确认必须提交浏览器生成的 .ocr-ready-v1.jpg 文件",
            },
        )

    root = Path(batch_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    paper_token = hashlib.sha256(paper_id.encode("utf-8")).hexdigest()[:12]
    action_token = hashlib.sha256(action_id.encode("utf-8")).hexdigest()[:12]
    destination = root / (
        f"teacher-oriented-{paper_token}-{source_sha256[:16]}-"
        f"q{quarter_turns_clockwise}-{action_token}.ocr-ready-v1.jpg"
    )
    temporary = root / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    try:
        size, content_sha256 = await _stream_to_disk_capped_with_sha256(
            upload,
            str(temporary),
            MAX_UPLOAD_FILE_SIZE,
        )
        if size < 0:
            raise HTTPException(413, detail="方向确认文件超过大小限制")
        if size == 0:
            raise HTTPException(400, detail="方向确认文件为空")
        validation = await asyncio.to_thread(
            _validate_client_ocr_ready_upload,
            filename,
            str(temporary),
        )
        if not validation.get("safe"):
            raise HTTPException(
                400,
                detail={
                    "error": "CLIENT_IMAGE_PREPROCESS_REQUIRED",
                    "message": validation.get("reason") or "客户端方向文件校验失败",
                },
            )
        if destination.exists():
            existing_hash = await asyncio.to_thread(sha256_file, str(destination))
            if existing_hash != content_sha256:
                raise HTTPException(409, detail="同一方向确认请求对应的客户端文件已变化")
            temporary.unlink(missing_ok=True)
        else:
            os.replace(str(temporary), str(destination))
        return str(destination), content_sha256
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(503, detail="服务器暂时无法保存方向确认文件") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


@router.post("/batch-grade/{batch_id}/paper/{paper_id}/orientation-confirm")
async def batch_grade_confirm_orientation(
    batch_id: str,
    paper_id: str,
    action_id: str = Form(...),
    quarter_turns_clockwise: int = Form(...),
    file: UploadFile | None = File(None),
    user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    """Persist a client-rendered teacher rotation, then retry one paper."""
    action_id = _orientation_action_id(action_id, quarter_turns_clockwise)
    try:
        with batch_operation_lock(batch_id, UPLOAD_DIR):
            return await _confirm_orientation_locked(
                batch_id,
                paper_id,
                action_id,
                quarter_turns_clockwise,
                file,
                user,
                db,
            )
    except BatchOperationBusy as exc:
        raise HTTPException(409, detail="该批次正在执行另一项操作，请稍后重试") from exc


def _orientation_action_id(action_id: str, quarter_turns_clockwise: int) -> str:
    """Validate the request before taking the batch lock; returns the action id."""
    action_id = str(action_id or "").strip()
    if not (8 <= len(action_id) <= 80) or not action_id.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(400, detail="action_id 格式无效")
    if quarter_turns_clockwise not in {0, 1, 2, 3}:
        raise HTTPException(400, detail="旋转角度必须是 0/90/180/270 度")
    return action_id


async def _confirm_orientation_locked(
    batch_id: str,
    paper_id: str,
    action_id: str,
    quarter_turns_clockwise: int,
    file: UploadFile | None,
    user: User,
    db: Session,
) -> dict:
    """The confirmation itself; the caller holds the batch operation lock."""
    batch = QuickBatch.load_meta(batch_id, UPLOAD_DIR)
    _require_owned_batch(batch, user)
    _require_orientation_editable(batch)
    upload = _batch_upload_or_404(batch, paper_id)

    history, existing = _orientation_action(upload, action_id, quarter_turns_clockwise)
    if existing is not None and existing.get("status") == "complete":
        return _orientation_state_payload(batch)
    if file is None:
        raise HTTPException(
            400,
            detail={
                "error": "CLIENT_ORIENTATION_UPLOAD_REQUIRED",
                "message": "请在浏览器完成旋转后重新提交方向文件",
            },
        )
    _require_orientation_change(batch, upload, paper_id)

    template_session, paper_cache = _orientation_template_evidence(batch, user.id)
    current_source_hash = await _verified_original_source_hash(batch, upload)
    _require_rotatable_page(upload)
    working_path, working_sha256 = await _save_client_orientation_upload(
        file,
        batch_dir=batch.batch_dir,
        paper_id=paper_id,
        action_id=action_id,
        source_sha256=current_source_hash,
        quarter_turns_clockwise=quarter_turns_clockwise,
    )

    event, confirmed_orientation, history = _begin_orientation_retry(
        batch,
        upload,
        history,
        existing,
        paper_id=paper_id,
        action_id=action_id,
        quarter_turns_clockwise=quarter_turns_clockwise,
        teacher_id=user.id,
        source_sha256=current_source_hash,
        working_path=working_path,
        working_sha256=working_sha256,
    )
    await _save_quick_batch_meta(batch)

    roster_names = _class_roster_names(db, batch, user.id)
    result = await _recognize_reoriented_upload(
        batch,
        upload,
        paper_cache,
        confirmed_orientation,
        teacher_id=user.id,
        paper_id=paper_id,
        action_id=action_id,
        roster_names=roster_names,
    )
    _finish_orientation_retry(
        batch, upload, event, history, result, template_session, db,
        teacher_id=user.id, paper_id=paper_id,
    )
    await _save_quick_batch_meta(batch)

    return _orientation_state_payload(batch)


def _require_orientation_editable(batch: QuickBatch) -> None:
    """Only a batch between recognition and grading may change a page direction."""
    if batch.review_results or batch.status in {"grading", "review_ready", "done"}:
        raise HTTPException(409, detail="本批次已进入批阅或审阅阶段，不能再修改试卷方向")
    if batch.status not in {
        "failed",
        "ocr_partial_failed",
        "ocr_done",
        "orientation_retrying",
    }:
        raise HTTPException(409, detail="请先等待当前识别或页型基准确认流程结束")


def _batch_upload_or_404(batch: QuickBatch, paper_id: str) -> dict:
    upload = next(
        (
            item for item in batch.uploads
            if str(item.get("paper_id") or "") == paper_id
        ),
        None,
    )
    if upload is None:
        raise HTTPException(404, detail="试卷不存在或已被清理")
    return upload


def _orientation_action(
    upload: dict,
    action_id: str,
    quarter_turns_clockwise: int,
) -> tuple[list[dict], dict | None]:
    """The page's direction history and this action's earlier event, if any.

    Replaying an action id with other turns is refused.
    """
    history = list(upload.get("orientation_history") or [])
    existing = next(
        (item for item in history if str(item.get("action_id") or "") == action_id),
        None,
    )
    if existing is not None:
        if int(existing.get("quarter_turns_clockwise") or 0) != quarter_turns_clockwise:
            raise HTTPException(409, detail="同一方向确认请求不能改用其他旋转角度")
    return history, existing


def _require_orientation_change(batch: QuickBatch, upload: dict, paper_id: str) -> None:
    """A cleared direction may be rotated again only while its OCR retry is unfinished."""
    job = next(
        (
            item for item in batch.ocr_jobs
            if str(item.get("paper_id") or "") == paper_id
        ),
        {},
    )
    if orientation_is_ready(upload) and not (
        upload.get("orientation_status") == "teacher_confirmed"
        and str(job.get("status") or "") in {"queued", "running", "failed"}
    ):
        raise HTTPException(409, detail="该试卷方向已经确认，无需重复旋转")


def _orientation_template_evidence(
    batch: QuickBatch,
    teacher_id: int,
) -> tuple[TemplateSession, PaperCache]:
    """The batch's frozen template and its visual evidence cache."""
    if batch.source_mode != "template" or not batch.template_id:
        raise HTTPException(409, detail="旧批次缺少模板快照标识，请重新上传本批试卷")
    template_session = _load_owned_template_session(batch.template_id, teacher_id)
    paper_cache = _build_template_paper_cache(template_session)
    return template_session, paper_cache


async def _verified_original_source_hash(batch: QuickBatch, upload: dict) -> str:
    """Hash of the preserved original; it must still be inside the batch and unchanged."""
    batch_root = os.path.realpath(batch.batch_dir)
    original_path = os.path.realpath(str(
        upload.get("original_image_path") or upload.get("image_path") or ""
    ))
    if (
        not original_path.startswith(batch_root + os.sep)
        or not os.path.isfile(original_path)
    ):
        raise HTTPException(404, detail="原始试卷已被清理，无法旋转")
    current_source_hash = await asyncio.to_thread(sha256_file, original_path)
    stored_source_hash = str(upload.get("source_sha256") or "")
    if stored_source_hash and stored_source_hash != current_source_hash:
        raise HTTPException(409, detail="原始试卷内容已变化，请重新上传")
    return current_source_hash


def _require_rotatable_page(upload: dict) -> None:
    if upload.get("orientation_reason") == "page_tilt_exceeds_limit":
        raise HTTPException(
            409,
            detail={
                "error": "ORIENTATION_STILL_INVALID",
                "message": "该页面存在连续倾斜，90 度旋转无法修正，请重新拍摄。",
            },
        )


def _begin_orientation_retry(
    batch: QuickBatch,
    upload: dict,
    history: list[dict],
    existing: dict | None,
    *,
    paper_id: str,
    action_id: str,
    quarter_turns_clockwise: int,
    teacher_id: int,
    source_sha256: str,
    working_path: str,
    working_sha256: str,
) -> tuple[dict, dict, list[dict]]:
    """Switch the page to the teacher-rotated file and reopen its OCR job.

    Returns ``(event, confirmed_orientation, history)``; the event stays
    ``ocr_running`` until the retry has finished.
    """
    working = {
        "image_path": working_path,
        "working_sha256": working_sha256,
        "manual_quarter_turns": quarter_turns_clockwise,
    }
    diagnostic = {
        **_client_ocr_ready_orientation(os.path.basename(working_path)),
        "orientation_coordinate_space": "client_teacher_rotated_pixels",
    }

    confirmed_at = datetime.now(timezone.utc).isoformat()
    event = existing or {
        "action_id": action_id,
        "quarter_turns_clockwise": quarter_turns_clockwise,
        "teacher_id": teacher_id,
        "created_at": confirmed_at,
    }
    event.update({
        "status": "ocr_running",
        "source_sha256": source_sha256,
        "working_sha256": working["working_sha256"],
        "detector": diagnostic,
    })
    history = [
        item for item in history
        if str(item.get("action_id") or "") != action_id
    ]
    history.append(event)

    old_path = str(upload.get("image_path") or "")
    confirmed_orientation = {
        **diagnostic,
        "orientation_status": "teacher_confirmed",
        "orientation_reason": "teacher_confirmed",
    }
    upload.update({
        **working,
        **confirmed_orientation,
        "orientation_confirmed_by": teacher_id,
        "orientation_confirmed_at": confirmed_at,
        "orientation_confirmation_id": action_id,
        "orientation_history": history[-20:],
    })
    batch.replace_ocr_job_path(old_path, upload)
    batch.mark_ocr_job(upload["image_path"], "running")
    batch.papers = [
        item for item in batch.papers
        if str(item.get("paper_id") or "") != paper_id
    ]
    # Followers must not keep pointing at the page being re-recognized.
    _refresh_identity_inheritance(batch)
    batch.confirmation_questions = []
    batch.status = "orientation_retrying"
    batch.error = None
    return event, confirmed_orientation, history


async def _recognize_reoriented_upload(
    batch: QuickBatch,
    upload: dict,
    paper_cache: PaperCache,
    confirmed_orientation: dict,
    *,
    teacher_id: int,
    paper_id: str,
    action_id: str,
    roster_names: list[str],
) -> dict:
    """OCR the rotated page under its own vision phase; a failure becomes a result."""
    vision_phase_token = set_vision_phase_key(
        f"orientation-retry:{paper_id}:{action_id}"
    )
    try:
        try:
            result = await _recognize_batch_upload(
                batch,
                upload["image_path"],
                paper_cache,
                teacher_id=str(teacher_id),
                paper_id=paper_id,
                roster_names=roster_names,
            )
            result["orientation"] = confirmed_orientation
        except Exception as exc:
            logger.error("方向确认后单卷 OCR 异常 %s: %s", upload.get("filename"), exc)
            result = ocr_failure_result(
                exc,
                message="方向已确认，但试卷识别失败",
            )
            result["orientation"] = confirmed_orientation
    finally:
        reset_vision_phase_key(vision_phase_token)
    return result


def _finish_orientation_retry(
    batch: QuickBatch,
    upload: dict,
    event: dict,
    history: list[dict],
    result: dict,
    template_session: TemplateSession,
    db: Session,
    *,
    teacher_id: int,
    paper_id: str,
) -> None:
    """Record the retried OCR in the ledger, settle the batch, complete the event.

    Unlike ``_apply_ocr_outcome`` an accelerator pause is ingested like any
    other result here; kept as it was.
    """
    _failed, can_retry = mark_ocr_outcome(
        batch, upload["image_path"], result,
    )
    failure_questions = _single_page_template_failure_questions(template_session)
    error_payload = _ingest_recognized_paper(
        batch,
        db,
        teacher_id,
        str(upload.get("filename") or "unknown"),
        upload["image_path"],
        result,
        failure_questions=(None if can_retry else failure_questions),
    )
    if (
        error_payload
        and error_payload.get("requires_manual_review")
        and orientation_is_ready(error_payload)
    ):
        batch.mark_ocr_job(
            upload["image_path"],
            "needs_review",
            str(error_payload.get("error") or ""),
        )

    upload_order = {
        str(item.get("paper_id") or ""): index
        for index, item in enumerate(batch.uploads)
    }
    batch.papers.sort(
        key=lambda item: upload_order.get(str(item.get("paper_id") or ""), len(upload_order))
    )
    _initialize_batch_answer_map(batch, teacher_id, template_session)
    summary = batch_ocr_summary(batch)
    all_orientations_ready = all(
        orientation_is_ready(item) for item in batch.uploads
    )
    if summary["can_grade"] and all_orientations_ready:
        batch.confirmation_questions = _confirmation_questions(batch)
        batch.status = "ocr_done"
    else:
        batch.status = "ocr_partial_failed"

    event["status"] = "complete"
    event["ocr_status"] = next(
        (
            item.get("status") for item in batch.ocr_jobs
            if str(item.get("paper_id") or "") == paper_id
        ),
        "failed",
    )
    event["completed_at"] = datetime.now(timezone.utc).isoformat()
    upload["orientation_history"] = history[-20:]


@router.get("/batch-grade/{batch_id}/paper/{paper_id}/image")
def batch_grade_paper_image(
    batch_id: str,
    paper_id: str,
    variant: Literal["active", "original", "review"] = Query("active"),
    user: User = Depends(_require_user),
):
    """Serve an owned uploaded paper during identity confirmation or result review."""
    batch = QuickBatch.load_meta(batch_id, UPLOAD_DIR)
    _require_owned_batch(batch, user)
    if variant == "review":
        output_path = _batch_review_image_path(batch, paper_id, user)
        return FileResponse(str(output_path), media_type="image/png", headers={"Cache-Control": "private, no-store"})
    _paper, display_path = _batch_paper_display_path(batch, paper_id, variant)
    return FileResponse(display_path, headers={"Cache-Control": "private, no-store"})
