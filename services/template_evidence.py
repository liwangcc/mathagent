"""Local template alignment and answer-only evidence image generation.

The artifact format is intentionally small and self-verifying.  A caller keeps
the returned mapping in template metadata, then passes a mapping keyed by
``page_type_id`` to :class:`TemplateEvidenceMatcher`.
"""

from __future__ import annotations

from services.grading_contract import reconcile_drawing_contract
from services.processing_errors import TemplateSchemaError

import hashlib
import importlib
import json
import math
import os
import re
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from services.accelerator_artifacts import validate_uploaded_artifact_file
from services.template_anchor_crops import validate_anchor_crop_plan
from services.template_canonicalization_contract import (
    validate_canonicalization_metadata,
)
from services.template_evidence_base import (
    _LazyModule,
    cv2,
    np,
    Image,
    ImageDraw,
    ImageFont,
    ImageOps,
    MAX_IMAGE_EDGE,
    MAX_SOURCE_PIXELS,
    _PAGE_TYPE_RE,
    _SHA256_RE,
    TemplateEvidenceError,
    EvidenceResult,
)
from services.template_evidence_schema import (
    MAX_ANSWER_REGIONS,
    MAX_QUESTION_ANCHORS,
    MAX_TEMPLATE_HANDWRITING_QUESTIONS,
    MAX_TEMPLATE_HANDWRITING_BOXES,
    MAX_TEMPLATE_HANDWRITING_BOXES_PER_QUESTION,
    TEMPLATE_CONFIDENCE_GATE_VERSION,
    TEMPLATE_ANCHOR_CONFIDENCE_THRESHOLD,
    TEMPLATE_HANDWRITING_CONFIDENCE_THRESHOLD,
    MAX_TEMPLATE_HANDWRITING_BOX_AREA,
    MAX_TOTAL_TEMPLATE_HANDWRITING_AREA,
    MAX_ANSWER_REGION_AREA,
    MAX_TOTAL_ANSWER_REGION_AREA,
    MAX_ANSWER_REGION_OVERLAP_RATIO,
    _QUESTION_TYPES,
    _is_int,
    _question_number,
    normalize_answer_regions,
    validate_drawing_schema_geometry,
    normalize_question_schema,
    _slot_geometry_metadata,
    _bind_slot_geometry,
    _has_text_slot_labels,
    _valid_drawing_coordinates,
    _validate_printed_solve_anchors,
    _normalize_template_confidence,
    _normalize_template_provenance,
    _template_item_is_human_confirmed,
    _template_confidence_is_trusted,
    template_confidence_diagnostics,
    validate_template_confidence_gate,
    _trusted_template_handwriting_regions,
    _validated_template_handwriting_regions,
    normalize_question_anchors,
    normalize_template_handwriting_regions,
)
from services.template_evidence_canonical import (
    CANONICALIZATION_VERSION,
    CANONICAL_COORDINATE_SYSTEM,
    CANONICAL_SOURCE_COORDINATE_SYSTEM,
    CANONICAL_PAGE_LONG_EDGE,
    CANONICAL_PAGE_MARGIN,
    _STORAGE_NAMESPACE_RE,
    _CANONICAL_CANDIDATE_KEYS,
    CanonicalTemplate,
    _LoadedTemplateSource,
    _flatten_to_rgb,
    _resize_long_edge,
    _validated_source_dimensions,
    _load_rgb_image,
    _load_template_source,
    _order_page_quad,
    _page_content_metrics,
    _full_frame_page_confidence,
    _full_frame_content_confidence,
    _approximate_quad,
    _detect_page_quad,
    _canonical_page,
    _sha256_file,
    _save_rgb_png_atomic,
    _copy_file_atomic,
    parse_canonical_metadata,
    accept_canonical_template_candidate,
    canonicalize_template_image,
    _safe_source_image_path,
    _validate_canonicalization,
)
from services.template_evidence_alignment import (
    LOCAL_TILES_X,
    LOCAL_MIN_INLIERS,
    LOCAL_MIN_INLIER_RATIO,
    LOCAL_OVERLAP_RATIO,
    LOCAL_MAX_TRANSLATION_RATIO,
    LOCAL_MAX_SCALE_CHANGE,
    LOCAL_MAX_ROTATION_DEGREES,
    LOCAL_MAX_SHEAR,
    _coverage_for_homography,
    _trusted_local_affine,
    _refine_local_alignment,
    _refine_source_mask,
)
from services.template_evidence_crops import (
    _answer_region_source_coverage,
    _answer_crop_boxes,
    _answer_crop_source_coverage,
    _frozen_column_bounds,
    _resolved_frozen_pixel_bounds,
    _pixel_regions,
    _horizontal_overlap,
    _same_answer_lane,
    _answer_crops,
)
from services.template_evidence_residue import (
    TEMPLATE_INK_LOCAL_CONTRAST,
    TEMPLATE_INK_MAX_GRAY,
    TEMPLATE_EDGE_DILATION_SIZE,
    TEMPLATE_ILLUMINATION_SIGMA,
    TEMPLATE_ILLUMINATION_MAX_OFFSET,
    MIN_NEW_STROKE_SUPPORT_PIXELS,
    MIN_NEW_STROKE_SUPPORT_RATIO,
    FILL_LINE_OPENING_WIDTH,
    FILL_LINE_VERTICAL_DILATION,
    FILL_LINE_SUPPORT_PAD_X,
    FILL_LINE_SUPPORT_PAD_Y,
    FILL_LINE_CROSSING_HORIZONTAL_PAD,
    FILL_LINE_MIN_SIDE_SUPPORT_PIXELS,
    TEMPLATE_HANDWRITING_MIN_AREA,
    TEMPLATE_HANDWRITING_RECOVERY_PAD,
    TEMPLATE_HANDWRITING_RECOVERY_MIN_SEED_PIXELS,
    TEMPLATE_HANDWRITING_RECOVERY_STRONG_SEED_RATIO,
    TEMPLATE_HANDWRITING_RECOVERY_MIN_AREA,
    TEMPLATE_HANDWRITING_RECOVERY_MIN_SEED_RATIO,
    TEMPLATE_HANDWRITING_RECOVERY_MAX_EXTENT,
    TEMPLATE_HANDWRITING_RECOVERY_MIN_ELONGATION,
    TEMPLATE_HANDWRITING_RECOVERY_MAX_PAGE_SPAN_RATIO,
    _template_handwriting_region_mask,
    _merge_template_exemption_regions,
    _erase_template_handwriting,
    _suppress_registered_template_residue_v2,
    _restore_fill_line_overlap_strokes,
    _local_ink_mask,
    _illumination_normalized_student_gray,
)
from services.template_evidence_pixels import (
    TEMPLATE_OVERLAP_DARKER_MARGIN,
    _answer_only_pixels_v2,
    _choice_answer_pixels,
    _new_ink_pixel_count,
    _confident_blank_questions,
)
from services.template_evidence_artifacts import (
    ARTIFACT_VERSION,
    PRINTED_SOLVE_LABEL_POLICY,
    build_template_artifact,
    _safe_artifact_path,
    _erase_template_handwriting_rgb,
    materialize_template_difference_artifact,
    _verified_template_manifest,
    verify_template_artifacts,
)
from services.template_evidence_sheets import (
    FOCUS_EVIDENCE_LABEL_WIDTH,
    FOCUS_EVIDENCE_PADDING,
    FOCUS_EVIDENCE_MAX_IMAGE_PIXELS,
    FOCUS_EVIDENCE_MAX_TOTAL_PIXELS,
    _machine_label,
    _build_evidence_table,
    _anchor_block_crops,
    build_focus_evidence_sheet_layout,
    build_multi_evidence_from_aligned,
)
from services.template_evidence_aligned import (
    build_evidence_from_aligned,
)
from services.template_evidence_labels import (
    EVIDENCE_Q_LABEL_COLOR,
    EVIDENCE_Q_LABEL_FONT_SIZE,
    EVIDENCE_Q_LABEL_PADDING_X,
    EVIDENCE_Q_LABEL_PADDING_Y,
    EVIDENCE_Q_LABEL_GAP,
    _evidence_question_label_font,
    _drawing_region_pixels,
    _render_column_question_labels,
    validate_drawing_label_geometry,
    _new_ink_extent,
    _unmarked_zones,
    _route_unmarked_answer_positions,
)


# Feature extraction is the memory-heavy part of local page registration.  Keep
# the stored template and the generated evidence at MAX_IMAGE_EDGE, but run
# SIFT on a smaller working image.  Coordinates are scaled back to the stored
# image space before estimating the homography.
FEATURE_MAX_EDGE = max(
    640,
    min(
        MAX_IMAGE_EDGE,
        int(os.environ.get("OCR_TEMPLATE_FEATURE_MAX_EDGE", "1280")),
    ),
)
FEATURE_NFEATURES = max(
    500,
    min(
        10000,
        int(os.environ.get("OCR_TEMPLATE_FEATURE_NFEATURES", "3000")),
    ),
)
MIN_GOOD_MATCHES = 25
MIN_INLIERS = 15
MIN_INLIER_RATIO = 0.20
MIN_PAGE_COVERAGE = 0.78
MAX_REPROJECTION_RMSE = 6.0
LOWE_RATIO = 0.75


_EVIDENCE_NAME_RE = re.compile(r"evidence-[0-9a-f]{32}\.png\Z")


@dataclass(frozen=True)
class _TemplateFeatures:
    descriptors: np.ndarray | None
    # Full-resolution coordinates corresponding to ``descriptors``.  SIFT is
    # run on a reduced image, so these points are explicitly mapped back to
    # the artifact coordinate space before RANSAC.
    points: np.ndarray
    shape: tuple[int, int]


def _feature_gray_and_scales(rgb: np.ndarray) -> tuple[np.ndarray, float, float]:
    """Return a bounded SIFT input and its x/y scale relative to ``rgb``."""

    height, width = rgb.shape[:2]
    longest = max(width, height)
    if longest <= FEATURE_MAX_EDGE:
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        return np.ascontiguousarray(gray), 1.0, 1.0

    scale = FEATURE_MAX_EDGE / float(longest)
    target_width = max(1, int(round(width * scale)))
    target_height = max(1, int(round(height * scale)))
    reduced = cv2.resize(
        rgb,
        (target_width, target_height),
        interpolation=cv2.INTER_AREA,
    )
    gray = cv2.cvtColor(reduced, cv2.COLOR_RGB2GRAY)
    return (
        np.ascontiguousarray(gray),
        target_width / float(width),
        target_height / float(height),
    )


def _keypoint_points(
    keypoints: tuple[Any, ...],
    scale_x: float,
    scale_y: float,
) -> np.ndarray:
    if not keypoints:
        return np.empty((0, 2), dtype=np.float32)
    points = np.asarray([keypoint.pt for keypoint in keypoints], dtype=np.float32)
    points[:, 0] /= float(scale_x)
    points[:, 1] /= float(scale_y)
    return np.ascontiguousarray(points)


def _candidate_public(candidate: dict[str, Any]) -> dict[str, Any]:
    reprojection_rmse = candidate.get("reprojection_rmse")
    return {
        "page_type_id": candidate["page_type_id"],
        "good_matches": candidate["good_matches"],
        "inliers": candidate["inliers"],
        "inlier_ratio": round(candidate["inlier_ratio"], 4),
        "reprojection_rmse": (
            round(reprojection_rmse, 3)
            if reprojection_rmse is not None
            else None
        ),
        "coverage": round(candidate["coverage"], 4),
        "qualified": candidate["qualified"],
    }


def _build_question_anchor_overlay(aligned: np.ndarray, anchors: list[dict]) -> np.ndarray:
    """Keep the aligned page unchanged; answer regions are the only overlays."""
    # Question-number geometry was removed from the active model contract.  The
    # blue answer-region overlays carry the question label instead.  Retain the
    # helper as a no-op for old callers and persisted artifacts.
    return aligned


@dataclass
class _FullColumnLayout:
    """A layout snapshot resolved for full-column difference evidence.

    Each question is labelled once per column it appears in: a number in
    ``solve_numbers`` at its printed question number, one in
    ``drawing_questions`` beside its drawing region, every number in
    ``region_question_numbers`` at its answer regions.
    """

    plan: dict[str, Any] | None
    question_numbers: list[str]
    anchors: list[dict]
    anchor_by_number: dict[str, dict]
    schema: list[dict]
    solve_numbers: set[str]
    legacy_solve_numbers: set[str]
    drawing_questions: dict[str, dict]
    handwriting: list[dict]
    answer_regions: list[dict]
    region_question_numbers: list[str]
    region_answer_regions: list[dict]
    reuse_question_anchor_labels: bool
    template_baseline_prewhitened: bool


@dataclass(frozen=True)
class _ColumnLabelSets:
    """Label positions as ``(question, [region,] column)`` keys."""

    printed: set[tuple]
    drawing: set[tuple]
    blank: set[tuple]


def _snapshot_page_type_id(layout_snapshot: dict[str, Any]) -> str:
    if not isinstance(layout_snapshot, dict):
        raise TemplateEvidenceError("layout snapshot is invalid")
    page_type_id = str(layout_snapshot.get("page_type_id") or "").strip()
    if not _PAGE_TYPE_RE.fullmatch(page_type_id):
        raise TemplateEvidenceError("layout snapshot page_type_id is invalid")
    return page_type_id


def _require_aligned_pixels(
    aligned_rgb: np.ndarray,
    source_mask: np.ndarray,
    template_rgb: np.ndarray,
) -> None:
    if (
        not isinstance(aligned_rgb, np.ndarray)
        or aligned_rgb.dtype != np.uint8
        or aligned_rgb.ndim != 3
        or aligned_rgb.shape[2] != 3
        or not isinstance(template_rgb, np.ndarray)
        or template_rgb.dtype != np.uint8
        or template_rgb.shape != aligned_rgb.shape
    ):
        raise TemplateEvidenceError("aligned or template image is invalid")
    if (
        not isinstance(source_mask, np.ndarray)
        or source_mask.dtype != np.uint8
        or source_mask.shape != aligned_rgb.shape[:2]
        or not set(int(value) for value in np.unique(source_mask).tolist()).issubset({0, 255})
    ):
        raise TemplateEvidenceError("aligned source mask is invalid")


def _layout_question_sequence(layout_snapshot: dict[str, Any]) -> list[str]:
    question_numbers = [
        str(value).strip()
        for value in (layout_snapshot.get("question_numbers") or [])
        if str(value).strip()
    ]
    if not question_numbers:
        question_numbers = [
            item["question_number"]
            for item in normalize_question_anchors(
                layout_snapshot.get("question_anchors") or []
            )
        ]
    if not question_numbers or len(set(question_numbers)) != len(question_numbers):
        raise TemplateEvidenceError("layout question sequence is invalid")
    return question_numbers


def _layout_question_kinds(
    layout_snapshot: dict[str, Any],
    question_numbers: list[str],
) -> tuple[list[dict], set[str], dict[str, dict]]:
    """``(schema, solve_numbers, drawing_questions)`` declared by the snapshot."""
    raw_schema = layout_snapshot.get("question_schema")
    schema = []
    solve_numbers: set[str] = set()
    drawing_questions: dict[str, dict] = {}
    if raw_schema is not None:
        schema = normalize_question_schema(raw_schema)
        if {item["question_number"] for item in schema} != set(question_numbers):
            raise TemplateEvidenceError("question schema does not match question sequence")
        # The schema normalizer has a historical solve default. Only an
        # explicitly declared type activates the new printed-number policy.
        solve_numbers = {
            item["question_number"] for item, raw in zip(schema, raw_schema, strict=True)
            if str(raw.get("question_type") or "").strip().lower() == "solve"
        }
        drawing_questions = {item["question_number"]: item for item in schema
                             if item.get("answer_kind") in {"drawing", "mixed"}}
        solve_numbers -= set(drawing_questions)
    return schema, solve_numbers, drawing_questions


def _full_column_layout(layout_snapshot: dict[str, Any]) -> _FullColumnLayout:
    """Validate the snapshot and decide where every question is labelled."""
    raw_plan = layout_snapshot.get("question_crop_plan")
    plan = (
        validate_anchor_crop_plan(raw_plan)
        if raw_plan is not None
        else None
    )
    question_numbers = _layout_question_sequence(layout_snapshot)

    raw_anchors = layout_snapshot.get("question_anchors") or []
    anchors = normalize_question_anchors(raw_anchors) if raw_anchors else []
    anchor_by_number = {item["question_number"]: item for item in anchors}
    if anchors and set(anchor_by_number) != set(question_numbers):
        raise TemplateEvidenceError("question anchors do not match question sequence")
    schema, solve_numbers, drawing_questions = _layout_question_kinds(
        layout_snapshot, question_numbers,
    )
    # Only the verified-artifact reader may select this compatibility path.
    # Direct helper callers and newly frozen artifacts retain the strict
    # explicit-solve contract; an old schema's default solve is not provenance.
    legacy_solve_numbers = set()
    if layout_snapshot.get("_legacy_solve_label_fallback") is True:
        legacy_solve_numbers = {
            number for number in solve_numbers
            if not anchor_by_number.get(number, {}).get("question_number_bbox")
        }
        solve_numbers -= legacy_solve_numbers
    raw_handwriting = layout_snapshot.get("template_handwriting_regions")
    handwriting = (
        normalize_template_handwriting_regions(raw_handwriting)
        if raw_handwriting is not None
        else []
    )
    raw_answer_regions = layout_snapshot.get("template_answer_regions")
    answer_regions = (
        normalize_template_handwriting_regions(raw_answer_regions)
        if raw_answer_regions is not None
        else []
    )
    answer_regions = _bind_slot_geometry(schema, answer_regions)
    solve_numbers -= {item["question_number"] for item in answer_regions if item["regions"]}
    for number in solve_numbers:
        if not anchor_by_number.get(number, {}).get("question_number_bbox"):
            raise TemplateEvidenceError(f"第 {number} 题缺少作答框或印刷题号位置，请教师补充")
    text_slot_numbers = {item["question_number"] for item in answer_regions if _has_text_slot_labels(item, drawing_questions)}
    region_question_numbers = [number for number in question_numbers
                               if number not in solve_numbers and (number not in drawing_questions or number in text_slot_numbers)]
    region_answer_regions = [
        item for item in answer_regions
        if item["question_number"] in region_question_numbers
    ]
    reuse_question_anchor_labels = (
        bool(answer_regions) or
        str(layout_snapshot.get("template_mode") or "").strip().lower() == "blank"
    )
    if reuse_question_anchor_labels and (
        {item["question_number"] for item in region_answer_regions} != set(region_question_numbers)
        or any(not item["regions"] for item in region_answer_regions)
    ):
        raise TemplateEvidenceError("模板作答框未覆盖全部题号，请教师补充或复核作答框")
    template_baseline_prewhitened = layout_snapshot.get(
        "template_baseline_prewhitened", False
    )
    if not isinstance(template_baseline_prewhitened, bool):
        raise TemplateEvidenceError(
            "layout snapshot template_baseline_prewhitened is invalid"
        )
    return _FullColumnLayout(
        plan=plan,
        question_numbers=question_numbers,
        anchors=anchors,
        anchor_by_number=anchor_by_number,
        schema=schema,
        solve_numbers=solve_numbers,
        legacy_solve_numbers=legacy_solve_numbers,
        drawing_questions=drawing_questions,
        handwriting=handwriting,
        answer_regions=answer_regions,
        region_question_numbers=region_question_numbers,
        region_answer_regions=region_answer_regions,
        reuse_question_anchor_labels=reuse_question_anchor_labels,
        template_baseline_prewhitened=template_baseline_prewhitened,
    )


def _full_column_answer_pixels(
    aligned_rgb: np.ndarray,
    template_rgb: np.ndarray,
    source_mask: np.ndarray,
    layout: _FullColumnLayout,
) -> tuple[np.ndarray, list[dict]]:
    """Difference pixels with drawing regions restored to the student's original."""
    merged_exemptions = _merge_template_exemption_regions(
        layout.answer_regions,
        layout.handwriting,
    )
    answer_only = _answer_only_pixels_v2(
        aligned_rgb,
        template_rgb,
        source_mask,
        answer_regions=None,
        template_handwriting_regions=merged_exemptions,
    )
    height, width = aligned_rgb.shape[:2]
    drawing_context = []
    for number, question in layout.drawing_questions.items():
        for box in question["drawing_regions"]:
            x1, y1, x2, y2 = _drawing_region_pixels(box, width, height)
            if not bool(np.all(source_mask[y1:y2, x1:x2] == 255)):
                raise TemplateEvidenceError(f"第 {number} 题作图区域照片不完整，请教师复核")
            # Preserve actual aligned STUDENT pixels including grid/axes and
            # printed references; do not reconstruct a graph from model text.
            answer_only[y1:y2, x1:x2] = aligned_rgb[y1:y2, x1:x2]
            drawing_context.append({"question_number": number, "region_bbox_1000": list(box),
                                    "pixel_box_exclusive": [x1, y1, x2, y2]})
    return answer_only, drawing_context


def _route_layout_answer_positions(
    layout: _FullColumnLayout,
    aligned_rgb: np.ndarray,
    template_rgb: np.ndarray,
    columns: list[tuple[int, int]],
) -> tuple[list, list]:
    """Move the label target of unmarked answer positions; returns the audit records."""
    if not any(item.get("answer_position") == "unmarked" for item in layout.schema):
        return [], []
    # Only the label target moves; the frozen boxes stay the exemptions.
    layout.answer_regions, answer_position_records, answer_position_review = _route_unmarked_answer_positions(
        layout.schema, layout.answer_regions, layout.handwriting, layout.anchor_by_number,
        layout.drawing_questions, aligned_rgb, template_rgb, columns)
    layout.region_answer_regions = [item for item in layout.answer_regions
                                    if item["question_number"] in layout.region_question_numbers]
    return answer_position_records, answer_position_review


def _expected_column_labels(
    layout: _FullColumnLayout,
    columns: list[tuple[int, int]],
    width: int,
) -> _ColumnLabelSets:
    """Where each label must land; a question outside every column is refused."""
    anchor_by_number = layout.anchor_by_number
    drawing_questions = layout.drawing_questions
    expected_printed_labels = {
        (number, column_index)
        for number in layout.solve_numbers
        for column_index, (left, right) in enumerate(columns, 1)
        if left <= (
            anchor_by_number[number]["question_number_bbox"][0]
            + anchor_by_number[number]["question_number_bbox"][2]
        ) * width / 2000 < right
    }
    expected_drawing_labels = {
        (number, column_index)
        for number, question in drawing_questions.items()
        for column_index, (left, right) in enumerate(columns, 1)
        if left <= question["label_anchor"][0] * width / 1000 < right
    }
    if len(expected_drawing_labels) != len(drawing_questions):
        raise TemplateEvidenceError("作图题标签未落在有效分栏内，请教师复核定位")
    if ({number for number, _ in expected_printed_labels} != layout.solve_numbers
            or len(expected_printed_labels) != len(layout.solve_numbers)):
        raise TemplateEvidenceError("简答题印刷题号未落在有效分栏内")
    expected_blank_labels = {
        (item["question_number"], region_index, column_index)
        for item in layout.region_answer_regions
        for region_index, box in enumerate(item["regions"])
        if item["question_number"] not in drawing_questions or item["region_kinds"][region_index] == "answer"
        for column_index, (left, right) in enumerate(columns, 1)
        if max(left, round(box[0] * width / 1000))
        < min(right, round(box[2] * width / 1000))
    } if layout.reuse_question_anchor_labels else set()
    if layout.reuse_question_anchor_labels and (
        {(number, region_index) for number, region_index, _ in expected_blank_labels}
        != {(item["question_number"], region_index)
            for item in layout.region_answer_regions for region_index in range(len(item["regions"]))
            if item["question_number"] not in drawing_questions or item["region_kinds"][region_index] == "answer"}
    ):
        raise TemplateEvidenceError("模板作答框未落在有效分栏内，请教师复核")
    return _ColumnLabelSets(
        printed=expected_printed_labels,
        drawing=expected_drawing_labels,
        blank=expected_blank_labels,
    )


_FROZEN_COLUMN_EVIDENCE_KEYS = (
    "column_id",
    "column_order",
    "layout_revision",
    "crop_left_px",
    "crop_right_px",
    "actual_pixel_endpoints",
    "crop_endpoint_version",
    "source_width",
    "source_height",
    "image_sha256",
)


def _render_full_columns(
    answer_only: np.ndarray,
    layout: _FullColumnLayout,
    columns: list[tuple[int, int]],
    output_root: Path,
    width: int,
    paths: list[Path],
) -> list[dict[str, Any]]:
    """Save one labelled evidence PNG per column; returns the column metadata.

    Every saved file is appended to ``paths`` at once, so the caller can remove
    all of them when a later column or the label verification fails.
    """
    from services.evidence_label_layout import source_patch_pnginfo

    column_metadata: list[dict[str, Any]] = []
    for column_index, (left, right) in enumerate(columns, 1):
        crop = np.ascontiguousarray(answer_only[:, left:right])
        canvas = Image.fromarray(crop, mode="RGB")
        canvas, label_placements, label_layout = _render_column_question_labels(
            crop, question_numbers=layout.question_numbers,
            region_question_numbers=layout.region_question_numbers,
            answer_regions=layout.answer_regions, handwriting=layout.handwriting,
            anchor_by_number=layout.anchor_by_number, solve_numbers=layout.solve_numbers,
            drawing_questions=layout.drawing_questions, column_left=left, page_width=width,
            reuse_question_anchor_labels=layout.reuse_question_anchor_labels)
        visible_labels = [p["question_number"] for p in label_placements]
        # Separator strokes used to repaint ink at region bottoms. Ownership
        # now uses foreground-safe leaders, not rules across answer pixels.
        separator_lines = []
        visible_labels = [number for number in layout.question_numbers if number in visible_labels]
        path = output_root / f"evidence-{uuid.uuid4().hex}.png"
        pnginfo = source_patch_pnginfo(crop, label_placements, label_layout)
        canvas.save(path, format="PNG", pnginfo=pnginfo)
        paths.append(path.resolve())
        column_metadata.append({
            "column_index": column_index,
            "sheet_id": f"column_{column_index:02d}",
            "width": int(canvas.width),
            "height": int(canvas.height),
            "question_numbers": visible_labels,
            "label_placements": label_placements,
            "separator_lines": separator_lines,
            "label_layout": label_layout,
            "crop_left_px": left,
            "crop_right_px": right,
        })
        if layout.plan is not None:
            raw_column = layout.plan["parameters"]["columns"][column_index - 1]
            for key in _FROZEN_COLUMN_EVIDENCE_KEYS:
                if key in raw_column:
                    column_metadata[-1][key] = raw_column[key]
        del canvas, crop
    return column_metadata


def _verify_rendered_labels(
    column_metadata: list[dict[str, Any]],
    expected: _ColumnLabelSets,
    layout: _FullColumnLayout,
) -> tuple[list[tuple], list[tuple]]:
    """Every expected label was placed exactly once; returns (printed, drawing)."""
    if layout.reuse_question_anchor_labels:
        actual_blank_labels = {
            (placement["question_number"], placement["region_index"], column["column_index"])
            for column in column_metadata for placement in column["label_placements"]
            if placement["source"] not in {"printed_question_number", "drawing_region_left"}
        }
        if actual_blank_labels != expected.blank:
            raise TemplateEvidenceError("模板题号标记不完整，请教师复核作答框")
    actual_printed_labels = [
        (placement["question_number"], column["column_index"])
        for column in column_metadata for placement in column["label_placements"]
        if placement["source"] == "printed_question_number"
    ]
    if (set(actual_printed_labels) != expected.printed
            or len(actual_printed_labels) != len(expected.printed)):
        raise TemplateEvidenceError("简答题印刷题号标记不完整或重复")
    actual_drawing_labels = [(placement["question_number"], column["column_index"])
                             for column in column_metadata for placement in column["label_placements"]
                             if placement["source"] == "drawing_region_left"]
    if (set(actual_drawing_labels) != expected.drawing
            or len(actual_drawing_labels) != len(expected.drawing)):
        raise TemplateEvidenceError("作图题标签标记不完整或重复，请教师复核定位")
    return actual_printed_labels, actual_drawing_labels


_FROZEN_COLUMN_DIAGNOSTIC_KEYS = (
    "column_index",
    "column_id",
    "column_order",
    "layout_revision",
    "x1_1000",
    "x2_1000",
    "crop_left_px",
    "crop_right_px",
    "actual_pixel_endpoints",
    "source_width",
    "source_height",
    "image_sha256",
)


def _full_column_diagnostics(
    diagnostics: dict[str, Any] | None,
    *,
    page_type_id: str,
    layout: _FullColumnLayout,
    width: int,
    height: int,
    column_metadata: list[dict[str, Any]],
    expected: _ColumnLabelSets,
    actual_printed_labels: list[tuple],
    actual_drawing_labels: list[tuple],
    drawing_context: list[dict],
    answer_position_records: list,
    answer_position_review: list,
) -> dict[str, Any]:
    """The evidence contract the grader and the artifact verifier read back."""
    from services.evidence_label_layout import ATTACHED_SLOT_MODES, SLOT_LABEL_STYLE

    drawing_questions = layout.drawing_questions
    result_diagnostics = dict(diagnostics or {})
    if answer_position_records:
        result_diagnostics["answer_position_routing"] = answer_position_records
        result_diagnostics["answer_position_review"] = answer_position_review
    if layout.legacy_solve_numbers:
        result_diagnostics["legacy_solve_label_policy"] = {
            "version": "pre-printed-solve-artifact-v1",
            "question_numbers": [number for number in layout.question_numbers if number in layout.legacy_solve_numbers],
            "reason": "persisted_artifact_without_printed_solve_policy_or_number_box",
        }
    contract_placements = [
        placement for column in column_metadata for placement in column["label_placements"]
    ]
    foreground_preserved = all(column["label_layout"]["foreground_preserved"] for column in column_metadata)
    attached_slots = any(p["placement_mode"] in ATTACHED_SLOT_MODES for p in contract_placements)
    foreground_outside_translucent = all(
        column["label_layout"]["foreground_preserved_outside_translucent_labels"] for column in column_metadata)
    result_diagnostics.update({
        "selected_page_type_id": page_type_id,
        "template_width": width,
        "template_height": height,
        "evidence_width": sum(item["width"] for item in column_metadata),
        "evidence_height": max(item["height"] for item in column_metadata),
        "ownership": "full_column_difference",
        "difference_algorithm": "difference-v2-cleaner",
        "evidence_columns": column_metadata,
        "question_anchor_count": len(layout.anchors),
        "template_handwriting_exemptions": sum(
            len(item["regions"]) for item in layout.handwriting
        ),
        "template_answer_regions": sum(
            len(item["regions"]) for item in layout.answer_regions
        ),
        "template_text_removed": not bool(drawing_questions),
        "template_baseline_prewhitened": layout.template_baseline_prewhitened,
        "answer_source_complete": True,
        # Only the generated blue ID has a hollow box; no red printed-number
        # boxes or colored answer-region frames are added.
        "qwen37_label_contract": {
            "version": "qwen37-blue-box-adaptive-slot-labels-v7" if attached_slots else "qwen37-blue-box-labels-v6",
            "label_style": SLOT_LABEL_STYLE,
            "slot_orientation_policy": "horizontal-unless-preceding-region-collision" if attached_slots else None,
            "label_boxes": True, "label_box_color": "blue", "label_background_opacity": 0,
            "foreground_preserved": foreground_preserved,
            "foreground_preserved_outside_labels": True,
            "foreground_preserved_outside_translucent_labels": foreground_outside_translucent,
            "source_pixels_recoverable": all(
                column["label_layout"]["source_pixels_recoverable"] for column in column_metadata),
            "question_number_boxes": False,
            "answer_region_overlays": False,
            "labels_left_of_regions": all(
                placement.get("label_left_of_region", False)
                for placement in contract_placements
            ),
            "labels_fully_visible": all(
                placement.get("label_fully_visible", False)
                for placement in contract_placements
            ),
        },
    })
    if layout.reuse_question_anchor_labels:
        region_placements = [
            placement for placement in contract_placements
            if placement["source"] not in {"printed_question_number", "drawing_region_left"}
        ]
        result_diagnostics["blank_label_safety"] = {
            "version": "blank-label-foreground-safe-v1",
            "expected_region_labels": len(expected.blank),
            "placed_region_labels": len(region_placements),
            "all_regions_labeled": bool(contract_placements)
            and len(region_placements) == len(expected.blank),
            "foreground_preserved": foreground_preserved,
            "foreground_preserved_outside_fixed_solve_labels": foreground_preserved,
            "foreground_preserved_outside_translucent_labels": foreground_outside_translucent,
            "foreground_preserved_outside_labels": True,
        }
        if drawing_questions:
            result_diagnostics["blank_label_safety"]["foreground_preserved_outside_fixed_labels"] = foreground_preserved
    if layout.solve_numbers:
        result_diagnostics["printed_solve_label_policy"] = {
            "version": "printed-number-safe-label-v2",
            "expected_labels": len(expected.printed),
            "placed_labels": len(actual_printed_labels),
            "fixed_position_overwrite_allowed": False,
            "coordinate_mapping": "evidence_columns.label_layout",
        }
    if drawing_questions:
        result_diagnostics["drawing_label_policy"] = {
            "version": "drawing-non-occluding-label-v2",
            "expected_labels": len(expected.drawing), "placed_labels": len(actual_drawing_labels),
            "coordinate_mapping": "evidence_columns.label_layout",
            "placement_offsets": [p["placement_offset_px"] for p in contract_placements
                                  if p["source"] == "drawing_region_left"],
            "retained_context": drawing_context,
            "context_pixel_source": "global_aligned_student_original_pixels",
            "printed_graph_references_retained": True,
            "student_graph_reconstructed": False,
        }
    if layout.plan is not None:
        result_diagnostics["layout_context"] = dict(layout.plan.get("layout_context") or {})
        result_diagnostics["frozen_columns"] = [
            {key: column[key] for key in _FROZEN_COLUMN_DIAGNOSTIC_KEYS if key in column}
            for column in layout.plan["parameters"]["columns"]
        ]
    return result_diagnostics


def build_full_column_difference_evidence_from_aligned(
    aligned_rgb: np.ndarray,
    source_mask: np.ndarray,
    template_rgb: np.ndarray,
    layout_snapshot: dict[str, Any],
    output_dir: str | os.PathLike[str],
    *,
    diagnostics: dict[str, Any] | None = None,
) -> EvidenceResult:
    """Render full-height column differences with local question ownership.

    Ownership anchors remain in template space. Ordinary labels use safe
    whitespace or an external rail. Attached horizontal-slot labels draw only
    blue glyphs, translucent over preceding boxes, with raw pixels saved for replay.
    The source offset is exported for geometry consumers; exemptions do not move.
    """
    page_type_id = _snapshot_page_type_id(layout_snapshot)
    _require_aligned_pixels(aligned_rgb, source_mask, template_rgb)
    layout = _full_column_layout(layout_snapshot)
    answer_only, drawing_context = _full_column_answer_pixels(
        aligned_rgb, template_rgb, source_mask, layout,
    )
    height, width = aligned_rgb.shape[:2]
    columns = (
        _frozen_column_bounds(layout.plan, width, height)
        if layout.plan is not None
        else [(0, width)]
    )
    answer_position_records, answer_position_review = _route_layout_answer_positions(
        layout, aligned_rgb, template_rgb, columns,
    )
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    expected = _expected_column_labels(layout, columns, width)

    paths: list[Path] = []
    try:
        column_metadata = _render_full_columns(
            answer_only, layout, columns, output_root, width, paths,
        )
        actual_printed_labels, actual_drawing_labels = _verify_rendered_labels(
            column_metadata, expected, layout,
        )
    except Exception:
        for path in paths:
            path.unlink(missing_ok=True)
        raise

    result_diagnostics = _full_column_diagnostics(
        diagnostics,
        page_type_id=page_type_id,
        layout=layout,
        width=width,
        height=height,
        column_metadata=column_metadata,
        expected=expected,
        actual_printed_labels=actual_printed_labels,
        actual_drawing_labels=actual_drawing_labels,
        drawing_context=drawing_context,
        answer_position_records=answer_position_records,
        answer_position_review=answer_position_review,
    )
    return EvidenceResult(
        page_type_id=page_type_id,
        evidence_path=str(paths[0]) if paths else "",
        evidence_paths=tuple(str(path) for path in paths),
        question_numbers=layout.question_numbers,
        blank_question_numbers=[],
        diagnostics=result_diagnostics,
    )


class TemplateEvidenceMatcher:
    """Match a student page to one verified template and build answer evidence."""

    def __init__(self, artifacts, template_dir):
        cv2.setNumThreads(1)
        if not hasattr(cv2, "SIFT_create"):
            raise RuntimeError("OpenCV SIFT support is required")
        self.template_dir = os.path.realpath(os.fspath(template_dir))
        self.artifacts = verify_template_artifacts(template_dir, artifacts)
        # The key set is bounded by the verified artifact mapping and each SIFT
        # result is bounded by FEATURE_NFEATURES. Full RGB pages are deliberately
        # excluded so a many-page template cannot retain every decoded image.
        self._feature_cache: dict[str, _TemplateFeatures] = {}
        self._feature_lock = threading.RLock()

    def _load_verified_template_rgb(self, page_type_id: str) -> np.ndarray:
        artifact = self.artifacts[page_type_id]
        path, _relative = _safe_artifact_path(
            self.template_dir, artifact["template_image"]
        )
        if _sha256_file(path) != artifact["sha256"]:
            raise TemplateEvidenceError("template artifact changed after verification")
        image = _load_rgb_image(path)
        height, width = image.shape[:2]
        if (width, height) != (artifact["width"], artifact["height"]):
            raise TemplateEvidenceError("template artifact dimensions changed")
        return image

    def _load_verified_difference_template_rgb(self, page_type_id: str) -> np.ndarray:
        """Load the hash-bound baseline with approved boxes painted white."""
        artifact = self.artifacts[page_type_id]
        relative = artifact.get("difference_template_image")
        if not relative:
            # Blank templates and legacy artifacts have no separate whiteout
            # image; their original registration image is the correct baseline.
            return self._load_verified_template_rgb(page_type_id)
        path, _relative = _safe_artifact_path(self.template_dir, relative)
        expected_hash = artifact.get("difference_template_sha256")
        if not isinstance(expected_hash, str) or _sha256_file(path) != expected_hash:
            raise TemplateEvidenceError(
                "difference template changed after verification"
            )
        image = _load_rgb_image(path)
        if image.shape[:2] != (artifact["height"], artifact["width"]):
            raise TemplateEvidenceError("difference template dimensions changed")
        return image

    def _template_features(self, page_type_id: str) -> _TemplateFeatures:
        with self._feature_lock:
            cached = self._feature_cache.get(page_type_id)
            if cached is not None:
                return cached
            image = self._load_verified_template_rgb(page_type_id)
            height, width = image.shape[:2]
            gray, scale_x, scale_y = _feature_gray_and_scales(image)
            sift = cv2.SIFT_create(nfeatures=FEATURE_NFEATURES)
            keypoints, descriptors = sift.detectAndCompute(gray, None)
            keypoints = tuple(keypoints or ())
            cached = _TemplateFeatures(
                descriptors,
                _keypoint_points(keypoints, scale_x, scale_y),
                (height, width),
            )
            self._feature_cache[page_type_id] = cached
            return cached

    def _match_candidate(
        self,
        page_type_id: str,
        student_keypoints,
        student_descriptors,
        student_points: np.ndarray | None = None,
        student_shape: tuple[int, int] | None = None,
    ) -> dict[str, Any]:
        template = self._template_features(page_type_id)
        candidate: dict[str, Any] = {
            "page_type_id": page_type_id,
            "good_matches": 0,
            "inliers": 0,
            "inlier_ratio": 0.0,
            "reprojection_rmse": None,
            "coverage": 0.0,
            "qualified": False,
            "homography": None,
            "source_inliers": None,
            "target_inliers": None,
        }
        if (
            student_descriptors is None
            or template.descriptors is None
            or len(student_descriptors) < 2
            or len(template.descriptors) < 2
        ):
            return candidate

        matcher = cv2.BFMatcher(cv2.NORM_L2, crossCheck=False)
        try:
            pairs = matcher.knnMatch(student_descriptors, template.descriptors, k=2)
        except cv2.error:
            return candidate
        good = [
            pair[0] for pair in pairs
            if len(pair) == 2 and pair[0].distance < LOWE_RATIO * pair[1].distance
        ]
        candidate["good_matches"] = len(good)
        if len(good) < MIN_GOOD_MATCHES:
            return candidate

        if student_points is None:
            student_points = np.asarray(
                [keypoint.pt for keypoint in student_keypoints],
                dtype=np.float32,
            )
        source_points = np.float32(
            [student_points[match.queryIdx] for match in good]
        ).reshape(-1, 1, 2)
        target_points = np.float32(
            [template.points[match.trainIdx] for match in good]
        ).reshape(-1, 1, 2)
        try:
            homography, mask = cv2.findHomography(
                source_points, target_points, cv2.RANSAC, 5.0
            )
        except cv2.error:
            return candidate
        if homography is None or mask is None or not np.isfinite(homography).all():
            return candidate

        inlier_mask = mask.ravel().astype(bool)
        inliers = int(np.count_nonzero(inlier_mask))
        inlier_ratio = inliers / len(good)
        candidate["inliers"] = inliers
        candidate["inlier_ratio"] = inlier_ratio
        if inliers == 0:
            return candidate
        source_inliers = source_points.reshape(-1, 2)[inlier_mask]
        target_inliers = target_points.reshape(-1, 2)[inlier_mask]
        projected = cv2.perspectiveTransform(
            source_inliers.reshape(-1, 1, 2), homography
        ).reshape(-1, 2)
        reprojection_rmse = float(np.sqrt(np.mean(np.sum(
            (projected - target_inliers) ** 2,
            axis=1,
        ))))
        source_shape = student_shape or template.shape
        coverage = _coverage_for_homography(
            source_shape,
            template.shape,
            homography,
        )
        candidate.update({
            "inliers": inliers,
            "inlier_ratio": inlier_ratio,
            "reprojection_rmse": reprojection_rmse,
            "coverage": coverage,
            "qualified": (
                inliers >= MIN_INLIERS
                and inlier_ratio >= MIN_INLIER_RATIO
                and reprojection_rmse <= MAX_REPROJECTION_RMSE
                and coverage >= MIN_PAGE_COVERAGE
            ),
            "homography": homography,
            "source_inliers": source_inliers,
            "target_inliers": target_inliers,
        })
        return candidate

    @staticmethod
    def _has_clear_winner(top: dict[str, Any], second: dict[str, Any]) -> bool:
        inlier_gap = top["inliers"] - second["inliers"]
        required_gap = max(8, int(math.ceil(second["inliers"] * 0.20)))
        if inlier_gap >= required_gap:
            return True
        return inlier_gap >= 5 and (
            top["inlier_ratio"] - second["inlier_ratio"] >= 0.15
        )

    def match_and_build(
        self,
        student_path,
        output_dir,
        *,
        include_evidence: bool = True,
        include_source_mask: bool = False,
    ) -> EvidenceResult:
        if not isinstance(include_evidence, bool):
            raise ValueError("include_evidence must be a boolean")
        if not isinstance(include_source_mask, bool):
            raise ValueError("include_source_mask must be a boolean")
        student_rgb = _load_rgb_image(student_path)
        student_height, student_width = student_rgb.shape[:2]
        student_gray, scale_x, scale_y = _feature_gray_and_scales(student_rgb)
        sift = cv2.SIFT_create(nfeatures=FEATURE_NFEATURES)
        student_keypoints, student_descriptors = sift.detectAndCompute(
            student_gray, None
        )
        student_keypoints = tuple(student_keypoints or ())
        student_points = _keypoint_points(student_keypoints, scale_x, scale_y)

        candidates = [
            self._match_candidate(
                page_type_id,
                student_keypoints,
                student_descriptors,
                student_points,
                student_rgb.shape[:2],
            )
            for page_type_id in self.artifacts
        ]
        public_candidates = [_candidate_public(value) for value in candidates]
        diagnostics: dict[str, Any] = {
            "student_width": student_width,
            "student_height": student_height,
            "student_keypoints": len(student_keypoints),
            "feature_max_edge": FEATURE_MAX_EDGE,
            "feature_nfeatures": FEATURE_NFEATURES,
            "candidates": public_candidates,
        }
        qualified = [value for value in candidates if value["qualified"]]
        qualified.sort(
            key=lambda value: (
                value["inliers"],
                value["inlier_ratio"],
                value["coverage"],
                -value["reprojection_rmse"],
                value["good_matches"],
            ),
            reverse=True,
        )
        if not qualified:
            raise TemplateEvidenceError("student page did not match a template", diagnostics)
        winner = qualified[0]
        if len(qualified) > 1 and not self._has_clear_winner(winner, qualified[1]):
            diagnostics["ambiguous_page_types"] = [
                winner["page_type_id"], qualified[1]["page_type_id"]
            ]
            raise TemplateEvidenceError("student page matched multiple templates", diagnostics)

        page_type_id = winner["page_type_id"]
        artifact = self.artifacts[page_type_id]
        globally_aligned = cv2.warpPerspective(
            student_rgb,
            winner["homography"],
            (artifact["width"], artifact["height"]),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(255, 255, 255),
        )
        aligned, local_alignment, local_corrections = _refine_local_alignment(
            globally_aligned,
            winner["homography"],
            winner["source_inliers"],
            winner["target_inliers"],
            return_corrections=True,
        )
        source_mask = np.full(student_rgb.shape[:2], 255, dtype=np.uint8)
        globally_aligned_mask = cv2.warpPerspective(
            source_mask,
            winner["homography"],
            (artifact["width"], artifact["height"]),
            flags=cv2.INTER_NEAREST,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        aligned_source_mask = _refine_source_mask(
            globally_aligned_mask,
            local_corrections,
        )
        question_crop_plan = artifact.get("question_crop_plan")
        question_anchors = artifact.get("question_anchors") or []
        question_numbers = list(
            artifact.get("question_numbers")
            or [item["question_number"] for item in artifact.get("question_schema") or []]
        )
        template_handwriting_regions = artifact.get(
            "template_handwriting_regions"
        ) or []
        template_answer_regions = artifact.get(
            "template_answer_regions"
        ) or []
        blank_detection_enabled = artifact["blank_detection_enabled"]
        anchor_overlay = _build_question_anchor_overlay(aligned, question_anchors)
        difference_template = self._load_verified_difference_template_rgb(
            page_type_id
        )
        evidence_result = (
            build_full_column_difference_evidence_from_aligned(
                aligned,
                aligned_source_mask,
                difference_template,
                {
                    "page_type_id": page_type_id,
                    "question_numbers": question_numbers,
                    **({"question_schema": artifact["question_schema"]}
                       if artifact.get("question_schema") else {}),
                    "_legacy_solve_label_fallback": "question_label_policy" not in artifact,
                    "question_label_policy": artifact.get("question_label_policy"),
                    "question_crop_plan": question_crop_plan,
                    "question_anchors": question_anchors,
                    "template_mode": artifact.get("template_mode"),
                    "template_answer_regions": template_answer_regions,
                    "template_handwriting_regions": template_handwriting_regions,
                    "template_baseline_prewhitened": bool(
                        artifact.get("difference_template_image")
                    ),
                },
                output_dir,
            )
            if include_evidence
            else None
        )
        evidence_paths = (
            tuple(evidence_result.evidence_paths)
            if evidence_result is not None
            else ()
        )

        output_root = Path(output_dir)
        output_root.mkdir(parents=True, exist_ok=True)
        aligned_path = output_root / f"aligned-{uuid.uuid4().hex}.png"
        anchor_overlay_path = (
            output_root / f"question-anchors-{uuid.uuid4().hex}.png"
            if question_anchors
            else None
        )
        try:
            Image.fromarray(aligned, mode="RGB").save(aligned_path, format="PNG")
            if anchor_overlay_path is not None:
                Image.fromarray(anchor_overlay, mode="RGB").save(
                    anchor_overlay_path,
                    format="PNG",
                )
        except Exception:
            for partial in (
                aligned_path,
                *(Path(path) for path in evidence_paths),
                anchor_overlay_path,
            ):
                if partial is None:
                    continue
                try:
                    partial.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

        diagnostics.update({
            "selected_page_type_id": page_type_id,
            "aligned_image_path": str(aligned_path.resolve()),
            "raw_pixel_to_template_pixel": winner["homography"].tolist(),
            "template_pixel_to_raw_pixel": np.linalg.inv(winner["homography"]).tolist(),
            "template_width": artifact["width"],
            "template_height": artifact["height"],
            "evidence_width": (
                int(evidence_result.diagnostics.get("evidence_width") or 0)
                if evidence_result is not None else 0
            ),
            "evidence_height": (
                int(evidence_result.diagnostics.get("evidence_height") or 0)
                if evidence_result is not None else 0
            ),
            "ownership": (
                evidence_result.diagnostics.get("ownership")
                if evidence_result is not None
                else "full_column_difference"
            ),
            "local_alignment": local_alignment,
            "blank_detection_enabled": blank_detection_enabled,
            "new_ink_pixels": {},
            "program_blank_count": 0,
            "answer_source_complete": True,
            "question_anchor_count": len(question_anchors),
            "question_anchor_overlay_path": (
                str(anchor_overlay_path.resolve())
                if anchor_overlay_path is not None
                else ""
            ),
        })
        if evidence_result is not None:
            # Keep the actual rendered label positions auditable at the
            # production matcher boundary, not just in direct helper tests.
            for key in ("evidence_columns", "qwen37_label_contract",
                        "blank_label_safety", "printed_solve_label_policy", "legacy_solve_label_policy",
                        "drawing_label_policy", "answer_position_routing", "answer_position_review"):
                if key in evidence_result.diagnostics:
                    diagnostics[key] = evidence_result.diagnostics[key]
        if question_crop_plan is not None:
            diagnostics["question_crop_plan"] = {
                "strategy": question_crop_plan["strategy"],
                "block_count": len(question_crop_plan["blocks"]),
                "anchor_count": len(question_crop_plan["anchors"]),
                "vertical_gaps": question_crop_plan["coverage"]["vertical_gaps"],
                "provider_bottom_ignored": question_crop_plan["provider_bottom_ignored"],
                "requires_question_number_gate": question_crop_plan[
                    "requires_question_number_gate"
                ],
                "missing_expected_question_numbers": question_crop_plan[
                    "hint_coverage"
                ]["missing_expected_question_numbers"],
            }
        return EvidenceResult(
            page_type_id=page_type_id,
            evidence_path=(
                evidence_paths[0] if evidence_paths else ""
            ),
            evidence_paths=evidence_paths,
            question_numbers=question_numbers,
            blank_question_numbers=[],
            diagnostics=diagnostics,
            aligned_path=str(aligned_path.resolve()),
            aligned_source_mask=aligned_source_mask if include_source_mask else None,
        )


def cleanup_evidence(path) -> None:
    """Remove an evidence image created by ``match_and_build`` (idempotently)."""

    if path is None or str(path).strip() == "":
        return
    evidence_path = Path(path)
    if not _EVIDENCE_NAME_RE.fullmatch(evidence_path.name):
        raise ValueError("refusing to remove a non-evidence path")
    try:
        evidence_path.unlink(missing_ok=True)
    except IsADirectoryError as exc:
        raise ValueError("evidence path is not a file") from exc
