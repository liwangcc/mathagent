"""Recognition of one registered student page of a template-code batch.

Input is the page's server-registered image (``answer_sheet_page_registration``)
in the frozen answer-sheet template frame; nothing else is read.

* ``choice_mark`` answers come from the local pixel recognizer
  (``recognize_bubble_template_page``): 对 / 错 / 空答 / 多涂 / needs_review
  per grid row, whole question or small question.
* Every other answer comes from the column transcription contract
  (``transcribe_page_columns``): one vision call per page with its N masked
  column images, then the blind verification stage.

The results are mapped to the page's answer rows for the unchanged teacher
review and grading chain.  Only decided, verified readings are
``recognized``; everything uncertain -- a needs_review, blank or multiply
marked bubble, a blank / unreadable / disputed / unverified transcription,
any failed stage -- stays ``needs_review`` for the teacher.  Nothing is
attributed silently and no answer is invented: a failed stage produces empty
answers with a reason, never defaults.  Transcription and grading stay
separate; grading later reads only the confirmed answer text.
"""

from __future__ import annotations

import copy
from typing import Any, Awaitable, Callable, Mapping, Sequence

from services.answer_sheet_bubble_recognizer import (
    OUTCOME_BLANK,
    OUTCOME_CORRECT,
    OUTCOME_MULTIPLE,
    OUTCOME_NEEDS_REVIEW,
    OUTCOME_WRONG,
)
from services.answer_sheet_column_transcription import (
    RECORD_FAILED,
    SOURCE_BUBBLE,
    VERIFY_CONFIRMED,
    page_transcription_gate,
)

RECOGNITION_SOURCE = "server_registered_image"
BUBBLE_FAILED = "failed"
_BUBBLE_SUMMARY_KEYS = (
    "outcome", "label", "reason", "needs_review", "answer_options", "marked_options",
    "uncertain_options", "missing_options", "extra_options",
)


def vision_images_call(teacher_id: object) -> Callable[..., Awaitable[str]]:
    """The student-OCR route for one teacher (same route and limits as the
    existing answer-sheet transcription), as ``call_images(paths, prompt, max_tokens=)``."""
    owner = str(teacher_id or "")

    async def call_images(paths: Sequence[str], prompt: str, *, max_tokens: int) -> str:
        from config import OCR_PER_TEACHER_LIMIT
        from services.key_pool import get_ocr_teacher_semaphore
        from services.ocr_service import call_vision_images, reset_vision_teacher_id, set_vision_teacher_id

        token = set_vision_teacher_id(owner)
        try:
            semaphore = await get_ocr_teacher_semaphore(owner, OCR_PER_TEACHER_LIMIT)
            async with semaphore:
                return await call_vision_images(list(paths), prompt, max_tokens=max_tokens)
        finally:
            reset_vision_teacher_id(token)

    return call_images


def _bubble_answer(bubbles: Mapping[str, Any], key: str) -> dict[str, Any]:
    if bubbles.get("status") == BUBBLE_FAILED:
        return {"answer": "", "status": "needs_review", "reason": "bubble_recognition_failed"}
    result = (bubbles.get("questions") or {}).get(key)
    if not isinstance(result, Mapping):
        return {"answer": "", "status": "needs_review", "reason": "bubble_result_missing"}
    summary = {name: copy.deepcopy(result.get(name)) for name in _BUBBLE_SUMMARY_KEYS}
    outcome = result.get("outcome")
    marked = "".join(result.get("marked_options") or [])
    if outcome in {OUTCOME_CORRECT, OUTCOME_WRONG} and marked and not result.get("needs_review"):
        # The filled cells of teacher-confirmed option geometry; grading
        # compares them with the answer again, it never trusts this label.
        item = {"answer": marked, "status": "recognized", "reason": f"bubble_{outcome}"}
    elif outcome == OUTCOME_BLANK:
        item = {"answer": "", "status": "needs_review", "reason": "bubble_blank_requires_teacher_confirmation"}
    elif outcome == OUTCOME_MULTIPLE:
        item = {"answer": marked, "status": "needs_review", "reason": "bubble_multiple_requires_teacher_confirmation"}
    else:
        item = {"answer": "", "status": "needs_review", "reason": (
            "bubble_needs_review" if outcome == OUTCOME_NEEDS_REVIEW else "bubble_result_invalid"
        )}
    return {**item, "bubble": summary}


def _transcribed_answer(field: Mapping[str, Any] | None, *, blocked: str) -> dict[str, Any]:
    if blocked:
        return {"answer": "", "status": "needs_review", "reason": blocked}
    if not isinstance(field, Mapping):
        return {"answer": "", "status": "needs_review", "reason": "transcription_field_missing"}
    verification = field.get("verification") if isinstance(field.get("verification"), Mapping) else {}
    summary = {
        "state": field.get("state"),
        "confidence": field.get("confidence"),
        "review_reasons": list(field.get("review_reasons") or []),
        "high_risk_reasons": list(field.get("high_risk_reasons") or []),
        "verification": copy.deepcopy(dict(verification)),
        "entry_ids": list(field.get("entry_ids") or []),
        "columns": list(field.get("columns") or []),
    }
    text = str(field.get("student_answer") or "")
    if summary["review_reasons"]:
        return {"answer": text, "status": "needs_review", "reason": summary["review_reasons"][0],
                "transcription": summary}
    if field.get("answer_kind") == "choice_handwritten":
        text = str(field.get("choice_letters") or "")
    if not text.strip():
        return {"answer": "", "status": "needs_review", "reason": "transcription_empty", "transcription": summary}
    reason = (
        "column_transcription_verified"
        if verification.get("status") == VERIFY_CONFIRMED
        else "column_transcription"
    )
    return {"answer": text, "status": "recognized", "reason": reason, "transcription": summary}


def batch_page_answers(
    plan: Mapping[str, Any],
    transcription: Mapping[str, Any],
    bubbles: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Answer rows (one per question number + small question) and page review reasons."""
    gate = page_transcription_gate(transcription)
    blocked = ""
    if transcription.get("status") == RECORD_FAILED:
        blocked = "transcription_failed"
    elif not gate["ready_for_grading"]:
        blocked = "transcription_incomplete"
    by_key = {
        field.get("answer_key"): field
        for field in transcription.get("fields") or []
        if isinstance(field, Mapping)
    }
    answers: list[dict[str, Any]] = []
    reasons: list[str] = []
    if blocked:
        reasons.append(blocked)
    if bubbles.get("status") == BUBBLE_FAILED:
        reasons.append("bubble_recognition_failed")
    for planned in plan["fields"]:
        key = planned["answer_key"]
        item = {
            "region_id": planned["region_ids"][0],
            "region_ids": list(planned["region_ids"]),
            "question_no": planned["question_no"],
            "subquestion_no": planned["subquestion_no"],
            "answer_key": key,
            "region_type": planned["answer_kind"],
            "source": planned["source"],
        }
        if planned["source"] == SOURCE_BUBBLE:
            item.update(_bubble_answer(bubbles, key))
        else:
            item.update(_transcribed_answer(by_key.get(key), blocked=blocked))
        if item["status"] != "recognized":
            reasons.append(f"answer_{key}_requires_review")
        answers.append(item)
    return answers, list(dict.fromkeys(reasons))
