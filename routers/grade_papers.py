"""The student papers of a batch: from one OCR result to a paper the teacher can review.

- ``_recognize_batch_upload`` runs OCR for one uploaded page against the frozen
  template (identity from its own crop, answers from the template prompt).
- ``_apply_ocr_outcome`` / ``_ingest_recognized_paper`` turn that result into a
  paper of the batch: identity (roster, file name clues, or anonymous labels),
  source and orientation metadata, its questions, or a fail-closed placeholder
  the teacher must review; ``_refresh_identity_inheritance`` lets blank pages
  inherit the identity of the page before them.
- ``_require_complete_batch_ocr`` / ``_require_orientation_ready``: every paper
  is recognized and upright before grading may start.
- ``_batch_paper_display_path`` / ``_batch_review_image_path``: the paper image
  to show, plain or with review marks, unless privacy retention removed it.

Moved unchanged from routers/grade.py, which still exposes these names.
"""

import copy
import logging
import os
import uuid
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy.orm import Session

from models import Student
from models_user import User
from routers.grade_common import _find_review_result, _load_owned_template_session
from routers.grade_uploads import _orientation_diagnostics
from services.collection_identity import resolve_collection_identity
from services.identity_inheritance import apply_identity_inheritance
from services.ocr_service import (
    recognize_paper_with_cache,
    recognize_student_info_with_roster,
)
from services.paper_cache import PaperCache
from services.paper_orientation import orientation_is_ready
from services.preprocess import get_display_image_path
from services.quick_grader import (
    QuickBatch,
    batch_ocr_summary,
    is_accelerator_pause_result,
    mark_ocr_outcome,
)
from services.review_marks import (
    align_template_review_details,
    detail_review_boxes,
    normalize_review_boxes,
    render_review_marks,
    review_cache_name,
)
from services.teaching_context import await_teaching

# routers.grade's logger, so these records keep their logger name.
logger = logging.getLogger("routers.grade")


# ── One recognized page into the batch ───────────────────────────────────
async def _recognize_batch_upload(
    batch: QuickBatch,
    image_path: str,
    paper_cache: PaperCache,
    *,
    teacher_id: str,
    paper_id: str,
    roster_names: list[str] | None = None,
) -> dict:
    """Recognize identity first, then run the frozen-template answer OCR.

    Identity is read from the dedicated top/left crop before any page-cache
    matching or column reuse.  The subsequent OCR request never receives the
    identity area, so splitting a page into one, two, or three columns cannot
    change which student owns the answers.
    """
    identity_data: dict = {}
    if batch.identity_mode != "anonymous":
        try:
            identity_data = await recognize_student_info_with_roster(
                image_path,
                list(roster_names or []),
                teacher_id=teacher_id,
            )
        except Exception as exc:
            # Identity uncertainty must remain visible to the teacher, but it
            # must not discard otherwise usable answer OCR.
            logger.warning("identity preflight failed for %s: %s", image_path, exc)
            identity_data = {
                "name": "",
                "student_no": "",
                "confidence": 0.0,
                "candidate_names": [],
                "match_status": "failed",
            }

    # Identity comes only from the dedicated crop above; the template OCR
    # prompt never asks for it, in identified and anonymous modes alike.
    result = await await_teaching(batch.teaching_context, recognize_paper_with_cache(
        image_path,
        batch.batch_id,
        paper_cache,
        teacher_id=teacher_id,
        paper_id=paper_id,
    ))
    if batch.identity_mode != "anonymous":
        result.update({
            "student_name": str(identity_data.get("name") or "").strip(),
            "student_no": str(identity_data.get("student_no") or "").strip(),
            "identity_ocr_source": "top_left_crop",
            "identity_ocr_confidence": float(identity_data.get("confidence") or 0.0),
            "identity_roster_candidates": list(
                identity_data.get("candidate_names") or []
            ),
            "identity_roster_match_status": str(
                identity_data.get("match_status") or ""
            ),
            # None (unknown, e.g. a failed identity OCR) never counts as blank.
            "identity_name_written": identity_data.get("name_written"),
        })
    return result


def _apply_ocr_outcome(
    batch: QuickBatch,
    db: Session,
    teacher_id: int,
    upload: dict,
    paper_data: dict,
    failure_questions: list[dict],
) -> dict | None:
    """Record one OCR outcome in the job ledger and the batch's papers.

    Returns the error payload of a page without a usable transcription, or
    None. A retryable failure stays without review placeholders so the next
    attempt can still succeed. The caller checkpoints the batch afterwards.
    """
    image_path = str(upload.get("image_path") or "")
    _failed, can_retry = mark_ocr_outcome(batch, image_path, paper_data)
    if is_accelerator_pause_result(paper_data):
        return None
    error_payload = _ingest_recognized_paper(
        batch,
        db,
        teacher_id,
        str(upload.get("filename") or "unknown"),
        image_path,
        paper_data,
        failure_questions=(None if can_retry else failure_questions),
    )
    if (
        error_payload
        and error_payload.get("requires_manual_review")
        and orientation_is_ready(error_payload)
    ):
        batch.mark_ocr_job(
            image_path,
            "needs_review",
            str(error_payload.get("error") or ""),
        )
    return error_payload


def _ingest_recognized_paper(
    batch: QuickBatch,
    db: Session,
    teacher_id: int,
    filename: str,
    image_path: str,
    paper_data: dict,
    failure_questions: list[dict] | None = None,
) -> dict | None:
    """Persist one OCR card into batch state without creating Student rows."""
    upload = next(
        (
            item for item in batch.uploads
            if os.path.realpath(str(item.get("image_path") or "")) == os.path.realpath(image_path)
        ),
        {},
    )
    paper_id = str(upload.get("paper_id") or uuid.uuid4().hex[:12])
    source_metadata = _paper_source_metadata(upload)
    anonymous_label, anonymous_identity = _anonymous_page_identity(
        batch, image_path,
    )
    is_anonymous = batch.identity_mode == "anonymous"
    # A page routed to a question_only/non_gradable template page has no
    # questions by contract; it is reported as not graded, not as a failure.
    if paper_data.get("error") or (
        not paper_data.get("questions") and not paper_data.get("_not_graded_page")
    ):
        error = str(paper_data.get("error") or "试卷内容无法识别")
        error_info = paper_data.get("error_info") or {}
        orientation = _stored_paper_orientation(upload, paper_data)
        # A routed failure (e.g. a zero-readout transcription) knows its page
        # type, so its own review placeholders also cover multi-page templates.
        routed_placeholders = paper_data.get("_fail_closed_questions")
        if failure_questions is not None and routed_placeholders:
            failure_questions = routed_placeholders
        # The QuickBatch owns the original image and persists this error below.
        # Copying it again into the legacy failed-import folder doubles disk use
        # during provider-wide outages without adding recoverability.
        if failure_questions:
            if is_anonymous:
                display_name, display_no, identity = anonymous_label, "", anonymous_identity
            else:
                display_name, display_no, identity = _resolve_identity_with_filename_clues(
                    db, teacher_id, batch.class_id, ocr_name="", ocr_student_no="",
                    filename=filename, source_relative_path=str(upload.get("source_relative_path") or ""),
                )
            batch.papers.append({
                "paper_id": paper_id,
                "filename": filename,
                "student_name": display_name,
                "student_no": display_no,
                "identity": identity,
                "identity_ocr_source": "disabled" if is_anonymous else "ocr_failure",
                "identity_ocr_confidence": 0.0,
                "identity_roster_candidates": [],
                "identity_roster_match_status": "",
                "image_path": image_path,
                **source_metadata,
                **orientation,
                "unexpected_page_type": False,
                "page_type_error": "",
                "ocr_failure": True,
                "ocr_error": error,
                "error_info": error_info,
                **({"ocr_receipt": paper_data["_ocr_receipt"]} if paper_data.get("_ocr_receipt") else {}),
                "requires_manual_review": True,
                "orientation_status": orientation.get("orientation_status"),
                "orientation_reason": orientation.get("orientation_reason"),
                "questions": [dict(question) for question in failure_questions],
            })
            _refresh_identity_inheritance(batch)
            return {
                "batch_id": batch.batch_id,
                "paper_id": paper_id,
                "filename": filename,
                "student_name": display_name,
                "student_no": display_no,
                "identity": identity,
                "questions_count": len(failure_questions),
                "matched_count": 0,
                "error": error,
                "error_info": error_info,
                "requires_manual_review": True,
                **_orientation_diagnostics(orientation),
            }
        return {
            "batch_id": batch.batch_id, "paper_id": paper_id,
            "filename": filename,
            "student_name": anonymous_label if is_anonymous else "",
            "student_no": "",
            "questions_count": 0, "matched_count": 0,
            "error": error,
            "error_info": error_info,
            "requires_manual_review": not orientation_is_ready(orientation),
            **_orientation_diagnostics(orientation),
        }

    ocr_questions = paper_data.get("questions", [])
    if is_anonymous:
        student_name = anonymous_label
        student_no = ""
        identity = anonymous_identity
    else:
        student_name, student_no, identity = _resolve_identity_with_filename_clues(
            db,
            teacher_id,
            batch.class_id,
            ocr_name=paper_data.get("student_name"),
            ocr_student_no=paper_data.get("student_no"),
            filename=filename,
            source_relative_path=str(upload.get("source_relative_path") or ""),
            roster_candidate_names=paper_data.get("identity_roster_candidates") or [],
            roster_match_status=str(paper_data.get("identity_roster_match_status") or ""),
        )
        identity_clue_source = str(identity.pop("_clue_source", ""))
    orientation = _stored_paper_orientation(upload, paper_data)
    batch.papers.append({
        "paper_id": paper_id,
        "filename": filename,
        "student_name": student_name,
        "student_no": student_no,
        "identity": identity,
        "identity_ocr_source": (
            "disabled"
            if is_anonymous
            else str(paper_data.get("identity_ocr_source") or "full_paper")
        ),
        "identity_ocr_confidence": (
            0.0
            if is_anonymous
            else float(paper_data.get("identity_ocr_confidence") or 0.0)
        ),
        "identity_roster_candidates": (
            []
            if is_anonymous
            else list(paper_data.get("identity_roster_candidates") or [])
        ),
        "identity_roster_match_status": (
            ""
            if is_anonymous
            else str(paper_data.get("identity_roster_match_status") or "")
        ),
        "identity_name_written": (
            None if is_anonymous else paper_data.get("identity_name_written")
        ),
        # 身份线索来源（ocr / filename / ocr+filename / conflict），供确认弹窗
        # 向老师说明"这个姓名学号是从哪来的"。
        "identity_clue_source": "" if is_anonymous else identity_clue_source,
        "image_path": image_path,
        **source_metadata,
        **orientation,
        "unexpected_page_type": bool(paper_data.get("_unexpected_page_type")),
        "page_type_error": str(paper_data.get("page_type_error") or ""),
        **({"page_not_graded": True, "page_role": str(paper_data.get("_page_role") or "")}
           if paper_data.get("_not_graded_page") else {}),
        "ocr_answers_only": bool(paper_data.get("_answers_only")),
        "ocr_page_type": str(paper_data.get("_cache_type_label") or ""),
        "ocr_local_template_evidence": bool(
            paper_data.get("_local_template_evidence")
        ),
        "ocr_full_page_transcription": bool(
            paper_data.get("_full_page_transcription")
        ),
        # Persist the OCR/score boundary with the paper, not merely in global
        # config, so stored results keep describing how they were produced.
        "ocr_transcription_only": bool(paper_data.get("_transcription_only")),
        "ocr_requires_separate_grading": bool(
            paper_data.get("_requires_separate_grading")
        ),
        **({"drawing_assessments": paper_data["_drawing_assessments"]}
           if paper_data.get("_drawing_assessments") else {}),
        # Hashes of the exact images sent and the response shape received.
        **({"ocr_receipt": paper_data["_ocr_receipt"]} if paper_data.get("_ocr_receipt") else {}),
        "questions": [
            {
                "question_no": str(q.get("question_number", "")),
                "student_answer": q.get("student_answer", "") or "",
                "answer_state": str(q.get("answer_state") or ""),
                "question_type": q.get("question_type") or "solve",
                "_ocr_local_recovery": bool(q.get("_ocr_local_recovery")),
                # Slot identity evidence must survive ingestion: grading
                # re-binds student_slots to the frozen slot_ids and
                # fail-closes contracted questions without it.
                **{key: q[key] for key in ("student_slots", "slot_identity_error") if key in q},
                **({"answer_bboxes": [list(box) for box in detail_review_boxes(q)]} if detail_review_boxes(q) else {}),
            }
            for q in ocr_questions
        ],
    })

    seen = {q["question_no"] for q in batch.questions}
    for question in ocr_questions:
        question_no = str(question.get("question_number", ""))
        if question_no and question_no not in seen:
            batch.questions.append({
                "question_no": question_no,
                "question_text": question.get("question_text", "") or "",
                "question_type": question.get("question_type", "solve") or "solve",
            })
            seen.add(question_no)
    _refresh_identity_inheritance(batch)
    return None


def _paper_source_metadata(upload: dict) -> dict:
    """Copy auditable source fields into internal paper state, never SSE paths."""
    fields = (
        "source_relative_path",
        "original_image_path",
        "source_sha256",
        "working_sha256",
        "manual_quarter_turns",
        "orientation_confirmed_by",
        "orientation_confirmed_at",
        "orientation_confirmation_id",
        "orientation_history",
    )
    return {field: upload.get(field) for field in fields if field in upload}


def _stored_paper_orientation(upload: dict, paper_data: dict) -> dict:
    """Return checkpointed orientation data without doing image I/O inline."""
    supplied = paper_data.get("orientation")
    if isinstance(supplied, dict):
        orientation = _orientation_diagnostics(supplied)
        if orientation.get("orientation_status"):
            return orientation
    orientation = _orientation_diagnostics(upload)
    if orientation.get("orientation_status"):
        return orientation
    return {
        "orientation_status": "needs_review",
        "orientation_reason": "orientation_metadata_missing",
        "orientation_confidence": 0.0,
        "detected_tilt_degrees": None,
        "exif_orientation": None,
        "orientation_coordinate_space": "exif_display_pixels",
    }


def _anonymous_page_identity(
    batch: QuickBatch,
    image_path: str,
) -> tuple[str, dict]:
    """Return a stable display label without persisting OCR identity data."""
    page_number = next(
        (
            index + 1
            for index, item in enumerate(batch.uploads)
            if os.path.realpath(str(item.get("image_path") or ""))
            == os.path.realpath(image_path)
        ),
        len(batch.papers) + 1,
    )
    return f"匿名页 {page_number}", {
        "status": "anonymous",
        "student_id": None,
        "reason": "anonymous_batch",
    }


def _class_roster_names(db: Session, batch: QuickBatch, teacher_id: int) -> list[str]:
    """Names on the batch's frozen class roster; anonymous batches have none."""
    if batch.identity_mode == "anonymous":
        return []
    students = db.query(Student).filter(
        Student.teacher_id == teacher_id,
        Student.class_id == batch.class_id,
    ).order_by(Student.id.asc()).all()
    return [student.name for student in students if student.name]


def _resolve_identity_with_filename_clues(
    db: Session,
    teacher_id: int,
    class_id: int | None,
    *,
    ocr_name: str | None,
    ocr_student_no: str | None,
    filename: str,
    source_relative_path: str = "",
    roster_candidate_names: list[str] | None = None,
    roster_match_status: str = "",
) -> tuple[str, str, dict]:
    """姓名先于学号；目录/文件名只在本批次班级内提供身份线索。"""
    students = db.query(Student).filter(
        Student.teacher_id == teacher_id,
        Student.class_id == class_id,
    ).order_by(Student.id.asc()).all() if class_id is not None else []
    identity = resolve_collection_identity(
        [{"student_id": student.id, "name": student.name,
          "student_no": student.student_no or "", "class_id": student.class_id}
         for student in students],
        ocr_name=ocr_name or "", ocr_student_no=ocr_student_no or "",
        filename=filename, source_relative_path=source_relative_path,
        roster_candidate_names=roster_candidate_names,
        roster_match_status=roster_match_status,
    )
    name = identity.get("resolved_name") or ocr_name or ""
    number = identity.get("resolved_student_no") or ""
    identity["_clue_source"] = identity["clue_source"]
    return name, number, identity


def _refresh_identity_inheritance(batch: QuickBatch) -> None:
    """Re-derive opt-in inherited identities after any change to the page set."""
    if batch.identity_mode == "anonymous" or not batch.identity_inheritance:
        return
    apply_identity_inheritance(
        batch.uploads,
        batch.papers,
        enabled=True,
        excluded_paper_ids=(
            str(job.get("paper_id") or "")
            for job in batch.ocr_jobs
            if job.get("status") == "excluded"
        ),
    )
# ── Ready to grade ───────────────────────────────────────────────────────
def _require_complete_batch_ocr(batch: QuickBatch) -> None:
    """Reject partial OCR batches before any grading or database mutation."""
    if batch.status == "failed":
        raise HTTPException(
            409,
            detail="本批次识别流程已失败，不能开始批阅；请重新识别",
        )
    if not batch.uploads:
        if batch.status not in {
            "awaiting_confirmation", "ocr_done", "grading", "grading_interrupted",
            "review_ready", "voided", "archived",
        }:
            raise HTTPException(
                409,
                detail=f"本批次当前状态 {batch.status or 'unknown'} 不允许开始批阅或回放；请完成识别或重新识别",
            )
        return
    summary = batch_ocr_summary(batch)
    if not summary["can_grade"]:
        missing = summary["failed_papers"] + summary["unresolved_papers"]
        raise HTTPException(
            409,
            detail=(
                f"本批次有 {missing} 张试卷尚未完成 OCR，不能开始部分批阅；"
                "请重新识别失败试卷"
            ),
        )
    if batch.status not in {
        "awaiting_confirmation", "ocr_done", "grading", "grading_interrupted",
        "review_ready", "voided", "archived",
    }:
        raise HTTPException(
            409,
            detail=f"本批次当前状态 {batch.status or 'unknown'} 不允许开始批阅或回放；请完成识别或重新识别",
        )


def _require_orientation_ready(batch: QuickBatch) -> None:
    """Never grade a paper whose page direction was not explicitly cleared."""
    blocked = [
        str(paper.get("filename") or paper.get("paper_id") or "unknown")
        for paper in batch.papers
        if not orientation_is_ready(paper)
    ]
    if blocked:
        raise HTTPException(
            409,
            detail={
                "error": "ORIENTATION_INVALID",
                "message": "存在方向异常或未确认方向的试卷，不能开始批阅；请旋转后重新上传或人工确认。",
                "files": blocked,
            },
        )
# ── Paper images ─────────────────────────────────────────────────────────
_RAW_PAPER_EXPIRED_MESSAGE = "原卷已按隐私保留策略清理，错题、得分和正确率记录仍保留"


def _batch_paper_display_path(batch: QuickBatch, paper_id: str, variant: str = "active"):
    """Resolve a paper only inside the already authorized batch."""
    if batch.asset_lifecycle.get("status") == "expired":
        raise HTTPException(410, detail=_RAW_PAPER_EXPIRED_MESSAGE)
    paper = next(
        (item for item in batch.papers if str(item.get("paper_id") or "") == paper_id),
        None,
    )
    if paper is None:
        paper = next(
            (item for item in batch.uploads if str(item.get("paper_id") or "") == paper_id),
            None,
        )
    if paper is None:
        raise HTTPException(404, detail="试卷不存在或已被清理")
    image_key = "original_image_path" if variant == "original" else "image_path"
    image_path = os.path.realpath(str(paper.get(image_key) or paper.get("image_path") or ""))
    batch_root = os.path.realpath(batch.batch_dir)
    if not image_path.startswith(batch_root + os.sep) or not os.path.isfile(image_path):
        raise HTTPException(404, detail="试卷不存在或已被清理")
    display_path = os.path.realpath(get_display_image_path(image_path))
    if not display_path.startswith(batch_root + os.sep) or not os.path.isfile(display_path):
        display_path = image_path
    return paper, display_path


def _batch_review_image_path(batch: QuickBatch, paper_id: str, user: User, decisions=None) -> Path:
    """Use one renderer for preview, single download and batch export."""
    paper, display_path = _batch_paper_display_path(batch, paper_id)
    review = _find_review_result(batch, paper_id)
    if review.get("disposition") == "voided":
        raise HTTPException(409, detail="该试卷已作废，不能导出批阅痕迹")
    details = copy.deepcopy([
        item for item in (review.get("result") or {}).get("details") or []
        if isinstance(item, dict)
    ])
    number_of = lambda item: str(item.get("question_no") or item.get("question_number") or "")
    by_number = {number_of(item): item for item in details}
    for decision in decisions or []:
        if decision.question_no not in by_number:
            raise HTTPException(400, detail="预览判定包含不属于本页的题号")
        detail = by_number[decision.question_no]
        previous_result = str(detail.get("result") or "needs_review")
        detail["result"] = decision.result
        # Folded fill details retain one verdict per physical answer box.  A
        # teacher decision targets the parent question, so a changed parent
        # verdict must update every nested mark used by the renderer as well.
        if previous_result != decision.result and isinstance(detail.get("grading_units"), list):
            for unit in detail["grading_units"]:
                if isinstance(unit, dict):
                    unit["result"] = decision.result

    # Older saved results can lack boxes even though OCR/template geometry exists.
    ocr_by_number = {number_of(item): item for item in paper.get("questions") or []}
    for detail in details:
        if detail_review_boxes(detail):
            continue
        number = number_of(detail)
        for source in (ocr_by_number.get(number) or {}, batch.answer_map.get(number) or {}):
            boxes = detail_review_boxes(source)
            if boxes:
                detail["answer_bboxes"] = boxes
                detail["answer_box_source"] = source.get("answer_box_source") or "student"
                break

    if batch.template_id and any(not detail_review_boxes(item) or item.get("answer_box_source") == "template" for item in details):
        template = _load_owned_template_session(batch.template_id, user.id)
        if template.teacher_id != user.id:
            raise HTTPException(403, detail="无权访问该模板")
        artifacts = template.page_artifacts or {}
        artifact = artifacts.get(str(paper.get("ocr_page_type") or ""))
        if not artifact:
            numbers = set(by_number)
            matches = [candidate for candidate in artifacts.values() if isinstance(candidate, dict)
                       and numbers.intersection(str(item.get("question_number") or item.get("question_no") or "")
                                                for item in candidate.get("question_schema") or [])]
            # Never use the first template page for every student's page.
            artifact = matches[0] if len(matches) == 1 else (next(iter(artifacts.values())) if len(artifacts) == 1 else None)
        if isinstance(artifact, dict):
            regions = {}
            for entry in artifact.get("template_answer_regions") or artifact.get("template_handwriting_regions") or []:
                if isinstance(entry, dict):
                    regions[number_of(entry)] = normalize_review_boxes(entry.get("regions"))
            for entry in artifact.get("question_schema") or []:
                if entry.get("drawing_regions"):
                    regions[number_of(entry)] = normalize_review_boxes(entry["drawing_regions"])
            for detail in details:
                if not detail_review_boxes(detail) and regions.get(number_of(detail)):
                    detail["answer_bboxes"] = regions[number_of(detail)]
                    detail["answer_box_source"] = "template"
            if any(item.get("answer_box_source") == "template" for item in details):
                template_root = os.path.realpath(template.template_dir)
                template_path = os.path.realpath(os.path.join(template_root, str(artifact.get("template_image") or "")))
                if not template_path.startswith(template_root + os.sep) or not os.path.isfile(template_path):
                    raise HTTPException(409, detail="模板定位底图已不可用，无法生成批阅痕迹")
                details = align_template_review_details(details, template_path, display_path)
        elif any(item.get("answer_box_source") == "template" for item in details):
            raise HTTPException(409, detail="无法确定本页对应的模板，无法生成批阅痕迹")

    output_dir = Path(batch.batch_dir) / "review_evidence"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / review_cache_name(display_path, details)
    if not output_path.is_file():
        temporary = output_dir / f".{uuid.uuid4().hex}.png"
        try:
            render_review_marks(display_path, details, temporary)
            os.replace(temporary, output_path)
        finally:
            temporary.unlink(missing_ok=True)
    return output_path
