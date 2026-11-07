"""The template's question contract in canonical 0..1000 page space, validated fail-closed.

- Questions: ``normalize_question_schema`` normalizes each leaf's identity and its
  optional drawing-label geometry (``validate_drawing_schema_geometry``, slot
  geometry, printed solve-label anchors); ``_question_number`` and ``_is_int``
  read the pieces.
- Regions: ``normalize_answer_regions``, ``normalize_question_anchors`` and
  ``normalize_template_handwriting_regions`` validate answer boxes, question-number
  boxes and handwriting exemptions within their count, area and overlap limits.
- Confidence: ``template_confidence_diagnostics`` and
  ``validate_template_confidence_gate`` decide which model geometry is trusted
  (or teacher-confirmed) and which waits for review.

Moved unchanged from services/template_evidence.py, which still exposes these names.
"""

from __future__ import annotations

import math

from services.grading_contract import reconcile_drawing_contract
from services.processing_errors import TemplateSchemaError
from services.template_evidence_base import TemplateEvidenceError


MAX_ANSWER_REGIONS = 200


MAX_QUESTION_ANCHORS = 200


MAX_TEMPLATE_HANDWRITING_QUESTIONS = 200


MAX_TEMPLATE_HANDWRITING_BOXES = 400


# Flash's hierarchy contract allows up to 20 independent answer slots for one
# leaf.  Keep the persisted exemption limit aligned with that contract; a
# question with nine small boxes must not be rejected after correct locating.
MAX_TEMPLATE_HANDWRITING_BOXES_PER_QUESTION = 20


# Model confidence is advisory only until it passes this explicit gate.  Keep
# the version beside the thresholds so persisted diagnostics remain auditable.
TEMPLATE_CONFIDENCE_GATE_VERSION = "template-confidence-gate-v1"


TEMPLATE_ANCHOR_CONFIDENCE_THRESHOLD = 0.75


TEMPLATE_HANDWRITING_CONFIDENCE_THRESHOLD = 0.75


MAX_TEMPLATE_HANDWRITING_BOX_AREA = 250_000


MAX_TOTAL_TEMPLATE_HANDWRITING_AREA = 600_000


MAX_ANSWER_REGION_AREA = 1_000_000


MAX_TOTAL_ANSWER_REGION_AREA = 1_500_000


MAX_ANSWER_REGION_OVERLAP_RATIO = 0.25


_QUESTION_TYPES = {"choice", "fill", "solve"}


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _question_number(raw: dict, index: int) -> str:
    values: list[str] = []
    for key in ("question_number", "question_no"):
        if key not in raw or raw[key] is None:
            continue
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError(f"question {index} has invalid {key}")
        normalized = str(value).strip()
        if normalized:
            values.append(normalized)
    if not values:
        raise ValueError(f"question {index} is missing question_number")
    if len(set(values)) != 1:
        raise ValueError(f"question {index} has conflicting question numbers")
    number = values[0]
    if len(number) > 64 or any(ord(char) < 32 for char in number):
        raise ValueError(f"question {index} has invalid question_number")
    return number


def normalize_answer_regions(questions) -> list[dict]:
    """Validate and canonicalize normalized answer boxes.

    Coordinates use a 0..1000 template space and are stored as
    ``[left, top, right, bottom]``.  Booleans and floating-point coordinates
    are rejected even though Python can otherwise treat them as integers.
    """

    if not isinstance(questions, list):
        raise ValueError("questions must be a list")
    if len(questions) > MAX_ANSWER_REGIONS:
        raise ValueError(
            f"questions must contain at most {MAX_ANSWER_REGIONS} answer regions"
        )

    normalized: list[dict] = []
    seen: set[str] = set()
    total_area = 0
    for index, raw in enumerate(questions, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"question {index} must be an object")

        number = _question_number(raw, index)
        if number in seen:
            raise ValueError(f"duplicate question_number: {number}")

        raw_type = raw.get("question_type")
        if not isinstance(raw_type, str):
            raise ValueError(f"question {index} has invalid question_type")
        question_type = raw_type.strip().lower()
        if question_type not in _QUESTION_TYPES:
            raise ValueError(f"question {index} has invalid question_type")

        bbox = raw.get("answer_bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            raise ValueError(f"question {index} has invalid answer_bbox")
        if any(not _is_int(value) for value in bbox):
            raise ValueError(f"question {index} answer_bbox must contain four integers")
        left, top, right, bottom = bbox
        if not (
            0 <= left < right <= 1000
            and 0 <= top < bottom <= 1000
        ):
            raise ValueError(f"question {index} answer_bbox has invalid area")
        area = (right - left) * (bottom - top)
        if area > MAX_ANSWER_REGION_AREA:
            raise ValueError(
                f"question {index} answer_bbox exceeds maximum normalized area"
            )
        total_area += area
        if total_area > MAX_TOTAL_ANSWER_REGION_AREA:
            raise ValueError("answer regions exceed maximum total normalized area")

        source_required = raw.get("answer_source_required", True)
        if not isinstance(source_required, bool):
            raise ValueError(
                f"question {index} has invalid answer_source_required"
            )
        normalized_region = {
            "question_number": number,
            "question_type": question_type,
            "answer_bbox": [left, top, right, bottom],
        }
        # Identity fields are still sent to Vision, but a partially cropped
        # identity box must become teacher review instead of blocking answers.
        if "answer_source_required" in raw:
            normalized_region["answer_source_required"] = source_required
        normalized.append(normalized_region)
        seen.add(number)

    for index, first in enumerate(normalized):
        first_left, first_top, first_right, first_bottom = first["answer_bbox"]
        first_area = (first_right - first_left) * (first_bottom - first_top)
        for second in normalized[index + 1:]:
            second_left, second_top, second_right, second_bottom = second[
                "answer_bbox"
            ]
            overlap_width = min(first_right, second_right) - max(
                first_left, second_left
            )
            overlap_height = min(first_bottom, second_bottom) - max(
                first_top, second_top
            )
            if overlap_width <= 0 or overlap_height <= 0:
                continue
            overlap_area = overlap_width * overlap_height
            second_area = (second_right - second_left) * (
                second_bottom - second_top
            )
            if overlap_area > min(first_area, second_area) * (
                MAX_ANSWER_REGION_OVERLAP_RATIO
            ):
                raise ValueError(
                    "answer regions overlap more than 25% of the smaller region"
                )
    return normalized


def validate_drawing_schema_geometry(number, regions, anchor, *, question_index=None):
    """One fail-closed drawing geometry contract for reception and freezing."""
    if (not isinstance(regions, list) or not regions
            or len(regions) > MAX_TEMPLATE_HANDWRITING_BOXES_PER_QUESTION
            or any(not _valid_drawing_coordinates(box, 4)
                   or box[0] >= box[2] or box[1] >= box[3] for box in regions)):
        raise TemplateSchemaError("drawing_regions_invalid", number, question_index=question_index)
    if not _valid_drawing_coordinates(anchor, 2):
        raise TemplateSchemaError("drawing_anchor_invalid", number, question_index=question_index)
    # Boundary contact is valid; never clamp a point supplied inside a drawing.
    if anchor[0] > regions[0][0]:
        raise TemplateSchemaError("drawing_anchor_not_left", number, question_index=question_index)


def normalize_question_schema(questions) -> list[dict]:
    """Normalize leaf identity and optional, explicit drawing-label geometry.

    Drawing is a solve answer subtype, never a new grading question type.
    Legacy schemas keep their previous keys and printed-number semantics.
    """
    if not isinstance(questions, list):
        raise ValueError("question_schema must be a list")
    if len(questions) > MAX_QUESTION_ANCHORS:
        raise ValueError("question_schema contains too many questions")
    normalized: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(questions, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"question schema {index} must be an object")
        number = _question_number(raw, index)
        if number in seen:
            raise TemplateSchemaError("question_number_duplicate", number, question_index=index)
        raw_type = raw.get("question_type", "solve")
        if not isinstance(raw_type, str):
            raise TemplateSchemaError("question_type_invalid", number, question_index=index)
        question_type = raw_type.strip().lower()
        if question_type not in _QUESTION_TYPES:
            raise TemplateSchemaError("question_type_invalid", number, question_index=index)
        choice_mode = str(raw.get("choice_mode") or "unknown").strip().lower()
        if question_type != "choice" or choice_mode not in {"single", "multiple"}:
            choice_mode = "unknown"
        item = {
            "question_number": number,
            "question_type": question_type,
            "choice_mode": choice_mode,
        }
        for key in ("directory_node_id", "directory_parent_id"):
            if key not in raw:
                continue
            value = raw[key]
            if value == "" or (key == "directory_parent_id" and value is None
                               and not raw.get("directory_node_id")):
                continue
            if key == "directory_parent_id" and value is None:
                item[key] = None
            elif (isinstance(value, str) and value.strip() == value and value
                  and len(value) <= 128):
                item[key] = value
            else:
                raise TemplateSchemaError("directory_identity_invalid", number, question_index=index)
        if "printed_path" in raw:
            path = raw["printed_path"]
            if path == []:
                path = None
            if path is not None and (not isinstance(path, list) or not 1 <= len(path) <= 16
                    or any(value is not None and (not isinstance(value, str) or not value.strip()
                           or len(value) > 128) for value in path)):
                raise TemplateSchemaError("printed_path_invalid", number, question_index=index)
            if path is not None:
                item["printed_path"] = list(path)
        if any(key in raw for key in ("answer_kind", "drawing_regions", "label_anchor")):
            # Teacher marks win over stale model layers (incident a5d95c2a):
            # region marks re-derive the kind and a resolved non-graphic row's
            # leftover drawing geometry clears. Mark-less geometry stays
            # fail-closed — its intent is unknowable.
            raw = reconcile_drawing_contract(dict(raw))
            kind = raw.get("answer_kind", "non_graphic")
            regions = raw.get("drawing_regions", [])
            anchor = raw.get("label_anchor")
            if not isinstance(kind, str) or kind not in {"non_graphic", "drawing", "mixed"}:
                raise TemplateSchemaError("answer_kind_invalid", number, question_index=index)
            if kind == "non_graphic":
                if (regions != [] or anchor is not None) and "answer_kind" not in raw:
                    raise TemplateSchemaError("non_graphic_geometry", number, question_index=index)
                regions, anchor = [], None
            else:
                validate_drawing_schema_geometry(number, regions, anchor, question_index=index)
            if kind != "non_graphic" or raw.get("directory_node_id"):
                item.update(answer_kind=kind, drawing_regions=[list(box) for box in regions],
                            label_anchor=list(anchor) if anchor is not None else None)
        if raw.get("slot_ids", ()) is None:
            # ``slot_ids: null`` in a teacher schema edit: every position of the
            # question was deleted, so its slot numbers and kinds go with them.
            raw = {key: value for key, value in raw.items() if key not in {"slot_ids", "region_kinds"}}
        item.update(_slot_geometry_metadata(raw, number))
        if raw.get("answer_position") == "unmarked" and question_type in {"choice", "fill"}:
            item["answer_position"] = "unmarked"
        normalized.append(item)
        seen.add(number)
    return normalized


def _slot_geometry_metadata(raw: dict, number: str, count: int | None = None) -> dict:
    """Preserve explicit identities; absent legacy fields stay absent."""
    result = {}
    if "slot_ids" in raw:
        from services.template_slots import normalize_slot_ids
        ids = raw["slot_ids"]
        size = count if count is not None else (len(ids) if isinstance(ids, list) else 0)
        result["slot_ids"] = normalize_slot_ids(number, ids, size)
    if "region_kinds" in raw:
        kinds = raw["region_kinds"]
        size = count if count is not None else len(result.get("slot_ids", kinds if isinstance(kinds, list) else []))
        if (not isinstance(kinds, list) or len(kinds) != size
                or any(kind not in {"answer", "drawing"} for kind in kinds)):
            raise ValueError(f"question {number} has invalid slot region_kinds")
        result["region_kinds"] = list(kinds)
    return result


def _bind_slot_geometry(schema: list[dict], regions: list[dict]) -> list[dict]:
    """Join schema IDs to geometry without sorting or repairing conflicts."""
    by_number = {item["question_number"]: item for item in schema}
    result = []
    for region in regions:
        item = dict(region)
        question = by_number.get(item["question_number"], {})
        for key, value in _slot_geometry_metadata(question, item["question_number"], len(item["regions"])).items():
            if key in item and item[key] != value:
                raise ValueError(f"question {item['question_number']} slot geometry binding conflicts with schema")
            item[key] = value
        result.append(item)
    return result


def _has_text_slot_labels(item: dict, drawing_questions: dict) -> bool:
    if item["question_number"] not in drawing_questions:
        return True
    return bool(item.get("slot_ids") and "answer" in item.get("region_kinds", []))


def _valid_drawing_coordinates(values: object, size: int) -> bool:
    return (isinstance(values, list) and len(values) == size
            and all(not isinstance(value, bool) and isinstance(value, (int, float))
                    and math.isfinite(value) and 0 <= value <= 1000 for value in values))


def _validate_printed_solve_anchors(question_schema: list[dict], anchors: list[dict], answer_regions=None) -> None:
    """Require explicit ownership: unified answer boxes or legacy printed anchors."""
    printed = {item["question_number"] for item in anchors if item.get("question_number_bbox")}
    boxed = {item["question_number"] for item in normalize_template_handwriting_regions(answer_regions or []) if item["regions"]}
    for question in question_schema:
        if (question["question_type"] == "solve"
                and question.get("answer_kind") not in {"drawing", "mixed"}
                and question["question_number"] not in printed | boxed):
            raise ValueError(
                f"第 {question['question_number']} 题缺少作答框或印刷题号位置，请教师补充"
            )


def _normalize_template_confidence(raw: object, *, label: str) -> float:
    """Validate a model confidence without accepting NaN/Infinity or booleans."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"{label} confidence must be a finite number between 0 and 1")
    value = float(raw)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{label} confidence must be a finite number between 0 and 1")
    return round(value, 6)


def _normalize_template_provenance(raw: dict, *, label: str) -> dict[str, object]:
    """Keep the small provenance contract while remaining compatible with old artifacts."""
    result: dict[str, object] = {}
    if "confidence" in raw:
        result["confidence"] = _normalize_template_confidence(
            raw.get("confidence"), label=label
        )
    if "human_confirmed" in raw:
        value = raw.get("human_confirmed")
        if not isinstance(value, bool):
            raise ValueError(f"{label} human_confirmed must be a boolean")
        result["human_confirmed"] = value
    if "confidence_source" in raw:
        source = raw.get("confidence_source")
        if not isinstance(source, str) or source.strip().lower() not in {
            "model",
            "teacher",
            "legacy",
        }:
            raise ValueError(
                f"{label} confidence_source must be model, teacher, or legacy"
            )
        result["confidence_source"] = source.strip().lower()
    return result


def _template_item_is_human_confirmed(item: dict) -> bool:
    return bool(item.get("human_confirmed") is True) or str(
        item.get("confidence_source") or ""
    ).strip().lower() == "teacher"


def _template_confidence_is_trusted(
    item: dict,
    *,
    threshold: float,
    allow_human_confirmed: bool = True,
) -> bool:
    """Missing confidence is a legacy artifact; explicit low values are not."""
    if allow_human_confirmed and _template_item_is_human_confirmed(item):
        return True
    confidence = item.get("confidence")
    if confidence is None:
        return True
    return float(confidence) >= threshold


def template_confidence_diagnostics(
    anchors: object,
    handwriting_regions: object,
    *,
    allow_human_confirmed: bool = True,
) -> dict[str, object]:
    """Return a serializable confidence gate result for UI and model callers."""
    normalized_anchors = normalize_question_anchors(anchors or [])
    normalized_regions = normalize_template_handwriting_regions(
        handwriting_regions or []
    )
    low_anchors = [
        item["question_number"]
        for item in normalized_anchors
        if not _template_confidence_is_trusted(
            item,
            threshold=TEMPLATE_ANCHOR_CONFIDENCE_THRESHOLD,
            allow_human_confirmed=allow_human_confirmed,
        )
    ]
    # A model may attach a confidence score to an explicit "no handwriting"
    # result. There is no geometry to white out in that case, so the score must
    # not block an otherwise usable template artifact.
    low_regions = [
        item["question_number"]
        for item in normalized_regions
        if item.get("regions")
        if not _template_confidence_is_trusted(
            item,
            threshold=TEMPLATE_HANDWRITING_CONFIDENCE_THRESHOLD,
            allow_human_confirmed=allow_human_confirmed,
        )
    ]
    return {
        "gate_version": TEMPLATE_CONFIDENCE_GATE_VERSION,
        "anchor_threshold": TEMPLATE_ANCHOR_CONFIDENCE_THRESHOLD,
        "handwriting_threshold": TEMPLATE_HANDWRITING_CONFIDENCE_THRESHOLD,
        "low_confidence_question_anchors": low_anchors,
        "low_confidence_handwriting_regions": low_regions,
        "needs_review": bool(low_anchors or low_regions),
        "review_reason": "low_confidence" if (low_anchors or low_regions) else "",
    }


def validate_template_confidence_gate(
    anchors: object,
    handwriting_regions: object,
    *,
    allow_human_confirmed: bool = True,
) -> dict[str, object]:
    """Fail closed when an unconfirmed model result is below the confidence gate."""
    diagnostics = template_confidence_diagnostics(
        anchors,
        handwriting_regions,
        allow_human_confirmed=allow_human_confirmed,
    )
    if diagnostics["needs_review"]:
        raise TemplateEvidenceError(
            "模板题号或豁免框置信度不足，需人工复核",
            diagnostics=diagnostics,
        )
    return diagnostics


def _trusted_template_handwriting_regions(
    regions: list[dict],
    *,
    allow_human_confirmed: bool = True,
) -> list[dict]:
    """Drop model boxes below the gate before any pixel whiteout is attempted."""
    return [
        item
        for item in regions
        if _template_confidence_is_trusted(
            item,
            threshold=TEMPLATE_HANDWRITING_CONFIDENCE_THRESHOLD,
            allow_human_confirmed=allow_human_confirmed,
        )
    ]


def _validated_template_handwriting_regions(
    regions: list[dict],
    *,
    allow_human_confirmed: bool = True,
) -> list[dict]:
    """Normalize exemption boxes and reject explicit low-confidence boxes.

    ``_trusted_template_handwriting_regions`` intentionally remains a filtering
    helper for diagnostics and legacy callers.  Pixel-producing paths must use
    this fail-closed wrapper so a low-confidence model box cannot silently turn
    into a partial whiteout (or disappear while the caller assumes it was used).
    """
    normalized = normalize_template_handwriting_regions(regions)
    diagnostics = template_confidence_diagnostics(
        [],
        normalized,
        allow_human_confirmed=allow_human_confirmed,
    )
    if diagnostics["needs_review"]:
        raise TemplateEvidenceError(
            "模板豁免框置信度不足，需人工复核",
            diagnostics=diagnostics,
        )
    return normalized


def normalize_question_anchors(anchors) -> list[dict]:
    """Validate question-number identity and an optional review box.

    Older persisted artifacts may contain only the question number, so the
    number box remains optional at the storage boundary. New model prompts
    request ``question_number_bbox`` so the teacher can inspect the printed
    number on the template preview.
    """
    if not isinstance(anchors, list):
        raise ValueError("question_anchors must be a list")
    if len(anchors) > MAX_QUESTION_ANCHORS:
        raise ValueError(
            f"question_anchors must contain at most {MAX_QUESTION_ANCHORS} items"
        )

    normalized: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(anchors, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"question anchor {index} must be an object")
        number = _question_number(raw, index)
        if number in seen:
            raise ValueError(f"duplicate question anchor: {number}")

        normalized_item = {
            "question_number": number,
        }
        # Read old saved artifacts without making the model continue this
        # separate geometry task. New responses intentionally omit this key.
        # An empty box is the teacher deleting the printed-number marker: the
        # question keeps its identity without a printed position.
        if raw.get("question_number_bbox") not in (None, [], ()):
            bbox = raw.get("question_number_bbox")
            if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                raise ValueError(
                    f"question anchor {number} has invalid question_number_bbox"
                )
            if any(not _is_int(value) for value in bbox):
                raise ValueError(
                    f"question anchor {number} question_number_bbox must contain four integers"
                )
            left, top, right, bottom = bbox
            if not (0 <= left < right <= 1000 and 0 <= top < bottom <= 1000):
                raise ValueError(
                    f"question anchor {number} question_number_bbox has invalid area"
                )
            normalized_item["question_number_bbox"] = [left, top, right, bottom]
        normalized_item.update(
            _normalize_template_provenance(
                raw,
                label=f"question anchor {number}",
            )
        )
        normalized.append(normalized_item)
        seen.add(number)
    return normalized


def normalize_template_handwriting_regions(regions) -> list[dict]:
    """Validate template handwriting exemptions in canonical 0..1000 space."""

    if not isinstance(regions, list):
        raise ValueError("template_handwriting_regions must be a list")
    if len(regions) > MAX_TEMPLATE_HANDWRITING_QUESTIONS:
        raise ValueError(
            "template_handwriting_regions contains too many questions"
        )

    normalized: list[dict] = []
    seen: set[str] = set()
    total_boxes = 0
    total_area = 0
    for index, raw in enumerate(regions, 1):
        if not isinstance(raw, dict):
            raise ValueError(
                f"template handwriting region {index} must be an object"
            )
        number = _question_number(raw, index)
        if number in seen:
            raise ValueError(f"duplicate template handwriting question: {number}")
        raw_boxes = raw.get("regions")
        if not isinstance(raw_boxes, list):
            raise ValueError(
                f"template handwriting question {number} regions must be a list"
            )
        if len(raw_boxes) > MAX_TEMPLATE_HANDWRITING_BOXES_PER_QUESTION:
            raise ValueError(
                f"第 {number} 题的作答框超过 {MAX_TEMPLATE_HANDWRITING_BOXES_PER_QUESTION} 个，请删除多余的框"
            )

        boxes: list[list[int]] = []
        for raw_bbox in raw_boxes:
            if not isinstance(raw_bbox, (list, tuple)) or len(raw_bbox) != 4:
                raise ValueError(
                    f"第 {number} 题有一个作答框坐标无效，请删除后重新框选"
                )
            if any(not _is_int(value) for value in raw_bbox):
                raise ValueError(
                    f"第 {number} 题有一个作答框坐标无效，请删除后重新框选"
                )
            left, top, right, bottom = raw_bbox
            if not (0 <= left < right <= 1000 and 0 <= top < bottom <= 1000):
                raise ValueError(
                    f"第 {number} 题有一个作答框超出页面或面积为零，请调整后重新保存"
                )
            area = (right - left) * (bottom - top)
            if area > MAX_TEMPLATE_HANDWRITING_BOX_AREA:
                raise ValueError(
                    f"第 {number} 题有一个框超过整页的四分之一，请缩小到实际作答或作图范围"
                )
            total_boxes += 1
            total_area += area
            if total_boxes > MAX_TEMPLATE_HANDWRITING_BOXES:
                raise ValueError(f"本页作答框超过 {MAX_TEMPLATE_HANDWRITING_BOXES} 个，请删除多余的框")
            if total_area > MAX_TOTAL_TEMPLATE_HANDWRITING_AREA:
                raise ValueError("本页作答框合计覆盖超过整页的六成，请缩小过大的框")
            boxes.append([left, top, right, bottom])

        normalized_item = {
            "question_number": number,
            "regions": boxes,
        }
        normalized_item.update(_slot_geometry_metadata(raw, number, len(boxes)))
        if raw.get("geometry_kind") == "label_anchor":
            normalized_item["geometry_kind"] = "label_anchor"
        normalized_item.update(
            _normalize_template_provenance(
                raw,
                label=f"template handwriting question {number}",
            )
        )
        normalized.append(normalized_item)
        seen.add(number)
    return normalized
