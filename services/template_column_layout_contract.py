"""The column layout contract: column modes, boundary validation and the layout payload.

- ``normalize_column_mode`` turns the teacher's single/double/triple choice
  (``_COLUMN_MODES``, ``_COLUMN_MODE_ALIASES``, ``DEFAULT_COLUMN_MODE``) into one
  mode; ``normalize_layout_mode`` and the underscore names are aliases.
- ``validate_layout_boundaries`` checks a model or teacher boundary proposal
  (column bounds in 0..1000 page coordinates, each at least
  ``MIN_COLUMN_WIDTH_1000`` wide) against its crossing evidence
  (``_validate_crossing_evidence``) and returns canonical bounds;
  ``validate_three_column_proposal`` does it for three columns.
- ``_payload_from_columns`` turns column bounds into the layout payload: the
  ``column_layout`` and one question-block candidate per column.
- The contract's version names (``LAYOUT_CONTRACT_VERSION``,
  ``LAYOUT_COORDINATE_SPACE``, ``PIXEL_ENDPOINT_VERSION``) do not depend on the
  provider or model.
- ``TemplateCutError`` is the safe, provider-neutral error of layout recognition;
  ``_as_exact_int`` and ``_integer_json_number`` accept only exact integers.

Moved unchanged from services/template_column_layout.py, which still exposes these names.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


# Layout values are part of the public template draft contract.  Keep the
# contract independent from the provider/model names so a teacher can edit a
# draft without making another model request.
LAYOUT_CONTRACT_VERSION = "template-column-layout-v2"
LAYOUT_COORDINATE_SPACE = "normalized_1000_v1"
PIXEL_ENDPOINT_VERSION = "rounded_source_pixels_v1"
DEFAULT_COLUMN_MODE = "double"
_COLUMN_MODES = frozenset({"single", "double", "triple"})
_COLUMN_MODE_ALIASES = {
    "单栏": "single",
    "双栏": "double",
    "三栏": "triple",
    "one": "single",
    "two": "double",
    "three": "triple",
}
# A 1000-unit column narrower than this is not useful for question OCR.  The
# value is deliberately conservative: ordinary two/three-column pages are
# well above it, while malformed model proposals fail closed.
MIN_COLUMN_WIDTH_1000 = 80


class TemplateCutError(RuntimeError):
    """Safe, provider-neutral error for template layout recognition."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def normalize_column_mode(value: object = None, *, default: str = DEFAULT_COLUMN_MODE) -> str:
    """Normalize the teacher-facing single/double/triple layout choice.

    ``None``/an empty value means the documented default (double).  Numeric
    counts are intentionally not accepted: accepting ``2`` here makes it too
    easy for a stale caller to silently select a different layout contract.
    Chinese labels are accepted as a UI convenience but are normalized before
    they reach the layout implementation.
    """
    fallback = str(default or DEFAULT_COLUMN_MODE).strip().lower()
    fallback = _COLUMN_MODE_ALIASES.get(fallback, fallback)
    if fallback not in _COLUMN_MODES:
        raise TemplateCutError("LAYOUT_MODE_INVALID", "分栏模式默认值无效")
    if value is None or (isinstance(value, str) and not value.strip()):
        return fallback
    if not isinstance(value, str):
        raise TemplateCutError(
            "LAYOUT_MODE_INVALID",
            "分栏模式必须是 single、double 或 triple",
        )
    normalized = value.strip().lower()
    normalized = _COLUMN_MODE_ALIASES.get(normalized, normalized)
    if normalized not in _COLUMN_MODES:
        raise TemplateCutError(
            "LAYOUT_MODE_INVALID",
            "分栏模式必须是 single、double 或 triple",
        )
    return normalized


# Keep private/alternate spellings available for callers that were written
# against an earlier draft of the handoff contract.
_normalize_column_mode = normalize_column_mode
normalize_layout_mode = normalize_column_mode
_normalize_layout_mode = normalize_column_mode


def _as_exact_int(value: object) -> int | None:
    """Return an integer for JSON integer values (including exact floats)."""
    return _integer_json_number(value)


def _layout_columns_from_value(
    value: object,
    *,
    allow_implicit_index: bool = True,
) -> list[dict[str, int]]:
    """Parse an ordered list of normalized column boundaries.

    The parser is intentionally stricter than the downstream legacy crop
    parser: every column must be indexed, contiguous, and cover the full page.
    It accepts either the provider's ``columns`` list or a list of normalized
    boundary values (``[0, 500, 1000]``), which is useful for teacher drafts.
    """
    if isinstance(value, dict):
        value = value.get("columns")
    if not isinstance(value, list) or not 1 <= len(value) <= 4:
        raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "分栏边界数量无效")

    # A compact boundary list is convenient for confirmation-page edits.
    if value and all(not isinstance(item, dict) for item in value):
        bounds = [_as_exact_int(item) for item in value]
        if any(item is None for item in bounds) or len(bounds) != len(value):
            raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "分栏边界必须是整数")
        if len(bounds) < 1 or len(bounds) > 4:
            raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "分栏边界数量无效")
        # Accept either all endpoints (0..1000 included) or only the interior
        # seam coordinates supplied by a confirmation control.
        if bounds[0] != 0:
            bounds = [0, *bounds]
        if bounds[-1] != 1000:
            bounds = [*bounds, 1000]
        if len(bounds) < 2 or len(bounds) > 4:
            raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "分栏边界数量无效")
        value = [
            {"column_index": index, "x1_1000": left, "x2_1000": right}
            for index, (left, right) in enumerate(zip(bounds, bounds[1:]), 1)
        ]

    columns: list[dict[str, int]] = []
    previous_right = 0
    for position, raw in enumerate(value, 1):
        if not isinstance(raw, dict):
            raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "分栏结构无效")
        index_value = raw.get("column_index")
        if index_value is None and allow_implicit_index:
            index_value = position
        index = _as_exact_int(index_value)
        left = _as_exact_int(raw.get("x1_1000"))
        right = _as_exact_int(raw.get("x2_1000"))
        if (
            index != position
            or left is None
            or right is None
            or left != previous_right
            or not 0 <= left < right <= 1000
        ):
            raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "分栏边界必须连续覆盖 0..1000")
        if right - left < MIN_COLUMN_WIDTH_1000:
            raise TemplateCutError(
                "LAYOUT_BOUNDARY_INVALID",
                "分栏宽度过窄，无法安全进入 OCR",
            )
        columns.append({
            "column_index": int(index),
            "x1_1000": int(left),
            "x2_1000": int(right),
        })
        previous_right = int(right)
    if previous_right != 1000:
        raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "分栏边界必须覆盖整页")
    return columns


def validate_layout_boundaries(
    value: object,
    *,
    column_mode: object = None,
    layout_mode: object = None,
    column_count: object = None,
    mode: object = None,
    requested_mode: object = None,
    min_column_width_1000: int = MIN_COLUMN_WIDTH_1000,
    evidence: object = None,
) -> list[dict[str, int]]:
    """Validate a model/teacher boundary proposal and return canonical bounds.

    Triple proposals are required to contain exactly three columns.  Geometry
    or explicit evidence that a question/answer region or printed line crosses
    a proposed seam is rejected; the caller can then expose a ``needs_review``
    draft instead of sending unsafe crops downstream.
    """
    supplied_modes = [
        item
        for item in (column_mode, layout_mode, column_count, mode, requested_mode)
        if item is not None
    ]
    if len(supplied_modes) > 1:
        normalized_modes = [normalize_column_mode(item) for item in supplied_modes]
        if len(set(normalized_modes)) != 1:
            raise TemplateCutError("LAYOUT_MODE_INVALID", "分栏模式参数冲突")
        mode = normalized_modes[0]
    else:
        mode = normalize_column_mode(
            supplied_modes[0] if supplied_modes else None,
            default=DEFAULT_COLUMN_MODE,
        )
    # Use the module threshold unless a stricter caller supplied one.
    try:
        minimum = int(min_column_width_1000)
    except (TypeError, ValueError) as exc:
        raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "最小栏宽无效") from exc
    if minimum < 1 or minimum > 500:
        raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "最小栏宽无效")
    # Temporarily parse with the public threshold, then apply a caller's
    # stricter threshold without mutating the input.
    columns = _layout_columns_from_value(value)
    if mode == "single" and len(columns) != 1:
        raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "单栏必须覆盖整页")
    if mode == "double" and len(columns) != 2:
        raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "双栏必须包含一个中缝")
    if mode == "triple" and len(columns) != 3:
        raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "三栏必须包含两条边界")
    if any(item["x2_1000"] - item["x1_1000"] < minimum for item in columns):
        raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "分栏宽度过窄，无法安全进入 OCR")
    _validate_crossing_evidence(columns, evidence)
    return columns


def _numeric_flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "cross", "crossing"}
    return bool(value)


def _box_crosses_boundary(box: object, boundary: int) -> bool:
    if isinstance(box, dict):
        left = box.get("x1_1000", box.get("left", box.get("x1")))
        right = box.get("x2_1000", box.get("right", box.get("x2")))
    elif isinstance(box, (list, tuple)) and len(box) >= 4:
        left, right = box[0], box[2]
    else:
        return False
    left = _as_exact_int(left)
    right = _as_exact_int(right)
    return left is not None and right is not None and left < boundary < right


def _line_crosses_boundary(line: object, boundary: int) -> bool:
    if isinstance(line, dict):
        # Explicit flags are authoritative when supplied by a detector/model.
        for key in ("crosses_boundary", "crossing", "cross_column", "spans_gutter"):
            if key in line and _numeric_flag(line[key]):
                return True
        x = line.get("x1000", line.get("x", line.get("center_x1000")))
        if x is not None:
            x = _as_exact_int(x)
            if x is not None and abs(x - boundary) <= 2:
                return True
        left = line.get("x1_1000", line.get("left", line.get("x1")))
        right = line.get("x2_1000", line.get("right", line.get("x2")))
    elif isinstance(line, (list, tuple)) and len(line) >= 4:
        left, right = line[0], line[2]
    else:
        return False
    left = _as_exact_int(left)
    right = _as_exact_int(right)
    return left is not None and right is not None and left <= boundary <= right


def _validate_crossing_evidence(columns: list[dict[str, int]], evidence: object) -> None:
    if not isinstance(evidence, dict):
        return
    boundaries = [item["x2_1000"] for item in columns[:-1]]
    for key in (
        "cross_column",
        "cross_column_question",
        "cross_column_questions",
        "question_crossings",
        "answer_crossings",
        "boundary_crossings",
        "has_cross_column_question",
        "has_crossing_line",
        "printed_line_crossing",
        "line_crossings",
        "crossing_boundary_lines",
        "cross_column_items",
        "question_spans",
        "answer_spans",
    ):
        value = evidence.get(key)
        if _numeric_flag(value) or (isinstance(value, (list, tuple)) and bool(value)):
            raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "题目或印刷线跨越分界线")
    for key in (
        "question_boxes",
        "question_regions",
        "answer_boxes",
        "answer_regions",
        "items",
    ):
        boxes = evidence.get(key)
        if isinstance(boxes, dict):
            boxes = [boxes]
        if isinstance(boxes, (list, tuple)) and any(
            _box_crosses_boundary(box, boundary)
            for box in boxes
            for boundary in boundaries
        ):
            raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "题目或作答区跨越分界线")
    for key in ("lines", "vertical_lines", "printed_lines", "boundary_lines", "crossing_lines"):
        lines = evidence.get(key)
        if isinstance(lines, dict):
            lines = [lines]
        if isinstance(lines, (list, tuple)) and any(
            _line_crosses_boundary(line, boundary)
            for line in lines
            for boundary in boundaries
        ):
            raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "贯穿分界线的印刷线不能作为中缝")


_validate_layout_boundaries = validate_layout_boundaries


def validate_three_column_proposal(value: object, **kwargs: object) -> list[dict[str, int]]:
    kwargs.pop("column_mode", None)
    kwargs.pop("layout_mode", None)
    kwargs.pop("column_count", None)
    return validate_layout_boundaries(value, column_mode="triple", **kwargs)


_validate_three_column_proposal = validate_three_column_proposal


def _payload_from_columns(columns: list[dict[str, int]], *, source_key: str, trust: str) -> dict[str, Any]:
    normalized = [dict(item) for item in columns]
    candidates: list[dict[str, Any]] = []
    for candidate_index, column in enumerate(normalized):
        bbox = [column["x1_1000"], 0, column["x2_1000"], 1000]
        candidates.append({
            "candidate_index": candidate_index,
            "source_key": source_key,
            "candidate_role": "question_block",
            "provider_ids": [f"col_{column['column_index']}"],
            "text": "",
            "question_number_hint": "",
            "question_number_hint_source": "",
            "coordinate_trust": trust,
            "usable_for_original_overlay": True,
            "raw_sha256": hashlib.sha256(
                json.dumps(column, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "template_bbox_1000": bbox,
        })
    return {
        "column_layout": {
            "column_count": len(normalized),
            "columns": normalized,
        },
        "candidates": candidates,
    }


_build_column_payload = _payload_from_columns


def _integer_json_number(value: object) -> int | None:
    """Accept JSON numbers such as 500.0 only when they are exact integers."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None
