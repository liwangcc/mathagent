"""Which pixels of an aligned page belong to each confirmed answer box.

- ``_answer_crop_boxes`` expands and clamps the confirmed answer boxes into the
  exact pixel boxes cropped, keeping neighbouring rows of one lane apart
  (``_same_answer_lane``, ``_horizontal_overlap``); ``_answer_crops`` crops them
  (no production code calls it; tests use it).
- ``_answer_region_source_coverage`` and ``_answer_crop_source_coverage`` require
  and report that every answer box holds real source pixels.
- ``_frozen_column_bounds`` and ``_resolved_frozen_pixel_bounds`` turn the frozen
  column plan into the pixel bounds of each column; ``_pixel_regions`` converts
  0..1000 boxes to pixels.

Moved unchanged from services/template_evidence.py, which still exposes these names.
"""

from __future__ import annotations

import math
from typing import Any

from services.template_anchor_crops import validate_anchor_crop_plan
from services.template_evidence_base import TemplateEvidenceError, np


def _answer_region_source_coverage(
    source_mask: np.ndarray,
    answer_regions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Require every confirmed answer bbox to contain only real source pixels."""

    height, width = source_mask.shape[:2]
    results: list[dict[str, Any]] = []
    incomplete: list[str] = []
    for region in answer_regions:
        number = str(region.get("question_number") or "").strip()
        left, top, right, bottom = region["answer_bbox"]
        x1 = max(0, min(width, int(math.floor(float(left) * width / 1000))))
        y1 = max(0, min(height, int(math.floor(float(top) * height / 1000))))
        x2 = max(0, min(width, int(math.ceil(float(right) * width / 1000))))
        y2 = max(0, min(height, int(math.ceil(float(bottom) * height / 1000))))
        crop = source_mask[y1:y2, x1:x2]
        total = int(crop.size)
        covered = int(np.count_nonzero(crop == 255)) if total else 0
        fraction = covered / total if total else 0.0
        complete = total > 0 and covered == total
        results.append({
            "question_number": number,
            "covered_pixels": covered,
            "total_pixels": total,
            "coverage": round(fraction, 6),
            "complete": complete,
        })
        if not complete:
            incomplete.append(number)
    return results, incomplete


def _answer_crop_boxes(
    regions: list[dict],
    width: int,
    height: int,
    *,
    column_bounds: list[tuple[int, int]] | None = None,
) -> list[dict[str, Any]]:
    """Return the exact expanded/clamped boxes consumed by ``_answer_crops``."""

    pixels = _pixel_regions(regions, width, height)
    wide_pad_x = max(8, min(96, int(round(width * 0.035))))
    # Template OCR can place a choice bracket several dozen pixels away from
    # the student's large handwritten letter.  Keep a wider horizontal safety
    # window while the neighboring-question midlines still clamp vertically.
    tight_pad_x = max(16, min(192, int(round(width * 0.08))))
    minimal_left_pad_x = max(2, min(8, int(round(width * 0.002))))
    pad_y = max(5, min(32, int(round(height * 0.015))))
    normalized_columns: list[tuple[int, int]] = []
    for bounds in column_bounds or []:
        if (
            not isinstance(bounds, tuple)
            or len(bounds) != 2
            or any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in bounds
            )
            or not 0 <= bounds[0] < bounds[1] <= width
        ):
            raise TemplateEvidenceError("answer crop column bounds are invalid")
        normalized_columns.append(bounds)
    boxes: list[dict[str, Any]] = []
    for current in pixels:
        above = [
            other for other in pixels
            if other is not current
            and other["cy"] < current["cy"]
            and _same_answer_lane(current, other, width)
        ]
        below = [
            other for other in pixels
            if other is not current
            and other["cy"] > current["cy"]
            and _same_answer_lane(current, other, width)
        ]
        previous = max(above, key=lambda value: value["cy"], default=None)
        following = min(below, key=lambda value: value["cy"], default=None)
        if current["question_type"] == "choice":
            left_pad_x = right_pad_x = tight_pad_x
        else:
            left_pad_x = minimal_left_pad_x
            right_pad_x = wide_pad_x
            if current["question_type"] == "fill" and normalized_columns:
                overlaps = [
                    max(0, min(current["x2"], right) - max(current["x1"], left))
                    for left, right in normalized_columns
                ]
                column_position = max(
                    range(len(normalized_columns)),
                    key=lambda index: overlaps[index],
                )
                if overlaps[column_position] <= 0:
                    raise TemplateEvidenceError(
                        "answer region falls outside frozen columns"
                    )
                column_left, column_right = normalized_columns[column_position]
                # ``answer_bbox`` remains the ownership boundary. The rendered
                # OCR capture uses the complete frozen column row, because a
                # student can begin before the printed line or continue past a
                # model-estimated handwriting box. Template subtraction removes
                # the printed question before this capture reaches Vision.
                left_pad_x = max(left_pad_x, current["x1"] - column_left)
                right_pad_x = max(right_pad_x, column_right - current["x2"])
        crop_top = max(0, current["y1"] - pad_y)
        if previous is not None:
            crop_top = max(crop_top, min(current["y1"], previous["y2"]))
        crop_bottom = min(height, current["y2"] + pad_y)
        if following is not None:
            crop_bottom = min(
                crop_bottom,
                max(current["y2"], following["y1"]),
            )
        x1 = max(0, current["x1"] - left_pad_x)
        x2 = min(width, current["x2"] + right_pad_x)
        if x2 <= x1 or crop_bottom <= crop_top:
            raise TemplateEvidenceError("answer region produced an empty crop")
        boxes.append({
            "question_number": current["question_number"],
            "question_type": current["question_type"],
            "x1": x1,
            "y1": crop_top,
            "x2": x2,
            "y2": crop_bottom,
        })
    return boxes


def _answer_crop_source_coverage(
    source_mask: np.ndarray,
    regions: list[dict[str, Any]],
    *,
    column_bounds: list[tuple[int, int]] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Report source coverage for the authoritative answer boxes.

    The OCR crop intentionally includes a small amount of context around the
    confirmed answer box.  That context may extend beyond a photographed page
    after perspective alignment, but it is not part of the student's answer
    region and must not make an otherwise readable question fail closed.
    """

    height, width = source_mask.shape[:2]
    raw_results, incomplete = _answer_region_source_coverage(
        source_mask,
        regions,
    )
    boxes = _answer_crop_boxes(
        regions,
        width,
        height,
        column_bounds=column_bounds,
    )
    results: list[dict[str, Any]] = []
    for raw, box in zip(raw_results, boxes):
        results.append({
            **raw,
            "crop_box": [box["x1"], box["y1"], box["x2"], box["y2"]],
        })
    return results, incomplete


def _frozen_column_bounds(
    question_crop_plan: dict[str, Any] | None,
    width: int,
    height: int,
) -> list[tuple[int, int]] | None:
    """Convert the immutable column plan to pixel bounds for answer capture."""

    if question_crop_plan is None:
        return None
    if (
        isinstance(width, bool)
        or not isinstance(width, int)
        or width < 1
        or isinstance(height, bool)
        or not isinstance(height, int)
        or height < 1
    ):
        raise TemplateEvidenceError("frozen column dimensions are invalid")
    plan = validate_anchor_crop_plan(question_crop_plan)
    bounds: list[tuple[int, int]] = []
    for raw_column in plan["parameters"]["columns"]:
        bounds.append(_resolved_frozen_pixel_bounds(raw_column, width, height))
    return bounds


def _resolved_frozen_pixel_bounds(
    raw_column: dict[str, Any],
    width: int,
    height: int,
) -> tuple[int, int]:
    """Resolve a frozen column to the exact pixels used by its source image."""
    try:
        normalized_left = int(raw_column["x1_1000"])
        normalized_right = int(raw_column["x2_1000"])
    except (KeyError, TypeError, ValueError) as exc:
        bbox = raw_column.get("bbox_1000") if isinstance(raw_column, dict) else None
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            raise TemplateEvidenceError("frozen column boundaries are invalid") from exc
        normalized_left, normalized_right = int(bbox[0]), int(bbox[2])
    expected_left = int(round(normalized_left * width / 1000))
    expected_right = int(round(normalized_right * width / 1000))

    if not isinstance(raw_column, dict):
        raise TemplateEvidenceError("frozen column metadata is invalid")
    for key, expected in (("source_width", width), ("source_height", height)):
        if key in raw_column:
            value = raw_column[key]
            if isinstance(value, bool) or not isinstance(value, int) or value != expected:
                raise TemplateEvidenceError(f"frozen column {key} does not match image")

    endpoint_values: dict[str, int] = {}
    for canonical, aliases in {
        "left": ("crop_left_px", "crop_left", "pixel_crop_left"),
        "right": ("crop_right_px", "crop_right", "pixel_crop_right"),
    }.items():
        seen: list[int] = []
        for key in aliases:
            if key in raw_column:
                value = raw_column[key]
                if isinstance(value, bool) or not isinstance(value, int):
                    raise TemplateEvidenceError(f"frozen column {key} is invalid")
                seen.append(int(value))
        if seen and len(set(seen)) != 1:
            raise TemplateEvidenceError("frozen column pixel endpoint aliases disagree")
        if seen:
            endpoint_values[canonical] = seen[0]
    actual = raw_column.get("actual_pixel_endpoints")
    if actual is not None:
        if not isinstance(actual, dict):
            raise TemplateEvidenceError("frozen column actual pixel endpoints are invalid")
        for canonical in ("left", "right"):
            value = actual.get(canonical)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TemplateEvidenceError("frozen column actual pixel endpoints are incomplete")
            if canonical in endpoint_values and endpoint_values[canonical] != value:
                raise TemplateEvidenceError("frozen column actual pixel endpoints disagree")
            endpoint_values[canonical] = int(value)
    if endpoint_values:
        if set(endpoint_values) != {"left", "right"}:
            raise TemplateEvidenceError("frozen column actual pixel endpoints are incomplete")
        if endpoint_values != {"left": expected_left, "right": expected_right}:
            raise TemplateEvidenceError(
                "frozen column actual pixel endpoints disagree with normalized boundaries"
            )
        left, right = endpoint_values["left"], endpoint_values["right"]
    else:
        left, right = expected_left, expected_right
    if not (0 <= left < right <= width):
        raise TemplateEvidenceError("frozen column produced an empty image")

    crop_box = raw_column.get("crop_box_px")
    if crop_box is not None:
        if not isinstance(crop_box, (list, tuple)) or len(crop_box) != 4:
            raise TemplateEvidenceError("frozen column crop_box_px is invalid")
        if list(crop_box) != [left, 0, right, height]:
            raise TemplateEvidenceError("frozen column crop_box_px does not match image")
    endpoint_version = raw_column.get("crop_endpoint_version")
    if endpoint_version is not None and str(endpoint_version) != "rounded_source_pixels_v1":
        raise TemplateEvidenceError("frozen column crop endpoint version is unsupported")
    coordinate_space = raw_column.get("crop_coordinate_space")
    if coordinate_space is not None and str(coordinate_space) != "source_pixels":
        raise TemplateEvidenceError("frozen column crop coordinate space is invalid")
    return left, right


def _pixel_regions(regions: list[dict], width: int, height: int) -> list[dict]:
    result: list[dict] = []
    for region in regions:
        left, top, right, bottom = region["answer_bbox"]
        x1 = max(0, min(width - 1, int(math.floor(left * width / 1000))))
        y1 = max(0, min(height - 1, int(math.floor(top * height / 1000))))
        x2 = max(x1 + 1, min(width, int(math.ceil(right * width / 1000))))
        y2 = max(y1 + 1, min(height, int(math.ceil(bottom * height / 1000))))
        result.append({
            **region,
            "x1": x1,
            "y1": y1,
            "x2": x2,
            "y2": y2,
            "cx": (x1 + x2) / 2.0,
            "cy": (y1 + y2) / 2.0,
        })
    return result


def _horizontal_overlap(left: dict, right: dict) -> bool:
    intersection = min(left["x2"], right["x2"]) - max(left["x1"], right["x1"])
    if intersection <= 0:
        return False
    narrower = min(left["x2"] - left["x1"], right["x2"] - right["x1"])
    return intersection >= max(1, int(round(narrower * 0.10)))


def _same_answer_lane(left: dict, right: dict, page_width: int) -> bool:
    """Keep neighboring rows together even when their answer x positions vary."""
    return _horizontal_overlap(left, right) or (
        abs(left["cx"] - right["cx"]) <= page_width * 0.22
    )


def _answer_crops(
    aligned_rgb: np.ndarray,
    regions: list[dict],
) -> list[np.ndarray]:
    """Crop confirmed answer boxes without consuming a neighboring answer."""

    height, width = aligned_rgb.shape[:2]
    crops: list[np.ndarray] = []
    for box in _answer_crop_boxes(regions, width, height):
        crops.append(np.ascontiguousarray(
            aligned_rgb[box["y1"]:box["y2"], box["x1"]:box["x2"]]
        ))
    return crops
