"""What a vision answer may contain before it is trusted: the student's identity and the transcriptions.

- ``_sanitize_student_name`` keeps a plausible 2-4 character Chinese name
  (``_NAME_PATTERN``) and ``_sanitize_student_no`` a 6-12 digit student number
  (``_STUDENT_NO_PATTERN``); anything else becomes empty.
- ``_normalize_roster_candidate_names`` keeps only unique, exact names from the
  current class roster (at most ``IDENTITY_ROSTER_MAX_CANDIDATES``); the model
  never widens the roster.  ``IDENTITY_ROSTER_AUTO_MATCH_CONFIDENCE`` is the
  confidence a roster match needs to be assigned automatically, and
  ``_build_identity_crop`` builds the full-resolution identity composite off the
  event loop.
- ``validate_visual_transcriptions`` requires each answer to declare evidence
  consistent with its text (``_ANSWER_STATES``), so self-contradictory output
  never reaches grading.

Moved unchanged from services/ocr_service.py, which still exposes these names.
"""

import logging
import os
import re
import uuid

from services.identity_regions import IDENTITY_LEFT_CROP_RATIO, IDENTITY_TOP_CROP_RATIO
from services.preprocess import _validate_source_dimensions

# services.ocr_service's logger, so these records keep their logger name.
logger = logging.getLogger("services.ocr_service")


_NAME_PATTERN = re.compile(r"^[一-鿿]{2,4}$")
_STUDENT_NO_PATTERN = re.compile(r"^\d{6,12}$")
_ANSWER_STATES = {"blank", "visible", "unreadable"}
IDENTITY_ROSTER_AUTO_MATCH_CONFIDENCE = 0.90
IDENTITY_ROSTER_MAX_CANDIDATES = 3


def _sanitize_student_name(raw: str) -> str:
    """Keep plausible 2–4 Chinese names; reject OCR status/prose sentinels."""
    if not raw:
        return ""
    name = raw.strip().strip('"').strip("'").strip("：:").strip()
    name = re.sub(r"^姓名\s*[：:]\s*", "", name).strip()
    invalid_markers = (
        "无姓名", "没有", "学号", "无法识别", "未识别", "看不清",
        "不清楚", "姓名不详", "空字符串",
    )
    if any(marker in name for marker in invalid_markers):
        return ""
    if _NAME_PATTERN.match(name):
        return name
    match = re.search(r"[一-鿿]{2,4}", name)
    if match and _NAME_PATTERN.match(match.group(0)):
        return match.group(0)
    return ""

def _sanitize_student_no(raw: str) -> str:
    """Keep a 6–12 digit student number; extract digits from mixed text, else ''."""
    if not raw:
        return ""
    no = str(raw).strip().strip('"').strip("'").strip("：:").strip()
    if _STUDENT_NO_PATTERN.match(no):
        return no
    # Fall back to the first 6–12 digit run embedded in prose like "学号：123456"
    digits = re.findall(r"\d{6,12}", no)
    return digits[0] if digits else ""


def validate_visual_transcriptions(
    raw_questions: object,
    *,
    slot_contracts: dict | None = None,
    drawing_only_numbers: set[str] | None = None,
) -> list[dict]:
    """Require each OCR answer to declare evidence consistent with its text.

    This cannot prove that a generative model read the right strokes, but it
    prevents structurally self-contradictory output from entering grading and
    gives later audit/review stages an explicit visual-evidence state.
    """
    if not isinstance(raw_questions, list):
        raise ValueError("questions must be a list")

    questions: list[dict] = []
    for index, raw_question in enumerate(raw_questions, 1):
        if not isinstance(raw_question, dict):
            raise ValueError(f"question {index} must be an object")
        question_number = str(raw_question.get("question_number") or "").strip()
        if not question_number:
            raise ValueError(f"question {index} is missing question_number")

        if slot_contracts is not None and question_number in slot_contracts:
            from services.template_slots import bind_student_slots
            raw_question = bind_student_slots(raw_question, question_number, {
                "slot_ids": slot_contracts[question_number],
            })

        # A pure drawing question has no text slot.  The visual model may still
        # return its required question row with ``student_slots: []``; normalize
        # that structural blank without inventing a textual answer.
        if drawing_only_numbers and question_number in drawing_only_numbers:
            if raw_question.get("student_slots") not in (None, []):
                raise ValueError(f"question {index} pure drawing has unexpected text slots")
            raw_question = dict(raw_question)
            raw_question.setdefault("student_answer", "")
            raw_question.setdefault("answer_state", "blank")

        state = str(raw_question.get("answer_state") or "").strip().lower()
        if state not in _ANSWER_STATES:
            raise ValueError(f"question {index} has invalid answer_state")
        raw_answer = raw_question.get("student_answer")
        if not isinstance(raw_answer, str):
            raise ValueError(f"question {index} student_answer must be text")
        answer = raw_answer.strip()

        # A multi-slot question whose every box is blank keeps only its
        # positional "；" separators. That is an empty transcription, not a
        # contradiction that may reject the whole page.
        #
        # A state that contradicts its own text (blank with text, visible
        # without text) says nothing reliable about this one answer. Keep it
        # fail-closed for this question only, as contracted slots already do,
        # instead of discarding every other answer on the page.
        if (
            (state == "blank" and answer.replace("；", "").strip())
            or (state == "visible" and (not answer or answer == "[无法识别]"))
        ):
            state = "unreadable"
        if state == "unreadable":
            if answer.startswith("[") and answer.endswith("]") and "无法识别" in answer:
                # Providers occasionally qualify the fixed marker (for
                # example "[手写汉字，无法识别]").  The explicit unreadable
                # state is already fail-closed, so canonicalize that harmless
                # wording variation instead of rejecting the whole paper.
                answer = "[无法识别]"
            else:
                # A provider can emit an empty or speculative transcription
                # alongside the unreadable state.  Keep the conservative state
                # and discard that text so one malformed question cannot reject
                # an otherwise complete paper or leak a guessed answer into
                # grading.
                answer = "[无法识别]"

        normalized = dict(raw_question)
        normalized["question_number"] = question_number
        normalized["student_answer"] = answer
        normalized["answer_state"] = state
        questions.append(normalized)
    return questions


def _normalize_roster_candidate_names(raw_candidates: object, roster_names: list[str]) -> list[str]:
    """Keep only unique, exact names from the current class roster.

    The vision model may suggest candidates, but it must never expand the roster
    used for automatic assignment.  Teachers retain the separate manual-create
    path when a genuinely new student appears.
    """
    allowed = {str(name).strip() for name in roster_names if str(name).strip()}
    if not allowed:
        return []
    if isinstance(raw_candidates, str):
        values = [raw_candidates]
    elif isinstance(raw_candidates, list):
        values = raw_candidates
    else:
        values = []

    candidates: list[str] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, dict):
            value = value.get("name", "")
        name = _sanitize_student_name(str(value or ""))
        if name and name in allowed and name not in seen:
            candidates.append(name)
            seen.add(name)
        if len(candidates) >= IDENTITY_ROSTER_MAX_CANDIDATES:
            break
    return candidates


def _build_identity_crop(image_path: str) -> str:
    """Build the full-resolution identity composite outside the event loop."""
    from PIL import Image, ImageEnhance, ImageOps

    crop_path = f"{image_path}.identity-{uuid.uuid4().hex}.jpg"
    temp_path = f"{crop_path}.{uuid.uuid4().hex}.tmp"
    try:
        with Image.open(image_path) as source:
            _validate_source_dimensions(source)
            oriented = ImageOps.exif_transpose(source)
            try:
                image = oriented.convert("RGB")
            finally:
                if oriented is not source:
                    oriented.close()
        try:
            width, height = image.size
            top_height = min(height, max(1, int(height * IDENTITY_TOP_CROP_RATIO)))
            left_width = min(width, max(1, int(width * IDENTITY_LEFT_CROP_RATIO)))

            header = image.crop((0, 0, width, top_height))
            left = image.crop((0, 0, left_width, height)).transpose(
                Image.Transpose.ROTATE_270
            )
            try:
                composite = Image.new(
                    "RGB",
                    (max(header.width, left.width), header.height + left.height),
                    "white",
                )
                try:
                    composite.paste(header, (0, 0))
                    composite.paste(left, (0, header.height))
                    enhanced = ImageEnhance.Contrast(composite).enhance(1.25)
                    try:
                        enhanced.save(
                            temp_path,
                            "JPEG",
                            quality=95,
                            subsampling=0,
                        )
                    finally:
                        enhanced.close()
                finally:
                    composite.close()
            finally:
                header.close()
                left.close()
        finally:
            image.close()
        os.replace(temp_path, crop_path)
        return crop_path
    except Exception:
        for path in (temp_path, crop_path):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning("Failed to clean identity crop %s", path)
        raise
