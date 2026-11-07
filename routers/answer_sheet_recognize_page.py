"""Recognizing one student page: its identity, its answers and the evidence behind them.

- ``_recognize_one_student_page`` hands the page to
  ``_recognize_one_student_page_accelerated``, which builds the page's region
  plan, gets aligned evidence from an accelerator node and transcribes it
  (``AnswerSheetAcceleratorState`` while the node is not ready;
  ``_ANSWER_SHEET_ACCELERATOR_WAIT_SECONDS``).  The local code after that
  return is unreachable; it is kept as it was.
- ``_identity_row_for_transcription`` and ``_answer_row_for_transcription`` turn a
  transcription into the page's identity and answer rows;
  ``_required_answer_source_complete`` checks every required answer region
  arrived; ``_unique_student_for_code`` finds the one roster student with an
  exam or student number.
- ``_extract_student_page_regions`` crops identity and answer regions from an
  aligned page through the lazy image helpers (``load_rgb_image`` …
  ``read_choice_marks``), which keep image runtimes out of app start-up.
- ``_student_recognition_signature`` and ``_student_page_checkpoint_complete``
  let a finished page be skipped; ``_student_accelerator_state`` and
  ``_student_accelerator_message`` word a page that waits for the accelerator.

Moved unchanged from routers/answer_sheet.py, which still exposes these names.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from models import Student
from routers.answer_sheet_common import _run_answer_sheet_image_work
from routers.answer_sheet_student_files import (
    _ALIGNED_EVIDENCE_RE,
    _cleanup_failed_aligned_asset,
    _cleanup_student_recognition_attempt,
    _restore_student_page,
)
from routers.answer_sheet_student_state import _student_recognition_source_path
from services.accelerator_business import try_accelerated_evidence
from services.answer_sheet_acceleration import (
    bind_answer_sheet_evidence_locations,
    build_answer_sheet_acceleration_plan,
    transcribe_answer_sheet_evidence,
)
from services.answer_sheet_session import AnswerSheetSession
from services.paper_orientation import orientation_is_ready
from services.template_evidence import TemplateEvidenceError, TemplateEvidenceMatcher

# routers.answer_sheet's logger, so these records keep their logger name.
logger = logging.getLogger("routers.answer_sheet")


_ANSWER_SHEET_ACCELERATOR_WAIT_SECONDS = 60.0


def load_rgb_image(image_path: str):
    from services.answer_sheet_identity import load_rgb_image as implementation

    return implementation(image_path)


def crop_normalized_rgb(image_rgb, bbox: list[int]):
    from services.answer_sheet_identity import crop_normalized_rgb as implementation

    return implementation(image_rgb, bbox)


def decode_machine_code(crop_rgb, code_type: str) -> dict:
    from services.answer_sheet_identity import decode_machine_code as implementation

    return implementation(crop_rgb, code_type)


def read_exam_no_marks(crop_rgb, config: dict) -> dict:
    from services.answer_sheet_identity import read_exam_no_marks as implementation

    return implementation(crop_rgb, config)


def read_choice_marks(crop_rgb, options: list[str]) -> dict:
    from services.answer_sheet_identity import read_choice_marks as implementation

    return implementation(crop_rgb, options)


class AnswerSheetAcceleratorState(RuntimeError):
    """A student page is durable but remote image work cannot continue yet."""

    def __init__(self, state: str, reason_code: str = ""):
        self.state = str(state or "fault")
        self.reason_code = str(reason_code or "")
        super().__init__(self.reason_code or self.state)


def _unique_student_for_code(
    db: Session,
    session: AnswerSheetSession,
    value: str,
    *,
    rows: list[Student] | None = None,
):
    if not value:
        return None
    if rows is None:
        rows = db.query(Student).filter(
            Student.teacher_id == session.teacher_id,
            Student.class_id == session.class_id,
        ).all()
    matches = [row for row in rows if value in {str(row.exam_no or ""), str(row.student_no or "")}]
    return matches[0] if len(matches) == 1 else None


def _extract_student_page_regions(
    aligned_path: str,
    output_dir: str,
    paper_id: str,
    page_id: str,
    identity_regions: list[dict],
    answer_regions: list[dict],
    created_files: list[str] | None = None,
) -> tuple[list[dict], list[dict], list[str]]:
    aligned_rgb = load_rgb_image(aligned_path)
    identity_candidates: list[dict] = []
    answers: list[dict] = []
    review_reasons: list[str] = []
    evidence_root = Path(output_dir)
    evidence_root.mkdir(parents=True, exist_ok=True)
    written_paths: list[Path] = []
    crop_generation = uuid.uuid4().hex

    try:
        for region in identity_regions:
            if region["page_id"] != page_id:
                continue
            identity_type = region["identity_type"]
            crop = crop_normalized_rgb(aligned_rgb, region["bbox"])
            try:
                if identity_type in {"qr", "barcode"}:
                    result = decode_machine_code(crop, identity_type)
                elif identity_type == "exam_no_marks":
                    result = read_exam_no_marks(crop, region.get("config") or {})
                else:
                    result = {
                        "value": "",
                        "confidence": 0.0,
                        "status": "needs_review",
                        "reason": "teacher_confirmation_required",
                    }
                result["identity_type"] = identity_type
                identity_candidates.append(result)
            finally:
                del crop

        for region in answer_regions:
            if region["page_id"] != page_id:
                continue
            crop = crop_normalized_rgb(aligned_rgb, region["bbox"])
            crop_token = hashlib.sha256(
                (
                    f"{paper_id}\0{region['region_id']}\0{crop_generation}"
                ).encode("utf-8")
            ).hexdigest()[:24]
            crop_name = f"crop-{crop_token}.png"
            crop_path = evidence_root / crop_name
            temporary = evidence_root / f".{crop_name}.{uuid.uuid4().hex}.tmp"
            from PIL import Image

            crop_image = Image.fromarray(crop, mode="RGB")
            try:
                crop_image.save(temporary, format="PNG")
                os.replace(temporary, crop_path)
                written_paths.append(crop_path)
                if created_files is not None:
                    created_files.append(str(crop_path.resolve()))
                item = {
                    "region_id": region["region_id"],
                    "question_no": region["question_no"],
                    "region_type": region["region_type"],
                    "evidence_file": crop_name,
                    "answer": "",
                    "status": "needs_review",
                    "reason": "handwritten_transcription_required",
                }
                if region["region_type"] == "choice_mark":
                    item.update(
                        read_choice_marks(
                            crop,
                            region.get("choice_options")
                            or ["A", "B", "C", "D"],
                        )
                    )
                if item["status"] != "recognized":
                    review_reasons.append(
                        f"answer_{region['question_no']}_requires_review"
                    )
                answers.append(item)
            finally:
                crop_image.close()
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    logger.warning("failed to remove incomplete answer crop temp file")
                del crop
        return identity_candidates, answers, review_reasons
    except BaseException:
        for path in written_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("failed to remove incomplete answer crop")
        raise
    finally:
        del aligned_rgb


def _student_recognition_signature(session: AnswerSheetSession, page: dict) -> str:
    image_hash = str(
        page.get("orientation_working_sha256") or page.get("sha256") or ""
    )
    signature = f"{session.layout_hash}:{image_hash}"
    if session.teaching_context:
        signature += ":" + hashlib.sha256(json.dumps(
            session.teaching_context, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
    return signature


def _student_page_checkpoint_complete(
    session: AnswerSheetSession,
    page: dict,
) -> bool:
    checkpoint = page.get("recognition_checkpoint")
    return (
        isinstance(checkpoint, dict)
        and checkpoint.get("signature") == _student_recognition_signature(session, page)
        and checkpoint.get("state") in {"completed", "awaiting_orientation"}
    )


def _student_accelerator_state(state: str) -> str:
    return {
        "pending": "waiting_accelerator",
        "unavailable": "unavailable",
        "failed": "fault",
        "fault": "fault",
    }.get(str(state or ""), "fault")


def _student_accelerator_message(state: str) -> str:
    return {
        "waiting_accelerator": "等待教师客户端加速节点处理当前答题卡页，进度已保存。",
        "unavailable": "教师客户端或机会式加速节点当前不可用，答题卡页已保存。",
        "fault": "客户端图像处理失败，当前答题卡页已停止批改。",
    }.get(state, "客户端图像处理状态异常，当前答题卡页已停止批改。")


def _identity_row_for_transcription(
    item: dict,
    roster_rows: list[Student],
) -> tuple[dict, Student | None]:
    value = str(item.get("text") or "").strip()
    confidence = float(item.get("confidence") or 0.0)
    identity_type = str(item.get("identity_type") or "")
    candidate = {
        "identity_type": identity_type,
        "value": value,
        "confidence": confidence,
        "status": "needs_review",
        "reason": "teacher_confirmation_required",
    }
    if item.get("state") != "visible" or not value:
        candidate["reason"] = (
            "identity_unreadable"
            if item.get("state") == "unreadable"
            else "identity_blank"
        )
        return candidate, None

    if identity_type == "name":
        matches = [row for row in roster_rows if str(row.name or "").strip() == value]
    elif identity_type in {"student_no", "exam_no_text"}:
        matches = [
            row for row in roster_rows
            if value in {str(row.student_no or ""), str(row.exam_no or "")}
        ]
    else:
        # QR, barcode and mark-grid text is still shown to the teacher, but a
        # generative transcription is not treated as a machine-code decoder.
        matches = []
    matched = matches[0] if len(matches) == 1 else None
    if matched is not None and confidence >= 0.90:
        candidate.update({
            "status": "recognized",
            "reason": "unique_high_confidence_roster_match",
            "student_id": matched.id,
        })
        return candidate, matched
    candidate["reason"] = (
        "identity_low_confidence"
        if matched is not None
        else "identity_requires_teacher_confirmation"
    )
    return candidate, None


def _answer_row_for_transcription(item: dict) -> tuple[dict, str | None]:
    from services.grading_contract import normalize_choice_answer

    state = str(item.get("state") or "")
    value = str(item.get("text") or "").strip()
    result = {
        "region_id": item.get("region_id"),
        "question_no": str(item.get("question_no") or ""),
        "region_type": item.get("region_type"),
        "answer": value,
        "status": "needs_review",
        "reason": "handwritten_transcription_requires_review",
        "vision_confidence": float(item.get("confidence") or 0.0),
    }
    if state != "visible" or not value:
        result["answer"] = "" if state == "blank" else "[无法识别]"
        result["reason"] = (
            "blank_requires_teacher_confirmation"
            if state == "blank"
            else "unreadable_requires_teacher_confirmation"
        )
        return result, f"answer_{result['question_no']}_requires_review"

    if str(item.get("question_type") or "") == "choice":
        normalized = normalize_choice_answer(value)
        options = set(item.get("choice_options") or ["A", "B", "C", "D"])
        mode = str(item.get("choice_mode") or "single")
        valid = bool(
            normalized
            and set(normalized).issubset(options)
            and (
                (mode == "single" and len(normalized) == 1)
                or (mode == "multiple" and 1 <= len(normalized) <= len(options))
            )
        )
        if not valid:
            result.update(
                answer="[无法识别]",
                reason="choice_transcription_invalid",
            )
            return result, f"answer_{result['question_no']}_requires_review"
        result["answer"] = normalized

    result.update(status="recognized", reason="vision_evidence_transcribed")
    return result, None


def _required_answer_source_complete(
    diagnostics: dict,
    region_plan: list[dict],
) -> bool:
    incomplete_raw = diagnostics.get("incomplete_answer_regions")
    if not isinstance(incomplete_raw, list):
        return bool(diagnostics.get("answer_source_complete", False))

    known_regions: set[str] = set()
    required_regions: set[str] = set()
    for item in region_plan:
        question_number = str(item.get("question_number") or "").strip()
        kind = str(item.get("kind") or "")
        if not question_number or kind not in {"identity", "answer"}:
            return False
        known_regions.add(question_number)
        if kind == "answer":
            required_regions.add(question_number)

    incomplete_regions = {
        str(value).strip()
        for value in incomplete_raw
        if str(value).strip()
    }
    if not incomplete_regions.issubset(known_regions):
        return False
    return incomplete_regions.isdisjoint(required_regions)


async def _recognize_one_student_page_accelerated(
    session: AnswerSheetSession,
    page: dict,
    roster_rows: list[Student],
) -> dict | None:
    signature = _student_recognition_signature(session, page)
    if not orientation_is_ready(page):
        page["review_reasons"] = ["orientation_requires_teacher_confirmation"]
        page["recognition_checkpoint"] = {
            "signature": signature,
            "state": "awaiting_orientation",
        }
        return None

    try:
        accelerated_artifacts, region_plans = build_answer_sheet_acceleration_plan(
            session.layout_artifacts,
            session.identity_regions,
            session.answer_regions,
        )
    except (TypeError, ValueError) as exc:
        page["recognition_checkpoint"] = {
            "signature": signature,
            "state": "fault",
            "reason_code": "answer_sheet_layout_contract_invalid",
        }
        page["review_reasons"] = ["fault"]
        raise AnswerSheetAcceleratorState(
            "fault",
            "answer_sheet_layout_contract_invalid",
        ) from exc
    handle = await try_accelerated_evidence(
        teacher_id=str(session.teacher_id or ""),
        batch_id=session.session_id,
        paper_id=str(page.get("paper_id") or ""),
        source_path=_student_recognition_source_path(page),
        template_id=session.session_id,
        artifacts=accelerated_artifacts,
        template_dir=session.session_dir,
        wait_seconds=_ANSWER_SHEET_ACCELERATOR_WAIT_SECONDS,
    )
    if handle.state != "ready" or handle.evidence is None:
        state = _student_accelerator_state(handle.state)
        page["recognition_checkpoint"] = {
            "signature": signature,
            "state": state,
            "job_ids": list(handle.job_ids),
            "reason_code": handle.reason_code,
        }
        page["review_reasons"] = [state]
        raise AnswerSheetAcceleratorState(state, handle.reason_code)

    evidence = handle.evidence
    plan = region_plans.get(evidence.page_type_id)
    if not plan or [item["question_number"] for item in plan] != evidence.question_numbers:
        page["recognition_checkpoint"] = {
            "signature": signature,
            "state": "fault",
            "job_ids": list(handle.job_ids),
            "reason_code": "accelerator_region_contract_mismatch",
        }
        page["review_reasons"] = ["fault"]
        raise AnswerSheetAcceleratorState(
            "fault",
            "accelerator_region_contract_mismatch",
        )

    evidence_paths = tuple(getattr(evidence, "evidence_paths", ()) or ())
    if not evidence_paths:
        evidence_paths = (evidence.evidence_path,)
    try:
        plan = bind_answer_sheet_evidence_locations(
            plan,
            dict(evidence.diagnostics or {}),
            len(evidence_paths),
        )
    except (TypeError, ValueError) as exc:
        page["recognition_checkpoint"] = {
            "signature": signature,
            "state": "fault",
            "job_ids": list(handle.job_ids),
            "reason_code": "accelerator_evidence_location_mismatch",
        }
        page["review_reasons"] = ["fault"]
        raise AnswerSheetAcceleratorState(
            "fault",
            "accelerator_evidence_location_mismatch",
        ) from exc
    transcriptions = await transcribe_answer_sheet_evidence(
        evidence_paths,
        plan,
        teacher_id=session.teacher_id or "",
    )
    identity_candidates: list[dict] = []
    matched_students: list[Student] = []
    answers: list[dict] = []
    review_reasons: list[str] = []
    for item in transcriptions:
        if item.get("kind") == "identity":
            candidate, matched = _identity_row_for_transcription(item, roster_rows)
            identity_candidates.append(candidate)
            if matched is not None:
                matched_students.append(matched)
            continue
        answer, review_reason = _answer_row_for_transcription(item)
        answers.append(answer)
        if review_reason:
            review_reasons.append(review_reason)

    matched_ids = {student.id for student in matched_students}
    if len(matched_ids) == 1:
        matched = matched_students[0]
        identity = {
            "status": "recognized",
            "student_id": matched.id,
            "student_name": matched.name,
            "candidates": identity_candidates,
        }
    else:
        identity = {
            "status": "needs_review",
            "student_id": None,
            "candidates": identity_candidates,
        }
        review_reasons.append("identity_requires_teacher_confirmation")

    diagnostics = dict(evidence.diagnostics or {})
    answer_source_complete = _required_answer_source_complete(
        diagnostics,
        plan,
    )
    page["page_id"] = evidence.page_type_id
    page["registration"] = {
        "status": "passed" if answer_source_complete else "needs_review",
        "diagnostics": {
            **diagnostics,
            "answer_source_complete": answer_source_complete,
        },
    }
    page["identity"] = identity
    page["answers"] = answers
    page["review_reasons"] = list(dict.fromkeys(review_reasons))
    page["recognition_checkpoint"] = {
        "signature": signature,
        "state": "completed",
    }
    return {"accelerator_handle": handle}


async def _recognize_one_student_page(
    session: AnswerSheetSession,
    page: dict,
    matcher: TemplateEvidenceMatcher,
    output_dir: str,
    db: Session,
    roster_rows: list[Student],
) -> dict | None:
    return await _recognize_one_student_page_accelerated(
        session,
        page,
        roster_rows,
    )

    previous_page = copy.deepcopy(page)
    review_reasons: list[str] = []
    signature = _student_recognition_signature(session, page)
    if not orientation_is_ready(page):
        page["review_reasons"] = ["orientation_requires_teacher_confirmation"]
        page["recognition_checkpoint"] = {
            "signature": signature,
            "state": "awaiting_orientation",
        }
        return None

    active_path = _student_recognition_source_path(page)
    evidence_root = Path(output_dir).resolve()
    attempt_root = (
        evidence_root / f".student-recognition-{uuid.uuid4().hex}"
    )
    attempt_root.mkdir(parents=True, exist_ok=False)
    match_handed_off = False

    def mark_match_handoff() -> None:
        nonlocal match_handed_off
        match_handed_off = True

    def cleanup_attempt() -> None:
        _cleanup_student_recognition_attempt(evidence_root, attempt_root)

    try:
        evidence = await _run_answer_sheet_image_work(
            matcher.match_and_build,
            active_path,
            str(attempt_root),
            include_evidence=False,
            on_handoff=mark_match_handoff,
            settle_callbacks=(cleanup_attempt,),
        )
    except TemplateEvidenceError as exc:
        cleanup_attempt()
        page["review_reasons"] = ["page_registration_failed"]
        page["registration"] = {
            "status": "needs_review",
            "diagnostics": exc.diagnostics,
        }
        page.pop("recognition_checkpoint", None)
        return None
    except BaseException:
        if not match_handed_off:
            cleanup_attempt()
        _restore_student_page(page, previous_page)
        raise

    diagnostics = dict(evidence.diagnostics)
    try:
        staged_aligned = Path(
            str(diagnostics.pop("aligned_image_path"))
        ).resolve(strict=False)
        if (
            staged_aligned.parent != attempt_root.resolve()
            or not _ALIGNED_EVIDENCE_RE.fullmatch(staged_aligned.name)
            or not staged_aligned.is_file()
        ):
            raise RuntimeError("student recognition produced an invalid aligned asset")
        final_aligned = evidence_root / staged_aligned.name
        os.replace(staged_aligned, final_aligned)
        aligned_path = str(final_aligned.resolve())
    except BaseException:
        cleanup_attempt()
        _restore_student_page(page, previous_page)
        raise
    cleanup_attempt()

    page_id = evidence.page_type_id
    registration = {"status": "passed", "diagnostics": diagnostics}
    if not diagnostics.get("answer_source_complete", False):
        registration["status"] = "needs_review"
        page["working_path"] = aligned_path
        page["page_id"] = page_id
        page["registration"] = registration
        page["review_reasons"] = ["answer_source_pixels_incomplete"]
        page["identity"] = {
            "status": "needs_review",
            "student_id": None,
            "candidates": [],
        }
        page["answers"] = []
        page["recognition_checkpoint"] = {
            "signature": signature,
            "state": "completed",
        }
        return {
            "page": page,
            "previous_page": previous_page,
            "aligned_path": aligned_path,
            "crop_paths": [],
            "answers": [],
        }

    created_crop_paths: list[str] = []
    extraction_handed_off = False

    def mark_extraction_handoff() -> None:
        nonlocal extraction_handed_off
        extraction_handed_off = True

    def cleanup_uncommitted_assets() -> None:
        _cleanup_failed_aligned_asset(
            session,
            aligned_path,
            crop_paths=created_crop_paths,
        )

    try:
        identity_candidates, answers, answer_review_reasons = (
            await _run_answer_sheet_image_work(
                _extract_student_page_regions,
                aligned_path,
                output_dir,
                str(page["paper_id"]),
                page_id,
                session.identity_regions,
                session.answer_regions,
                created_crop_paths,
                on_handoff=mark_extraction_handoff,
                settle_callbacks=(cleanup_uncommitted_assets,),
            )
        )
        values = {
            item["value"]
            for item in identity_candidates
            if item.get("status") == "recognized" and item.get("value")
        }
        matched = (
            _unique_student_for_code(
                db,
                session,
                next(iter(values)),
                rows=roster_rows,
            )
            if len(values) == 1
            else None
        )
        if matched:
            identity = {
                "status": "recognized",
                "student_id": matched.id,
                "student_name": matched.name,
                "candidates": identity_candidates,
            }
        else:
            identity = {
                "status": "needs_review",
                "student_id": None,
                "candidates": identity_candidates,
            }
            review_reasons.append("identity_requires_teacher_confirmation")
    except BaseException:
        if not extraction_handed_off:
            cleanup_uncommitted_assets()
        _restore_student_page(page, previous_page)
        raise

    page["working_path"] = aligned_path
    page["page_id"] = page_id
    page["registration"] = registration
    page["identity"] = identity
    page["review_reasons"] = [*review_reasons, *answer_review_reasons]
    page["answers"] = answers
    page["recognition_checkpoint"] = {
        "signature": signature,
        "state": "completed",
    }
    return {
        "page": page,
        "previous_page": previous_page,
        "aligned_path": aligned_path,
        "crop_paths": created_crop_paths,
        "answers": answers,
    }
