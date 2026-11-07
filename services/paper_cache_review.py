"""Results that fail closed for teacher review, and the receipt of what was sent and received.

- ``_fail_closed_ocr_result`` replaces unsafe OCR output with an empty failed
  result that keeps the retry metadata and a diagnostic id
  (``_rejection_error_info``); ``_transcription_contract_failure`` rejects a
  response that skipped or reordered contracted questions but keeps the page's
  recovery attempt.
- ``_transcription_receipt`` (``_response_shape``) records hashes, sizes and the
  per-question structure of a request and its response, never their text.
- ``TranscriptionZeroReadout`` (``_ZERO_READOUT``, ``_SLOT_CONTRACT_VIOLATED``)
  is an accepted transcription that read no answer on the page.

``_PaperCacheReviewMixin`` holds these ``PaperCache`` methods:
``_zero_readout_failure`` turns such a page into a persisted failure with review
placeholders (``_review_placeholders``, ``_with_review_placeholders``), and
``_not_graded_page_result`` reports a page routed to a question-only or
non-gradable template page.

Moved unchanged from services/paper_cache.py, which still exposes these names.
"""

import hashlib
import logging
import re

from services.paper_cache_base import _normalize_question_number

# services.paper_cache's logger, so these records keep their logger name.
logger = logging.getLogger("services.paper_cache")


_DIAGNOSTIC_ID = re.compile(r"[a-f0-9]{32}")
_RECEIPT_KEY = re.compile(r"[A-Za-z0-9_]{1,40}")
_RECEIPT_QUESTION_NUMBER = re.compile(r"[0-9A-Za-z.\[\]()（）-]{1,32}")
_ANSWER_STATES = ("visible", "blank", "unreadable")
_SLOT_CONTRACT_VIOLATED = "transcription_slot_contract_violated"
_ZERO_READOUT = "transcription_zero_readout"
_ZERO_READOUT_MESSAGES = {
    _SLOT_CONTRACT_VIOLATED: "答案转录未按作答位合同返回任何可用作答，已停止自动批改该页，请老师对照原卷复核",
    _ZERO_READOUT: "答案转录未读出本页任何作答（均无法识别），已停止自动批改该页，请老师对照原卷复核",
}


class TranscriptionZeroReadout(ValueError):
    """A structurally accepted transcription that read no answer on the page.

    Per-question fail-closed binding keeps answer ownership safe, but a page
    whose text answers are all unreadable/blank with at least one unreadable,
    or whose response ignored the frozen slot contract for every contracted
    question, is a failed recognition, never an all-``[无法识别]`` success.
    """

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        self.code = (
            "OUTPUT_PARSE_FAILED"
            if reason_code == _SLOT_CONTRACT_VIOLATED
            else "TRANSCRIPTION_EMPTY"
        )
        super().__init__(reason_code)


def _rejection_error_info(source: dict | None) -> dict:
    """Reuse the rejected route's diagnostic; mint one only when it had none."""
    source = source if isinstance(source, dict) else {}
    info = source.get("error_info")
    if isinstance(info, dict) and all(
        _DIAGNOSTIC_ID.fullmatch(str(info.get(key) or ""))
        for key in ("error_id", "correlation_id")
    ):
        return dict(info)
    from services.processing_errors import ProcessingError, report_failure

    return report_failure(
        ProcessingError(
            "PROCESSING_FAILED",
            stage="image" if source.get("_accelerator_state") else "transcription",
        ),
        reason_code=str(
            source.get("_accelerator_reason_code")
            or source.get("_ocr_error_code")
            or ""
        ),
    )


def _fail_closed_ocr_result(message: str, source: dict | None = None) -> dict:
    """Keep retry metadata and the diagnostic id while replacing unsafe OCR output."""
    result = {
        "questions": [],
        "student_name": "",
        "student_no": "",
        "error": message,
    }
    if isinstance(source, dict):
        for key in (
            "_ocr_retryable",
            "_ocr_error_code",
            "_accelerator_state",
            "_accelerator_reason_code",
            "_ocr_receipt",
            "_fail_closed_questions",
        ):
            if key in source:
                result[key] = source[key]
    # Pending/unavailable accelerators are scheduling pauses, not failures.
    # Every other rejection is persisted as failed and must stay traceable.
    if str(result.get("_accelerator_state") or "") not in {"pending", "unavailable"}:
        result["error_info"] = _rejection_error_info(source)
    return result


def _transcription_contract_failure(message: str, receipt: dict | None) -> dict:
    """A response that skipped or reordered contracted questions.

    The page is still rejected as a whole, but the cause is the model's output
    for this one request, so the page keeps its durable recovery attempt.
    """
    result = {
        "error": message,
        "_ocr_retryable": True,
        "_ocr_error_code": "transcription_contract_mismatch",
    }
    if receipt:
        result["_ocr_receipt"] = receipt
    return result


def _transcription_receipt(
    image_paths: list[str],
    raw_content: object,
    data: object,
    request_receipt: dict | None = None,
) -> dict:
    """Trace the exact image bytes sent and the response shape received.

    Only hashes, sizes and per-question structure are kept: neither the
    derived student images nor the response text (student answers) are
    copied, so the receipt can live beside the paper under retention.
    """
    from PIL import Image

    images = []
    for path in image_paths:
        entry: dict = {"sha256": "", "bytes": 0, "width": 0, "height": 0}
        try:
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
                    entry["bytes"] += len(chunk)
            entry["sha256"] = digest.hexdigest()
            with Image.open(path) as image:
                entry["width"], entry["height"] = image.size
        except Exception:  # noqa: BLE001 - a receipt must never change the outcome
            entry["unavailable"] = True
        images.append(entry)
    receipt: dict = {"version": 1, "images": images}
    if request_receipt:
        receipt["transport"] = {
            key: request_receipt[key]
            for key in ("phase", "route", "requested_model", "response_model",
                        "finish_reason", "attempts", "enable_thinking", "reasoning_effort", "elapsed_seconds")
            if key in request_receipt
        }
    try:
        receipt.update(_response_shape(raw_content, data))
    except Exception:  # noqa: BLE001 - a receipt must never change the outcome
        receipt["response_unavailable"] = True
    return receipt


def _response_shape(raw_content: object, data: object) -> dict:
    """Hash of the raw response plus its per-question structure, no text."""
    receipt: dict = {}
    if isinstance(raw_content, str):
        receipt["response_sha256"] = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
        receipt["response_chars"] = len(raw_content)
    if isinstance(data, dict):
        receipt["response_keys"] = sorted(
            key for key in data if isinstance(key, str) and _RECEIPT_KEY.fullmatch(key)
        )[:16]
        questions = data.get("questions")
        if isinstance(questions, list):
            shapes = []
            for item in questions[:200]:
                if not isinstance(item, dict):
                    shapes.append({"type": type(item).__name__})
                    continue
                slots = item.get("student_slots")
                number = str(item.get("question_number") or "")
                shape = {
                    "question_number": number if _RECEIPT_QUESTION_NUMBER.fullmatch(number) else "other",
                    "keys": sorted(
                        key for key in item
                        if isinstance(key, str) and _RECEIPT_KEY.fullmatch(key)
                    )[:16],
                    "student_slots": (
                        f"list:{len(slots)}" if isinstance(slots, list)
                        else "absent" if "student_slots" not in item
                        else type(slots).__name__
                    ),
                }
                state = str(item.get("answer_state") or "").strip().lower()
                if state:
                    shape["answer_state"] = state if state in _ANSWER_STATES else "other"
                if isinstance(slots, list):
                    counts: dict[str, int] = {}
                    for slot in slots:
                        slot_state = (
                            str(slot.get("answer_state") or "").strip().lower()
                            if isinstance(slot, dict) else ""
                        )
                        slot_state = slot_state if slot_state in _ANSWER_STATES else "other"
                        counts[slot_state] = counts.get(slot_state, 0) + 1
                    shape["slot_states"] = counts
                shapes.append(shape)
            receipt["questions"] = shapes
    return receipt


class _PaperCacheReviewMixin:
    """PaperCache's methods for results that send a page to teacher review."""

    def _not_graded_page_result(self, label: str) -> dict:
        """Report a student page routed to a question_only/non_gradable page."""
        role = self._non_graded_pages[label]
        reason = (
            "该页为题目页（不计分），未批改"
            if role == "question_only"
            else "该页为不计分页，未批改"
        )
        return {
            "questions": [],
            "student_name": "",
            "student_no": "",
            "_cache_type_label": label,
            "_answers_only": True,
            "_local_template_evidence": False,
            "_unexpected_page_type": True,
            "_not_graded_page": True,
            "_page_role": role,
            "page_type_error": reason,
            "_observed_question_numbers": [],
        }

    def _review_placeholders(self, label: str) -> list[dict]:
        """Fail-closed review rows covering every question of one known page."""
        from services.template_slots import ocr_failed_placeholder

        baseline_questions = (
            self._baselines.get(label, {}).get("ocr_result", {}).get("questions", [])
        )
        return [
            ocr_failed_placeholder(
                str(question.get("question_number") or "").strip(),
                question.get("question_type"),
                question.get("blank_count"),
            )
            for question in baseline_questions
            if str(question.get("question_number") or "").strip()
        ]

    def _with_review_placeholders(self, failure: dict, label: str) -> dict:
        """Attach routed placeholders to a failure whose page is already known.

        Pauses (pending/unavailable accelerator work) stay placeholder-free:
        they are resumable scheduling states, not recognition outcomes.
        """
        if (
            not label
            or label not in self._baselines
            or not isinstance(failure, dict)
            or failure.get("_fail_closed_questions")
            or str(failure.get("_accelerator_state") or "") in {"pending", "unavailable"}
        ):
            return failure
        placeholders = self._review_placeholders(label)
        if placeholders:
            failure = {**failure, "_fail_closed_questions": placeholders}
        return failure

    def _zero_readout_failure(self, result: dict | None) -> dict | None:
        """Reject an accepted transcription that read no answer on its page.

        Slot-contract violations, local-evidence conflicts and model
        ``unreadable`` states each stay fail-closed per question.  When no text
        answer on the page is readable and at least one is unreadable, the page
        is a failed recognition: return a persisted-failure result carrying a
        diagnostic id, the response receipt and routed review placeholders.
        A page whose every answer is genuinely blank is left unchanged.
        """
        if (
            not isinstance(result, dict)
            or result.get("error")
            or result.get("_not_graded_page")
        ):
            return None
        label = str(result.get("_cache_type_label") or "")
        baseline_questions = (
            self._baselines.get(label, {}).get("ocr_result", {}).get("questions", [])
        )
        drawing_only = {
            _normalize_question_number(str(question.get("question_number") or ""))
            for question in baseline_questions
            if str(question.get("answer_kind") or "").strip().lower() == "drawing"
        }
        text_numbers: list[str] = []
        states: list[str] = []
        for question in result.get("questions") or []:
            if not isinstance(question, dict):
                continue
            number = _normalize_question_number(str(question.get("question_number") or ""))
            if number in drawing_only:
                continue
            text_numbers.append(number)
            slots = question.get("student_slots")
            for entry in slots if isinstance(slots, list) and slots else [question]:
                if not isinstance(entry, dict):
                    continue
                state = str(entry.get("answer_state") or "").strip().lower()
                if state not in _ANSWER_STATES:
                    answer = str(entry.get("student_answer") or "").strip()
                    state = (
                        "unreadable" if "无法识别" in answer
                        else "visible" if answer else "blank"
                    )
                states.append(state)
        if not states or "visible" in states or "unreadable" not in states:
            return None
        violated = {
            _normalize_question_number(str(number))
            for number in result.get("_slot_contract_violations") or []
        }
        reason = (
            _SLOT_CONTRACT_VIOLATED
            if text_numbers and set(text_numbers) <= violated
            else _ZERO_READOUT
        )
        from services.ocr_service import ocr_failure_result

        failure = ocr_failure_result(
            TranscriptionZeroReadout(reason),
            retryable=False,
            error_code=reason,
        )
        if result.get("_ocr_receipt"):
            failure["_ocr_receipt"] = result["_ocr_receipt"]
        # The page type is certain here, so review placeholders exist even for
        # multi-page templates where an unrouted failure has none.
        failure["_fail_closed_questions"] = self._review_placeholders(label)
        logger.warning(
            "PaperCache: zero-readout transcription rejected reason=%s page=%s "
            "error_id=%s",
            reason,
            label,
            failure["error_info"]["error_id"],
        )
        return _fail_closed_ocr_result(_ZERO_READOUT_MESSAGES[reason], failure)
