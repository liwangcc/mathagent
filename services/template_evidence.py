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


# qwen3.8-flash receives the difference image, not the teacher overlay. Keep
# the ownership marker small and blue. Horizontal slot labels alone may blend
# at 65% opacity when they overlap a preceding answer box; never draw frames.
EVIDENCE_Q_LABEL_COLOR = (20, 84, 178)
EVIDENCE_Q_LABEL_FONT_SIZE = 30
EVIDENCE_Q_LABEL_PADDING_X = 8
EVIDENCE_Q_LABEL_PADDING_Y = 8
EVIDENCE_Q_LABEL_GAP = 12
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
FOCUS_EVIDENCE_LABEL_WIDTH = 112
FOCUS_EVIDENCE_PADDING = 14
FOCUS_EVIDENCE_MAX_IMAGE_PIXELS = 16_000_000
FOCUS_EVIDENCE_MAX_TOTAL_PIXELS = 32_000_000


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


def _machine_label(number: str, index: int) -> str:
    label = f"Q{number}"
    if label.isascii() and all(char.isalnum() or char in "Q()._-" for char in label):
        return label[:18]
    return f"Q{index}"


def _build_evidence_table(
    aligned_rgb: np.ndarray,
    regions: list[dict],
    *,
    column_bounds: list[tuple[int, int]] | None = None,
) -> np.ndarray:
    height, width = aligned_rgb.shape[:2]
    boxes = _answer_crop_boxes(
        regions,
        width,
        height,
        column_bounds=column_bounds,
    )
    if not boxes:
        raise TemplateEvidenceError("matched artifact has no answer regions")

    columns = 2
    cell_width = 900
    cell_height = 160
    label_width = 112
    padding = 14
    rows = int(math.ceil(len(boxes) / columns))
    canvas = Image.new("RGB", (columns * cell_width, rows * cell_height), "white")
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.load_default(size=34)
    except TypeError:
        font = ImageFont.load_default()

    for index, (region, box) in enumerate(zip(regions, boxes), 1):
        crop = np.ascontiguousarray(
            aligned_rgb[box["y1"]:box["y2"], box["x1"]:box["x2"]]
        )
        column = 0 if index <= rows else 1
        row = (index - 1) if column == 0 else (index - rows - 1)
        cell_left = column * cell_width
        cell_top = row * cell_height
        cell_right = cell_left + cell_width - 1
        cell_bottom = cell_top + cell_height - 1
        draw.rectangle(
            (cell_left, cell_top, cell_right, cell_bottom),
            outline=(190, 190, 190),
            width=2,
        )
        draw.line(
            (cell_left + label_width, cell_top, cell_left + label_width, cell_bottom),
            fill=(205, 205, 205),
            width=2,
        )
        draw.text(
            (cell_left + 12, cell_top + 16),
            _machine_label(region["question_number"], index),
            fill="black",
            font=font,
        )

        crop_image = Image.fromarray(crop, mode="RGB")
        available_width = cell_width - label_width - padding * 2
        available_height = cell_height - padding * 2
        scale = min(
            available_width / crop_image.width,
            available_height / crop_image.height,
        )
        target_size = (
            max(1, int(round(crop_image.width * scale))),
            max(1, int(round(crop_image.height * scale))),
        )
        if target_size != crop_image.size:
            crop_image = crop_image.resize(target_size, Image.Resampling.LANCZOS)
        paste_left = cell_left + label_width + padding
        paste_top = cell_top + (cell_height - crop_image.height) // 2
        canvas.paste(crop_image, (paste_left, paste_top))
        del crop_image, crop
    return np.ascontiguousarray(np.asarray(canvas, dtype=np.uint8))


def _anchor_block_crops(
    aligned_rgb: np.ndarray,
    question_crop_plan: dict,
) -> list[tuple[dict, np.ndarray]]:
    """Crop validated full-height columns in left-to-right template order."""

    height, width = aligned_rgb.shape[:2]
    result: list[tuple[dict, np.ndarray]] = []
    for block in question_crop_plan["blocks"]:
        _normalized_left, top, _normalized_right, bottom = block["bbox_1000"]
        # The plan stores the same rounded source-pixel endpoints that were
        # used to build the model input. Reusing them avoids a one-pixel drift
        # when the source width is not divisible by 1000.
        x1, x2 = _resolved_frozen_pixel_bounds(block, width, height)
        y1 = max(0, min(height - 1, int(round(top * height / 1000))))
        y2 = max(y1 + 1, min(height, int(round(bottom * height / 1000))))
        crop = np.ascontiguousarray(aligned_rgb[y1:y2, x1:x2])
        if crop.size == 0:
            raise TemplateEvidenceError("anchor crop block produced an empty crop")
        result.append((block, crop))
    return result


def build_focus_evidence_sheet_layout(
    regions: list[dict],
    template_width: int,
    template_height: int,
    question_crop_plan: dict | None = None,
) -> list[dict[str, Any]]:
    """Return one native-resolution evidence image per frozen template column.

    The successful OCR path keeps answers at their original vertical positions
    and sends the frozen one/two/three-column layout in one request.  This gives
    Qwen stable spatial context without exposing the printed question text.
    The renderer and the server validator call this same geometry function.
    """

    if (
        isinstance(template_width, bool)
        or not isinstance(template_width, int)
        or template_width < 1
        or isinstance(template_height, bool)
        or not isinstance(template_height, int)
        or template_height < 1
    ):
        raise TemplateEvidenceError("focus evidence template dimensions are invalid")
    normalized = normalize_answer_regions(regions)
    plan = validate_anchor_crop_plan(question_crop_plan)
    columns: list[dict[str, Any]] = []
    for raw_column in plan["parameters"]["columns"]:
        left, right = _resolved_frozen_pixel_bounds(
            raw_column,
            template_width,
            template_height,
        )
        column = {
            "column_index": int(raw_column["column_index"]),
            "x1": left,
            "x2": right,
        }
        for key in (
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
        ):
            if key in raw_column:
                column[key] = raw_column[key]
        columns.append(column)
    boxes = _answer_crop_boxes(
        normalized,
        template_width,
        template_height,
        column_bounds=[(column["x1"], column["x2"]) for column in columns],
    )
    if not boxes:
        raise TemplateEvidenceError("matched artifact has no answer regions")

    grouped: list[list[dict[str, Any]]] = [[] for _column in columns]
    for entry in boxes:
        overlaps = [
            max(0, min(entry["x2"], column["x2"]) - max(entry["x1"], column["x1"]))
            for column in columns
        ]
        column_position = max(range(len(columns)), key=lambda index: overlaps[index])
        if overlaps[column_position] <= 0:
            raise TemplateEvidenceError("answer region falls outside frozen columns")
        column = columns[column_position]
        grouped[column_position].append({
            **entry,
            "x1": max(entry["x1"], column["x1"]),
            "x2": min(entry["x2"], column["x2"]),
        })

    sheets: list[dict[str, Any]] = []
    total_pixels = 0
    for column, entries in zip(columns, grouped, strict=True):
        if not entries:
            continue
        sheet_width = FOCUS_EVIDENCE_LABEL_WIDTH + column["x2"] - column["x1"]
        sheet_height = template_height
        rows: list[dict[str, Any]] = []
        for entry in entries:
            width = entry["x2"] - entry["x1"]
            height = entry["y2"] - entry["y1"]
            paste_left = FOCUS_EVIDENCE_LABEL_WIDTH + entry["x1"] - column["x1"]
            paste_top = entry["y1"]
            paste_right = paste_left + width
            paste_bottom = paste_top + height
            rows.append({
                "question_number": entry["question_number"],
                "question_type": entry["question_type"],
                "source_bbox": [
                    entry["x1"],
                    entry["y1"],
                    entry["x2"],
                    entry["y2"],
                ],
                "paste_bbox": [
                    paste_left,
                    paste_top,
                    paste_right,
                    paste_bottom,
                ],
                "label_y": max(0, min(sheet_height - 1, (entry["y1"] + entry["y2"]) // 2)),
                "label": _machine_label(entry["question_number"], len(rows) + 1),
            })

        pixels = sheet_width * sheet_height
        total_pixels += pixels
        if (
            pixels > FOCUS_EVIDENCE_MAX_IMAGE_PIXELS
            or total_pixels > FOCUS_EVIDENCE_MAX_TOTAL_PIXELS
        ):
            raise TemplateEvidenceError("focus evidence sheets exceed pixel limits")

        public_regions = []
        for row in rows:
            left, top, right, bottom = row["paste_bbox"]
            normalized_left = max(
                0,
                min(999, math.floor(left * 1000 / sheet_width)),
            )
            normalized_top = max(
                0,
                min(999, math.floor(top * 1000 / sheet_height)),
            )
            normalized_right = max(
                normalized_left + 1,
                min(1000, math.ceil(right * 1000 / sheet_width)),
            )
            normalized_bottom = max(
                normalized_top + 1,
                min(1000, math.ceil(bottom * 1000 / sheet_height)),
            )
            public_regions.append({
                "question_number": row["question_number"],
                "bbox_1000": [
                    normalized_left,
                    normalized_top,
                    normalized_right,
                    normalized_bottom,
                ],
            })
        sheets.append({
            "sheet_id": f"sheet_{len(sheets) + 1:02d}",
            "width": sheet_width,
            "height": sheet_height,
            "regions": public_regions,
            "rows": rows,
        })
        for key in (
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
        ):
            if key in column:
                sheets[-1][key] = column[key]
    if not sheets:
        raise TemplateEvidenceError("frozen columns contain no answer regions")
    rendered_numbers = [
        row["question_number"]
        for sheet in sheets
        for row in sheet["rows"]
    ]
    expected_numbers = [region["question_number"] for region in normalized]
    if rendered_numbers != expected_numbers:
        raise TemplateEvidenceError("answer regions do not follow frozen column order")
    return sheets


def build_multi_evidence_from_aligned(
    aligned_rgb: np.ndarray,
    source_mask: np.ndarray,
    template_rgb: np.ndarray,
    layout_snapshot: dict[str, Any],
    output_dir: str | os.PathLike[str],
    *,
    diagnostics: dict[str, Any] | None = None,
) -> EvidenceResult:
    """Materialize native-pixel answer focus sheets for one OCR call."""

    if not isinstance(layout_snapshot, dict):
        raise TemplateEvidenceError("layout snapshot is invalid")
    page_type_id = str(layout_snapshot.get("page_type_id") or "").strip()
    if not _PAGE_TYPE_RE.fullmatch(page_type_id):
        raise TemplateEvidenceError("layout snapshot page_type_id is invalid")
    # ``_legacy_answer_regions`` is populated only when an already-issued
    # focus-sheet job is being resumed. New ordinary-paper layouts never carry
    # answer boxes; they use the full-column difference builder instead.
    regions = normalize_answer_regions(
        layout_snapshot.get("answer_regions")
        or layout_snapshot.get("_legacy_answer_regions")
    )
    validate_anchor_crop_plan(
        layout_snapshot.get("question_crop_plan"),
    )
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

    frozen_columns = _frozen_column_bounds(
        layout_snapshot.get("question_crop_plan"),
        int(aligned_rgb.shape[1]),
        int(aligned_rgb.shape[0]),
    )
    answer_source_coverage, incomplete_answer_regions = (
        _answer_crop_source_coverage(
            source_mask,
            regions,
            column_bounds=frozen_columns,
        )
    )
    blank_detection_enabled = layout_snapshot.get("blank_detection_enabled", False)
    if not isinstance(blank_detection_enabled, bool):
        raise TemplateEvidenceError("layout snapshot blank_detection_enabled is invalid")
    template_baseline_prewhitened = layout_snapshot.get(
        "template_baseline_prewhitened", False
    )
    if not isinstance(template_baseline_prewhitened, bool):
        raise TemplateEvidenceError(
            "layout snapshot template_baseline_prewhitened is invalid"
        )

    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    sheet_metadata: list[dict[str, Any]] = []
    raw_handwriting = layout_snapshot.get("template_handwriting_regions")
    handwriting_regions = (
        normalize_template_handwriting_regions(raw_handwriting)
        if raw_handwriting is not None
        else []
    )
    merged_exemptions = _merge_template_exemption_regions(
        regions,
        handwriting_regions,
    )
    answer_only = _answer_only_pixels_v2(
        aligned_rgb,
        template_rgb,
        source_mask,
        regions,
        template_handwriting_regions=merged_exemptions,
    )
    has_handwriting_exemptions = any(item["regions"] for item in merged_exemptions)
    if blank_detection_enabled:
        blank_numbers, new_ink_counts = _confident_blank_questions(
            aligned_rgb,
            template_rgb,
            regions,
            answer_only_rgb=(answer_only if has_handwriting_exemptions else None),
        )
    else:
        blank_numbers, new_ink_counts = [], {}
    sheets = build_focus_evidence_sheet_layout(
        regions,
        int(aligned_rgb.shape[1]),
        int(aligned_rgb.shape[0]),
        layout_snapshot.get("question_crop_plan"),
    )
    try:
        for sheet in sheets:
            canvas = Image.new(
                "RGB",
                (sheet["width"], sheet["height"]),
                "white",
            )
            draw = ImageDraw.Draw(canvas)
            try:
                font = ImageFont.load_default(size=30)
            except TypeError:
                font = ImageFont.load_default()
            for row in sheet["rows"]:
                source_left, source_top, source_right, source_bottom = row[
                    "source_bbox"
                ]
                crop = np.ascontiguousarray(
                    answer_only[
                        source_top:source_bottom,
                        source_left:source_right,
                    ]
                )
                if row["question_type"] == "choice":
                    crop = _choice_answer_pixels(crop)
                paste_left, paste_top, _paste_right, _paste_bottom = row[
                    "paste_bbox"
                ]
                canvas.paste(
                    Image.fromarray(crop, mode="RGB"),
                    (paste_left, paste_top),
                )
                label_y = int(row["label_y"])
                draw.line(
                    (
                        72,
                        label_y,
                        FOCUS_EVIDENCE_LABEL_WIDTH - 8,
                        label_y,
                    ),
                    fill=(86, 145, 214),
                    width=1,
                )
                draw.text(
                    (12, max(0, label_y - 14)),
                    row["label"],
                    fill=(20, 84, 178),
                    font=font,
                )
                del crop
            path = output_root / f"evidence-{uuid.uuid4().hex}.png"
            canvas.save(path, format="PNG")
            paths.append(path.resolve())
            sheet_metadata.append({
                "sheet_id": sheet["sheet_id"],
                "regions": list(sheet["regions"]),
            })
            del canvas
    except Exception:
        for path in paths:
            path.unlink(missing_ok=True)
        raise

    result_diagnostics = dict(diagnostics or {})
    result_diagnostics.update({
        "selected_page_type_id": page_type_id,
        "template_width": int(aligned_rgb.shape[1]),
        "template_height": int(aligned_rgb.shape[0]),
        "ownership": "confirmed_bbox_focus_sheets",
        "difference_algorithm": "difference-v2-cleaner",
        "evidence_sheets": sheet_metadata,
        "blank_detection_enabled": blank_detection_enabled,
        "new_ink_pixels": new_ink_counts,
        "program_blank_count": len(blank_numbers),
        "answer_source_coverage": answer_source_coverage,
        "incomplete_answer_regions": incomplete_answer_regions,
        "answer_source_complete": not incomplete_answer_regions,
        "template_text_removed": True,
        "template_baseline_prewhitened": template_baseline_prewhitened,
        "template_handwriting_exemptions": sum(
            len(item["regions"]) for item in (handwriting_regions or [])
        ),
    })
    return EvidenceResult(
        page_type_id=page_type_id,
        evidence_path=str(paths[0]),
        evidence_paths=tuple(str(path) for path in paths),
        question_numbers=[region["question_number"] for region in regions],
        blank_question_numbers=blank_numbers,
        diagnostics=result_diagnostics,
    )


def _evidence_question_label_font():
    """Return the largest portable font used for Qwen ownership labels."""
    try:
        return ImageFont.load_default(size=EVIDENCE_Q_LABEL_FONT_SIZE)
    except TypeError:
        # Older Pillow releases do not accept ``size`` for the bundled font.
        return ImageFont.load_default()


def _drawing_region_pixels(box, width: int, height: int, column_left: int = 0) -> list[int]:
    """Whole-page normalized geometry to a half-open local pixel rectangle."""
    return [math.floor(box[0] * width / 1000) - column_left,
            math.floor(box[1] * height / 1000),
            math.ceil(box[2] * width / 1000) - column_left,
            math.ceil(box[3] * height / 1000)]


def _render_column_question_labels(
    crop, *, question_numbers, region_question_numbers, answer_regions, handwriting,
    anchor_by_number, solve_numbers, drawing_questions, column_left, page_width,
    reuse_question_anchor_labels=False,
):
    """One renderer shared by pre-freeze, template preview and student evidence."""
    height, column_width = crop.shape[:2]
    width, left, right = page_width, column_left, column_left + column_width
    # Unified answer boxes own ordinary labels in both historical modes.
    # Printed-number positions remain a compatibility fallback without boxes.
    boxed = {r["question_number"] for r in answer_regions if r["regions"]} - set(drawing_questions)
    solve_numbers = set(solve_numbers) - boxed
    text_slot_numbers = {r["question_number"] for r in answer_regions if _has_text_slot_labels(r, drawing_questions)}
    region_question_numbers = [n for n in question_numbers if n not in solve_numbers and (n not in drawing_questions or n in text_slot_numbers)]
    region_answer_regions = [r for r in answer_regions if r["question_number"] in region_question_numbers]
    from services.evidence_label_layout import render_labels

    def local_box(box):
        return [max(0, round(box[0] * width / 1000) - left),
                max(0, round(box[1] * height / 1000)),
                min(right - left, round(box[2] * width / 1000) - left),
                min(height, round(box[3] * height / 1000))]

    protected = [local_box(box) for item in answer_regions + handwriting
                 for box in item["regions"]]
    protected.extend(local_box(box) for q in drawing_questions.values()
                     for box in q["drawing_regions"])
    targets = []
    for number in region_question_numbers:
        item = next((r for r in region_answer_regions if r["question_number"] == number and r["regions"]), None)
        source = "template_answer_region"
        if item is None and not reuse_question_anchor_labels:
            item = next((r for r in handwriting if r["question_number"] == number and r["regions"]), None)
            source = "template_handwriting_region"
        if item:
            for index, box in enumerate(item["regions"]):
                if number in drawing_questions and item.get("region_kinds", [])[index] != "answer":
                    continue
                x1, y1, x2, y2 = local_box(box)
                if x2 <= x1:
                    continue
                if y2 <= y1:
                    raise TemplateEvidenceError(f"第 {number} 题作答框过小，请教师复核")
                # Large solve areas start near the top. Short adjacent
                # blanks stay at their own vertical midpoint.
                cy = (y1 + y2) / 2
                tall = y2 - y1 > 3 * EVIDENCE_Q_LABEL_FONT_SIZE
                label_top = y1
                if tall:
                    # An answer never sits above its own printed number. A box
                    # drawn up into the previous question keeps its label there.
                    printed = anchor_by_number.get(number, {}).get("question_number_bbox")
                    if printed and left <= (printed[0] + printed[2]) * width / 2000 < right:
                        number_top = round(printed[1] * height / 1000)
                        if y1 < number_top < y2:
                            label_top = number_top
                    cy = label_top + EVIDENCE_Q_LABEL_FONT_SIZE / 2
                targets.append({"question_number": number, "source": source,
                                "region_index": index, "region_bbox_1000": list(box),
                                **({"slot_id": item["slot_ids"][index]} if "slot_ids" in item else {}),
                                **({"label_align": "top", "label_top_px": label_top} if tall else {}),
                                "region_px": [x1, y1, x2, y2], "anchor_px": [x1, cy]})
        else:
            box = anchor_by_number.get(number, {}).get("question_number_bbox")
            if box and left <= (box[0] + box[2]) * width / 2000 < right:
                targets.append({"question_number": number, "source": "legacy_question_anchor",
                                "anchor_px": [round(box[0] * width / 1000) - left,
                                              (box[1] + box[3]) * height / 2000]})
    for number in question_numbers:
        if number in solve_numbers and left <= (anchor_by_number[number]["question_number_bbox"][0] + anchor_by_number[number]["question_number_bbox"][2]) * width / 2000 < right:
            box = anchor_by_number[number]["question_number_bbox"]
            targets.append({"question_number": number, "source": "printed_question_number",
                            "question_number_bbox": list(box),
                            "anchor_px": [round(box[2] * width / 1000) - left,
                                          (box[1] + box[3]) * height / 2000]})
        if number in drawing_questions and left <= drawing_questions[number]["label_anchor"][0] * width / 1000 < right:
            anchor = drawing_questions[number]["label_anchor"]
            main = _drawing_region_pixels(drawing_questions[number]["drawing_regions"][0], width, height, left)
            # The schema has already checked this frozen routing point is at
            # or left of the drawing. Visible text may still move to safe
            # whitespace/a rail; rendering never rewrites the frozen anchor.
            targets.append({"question_number": number, "source": "drawing_region_left",
                            "label_anchor": list(anchor),
                            "label_vertical_overlap_with_main_drawing": True,
                            "main_drawing_pixel_box_exclusive": main,
                            "region_bbox_1000": list(drawing_questions[number]["drawing_regions"][0]),
                            "anchor_px": [round(anchor[0] * width / 1000) - left,
                                          round(anchor[1] * height / 1000)]})
    try:
        return render_labels(
            crop, targets, protected, font=_evidence_question_label_font(),
            color=EVIDENCE_Q_LABEL_COLOR,
            padding_x=EVIDENCE_Q_LABEL_PADDING_X, padding_y=EVIDENCE_Q_LABEL_PADDING_Y,
            gap=EVIDENCE_Q_LABEL_GAP)
    except TemplateSchemaError:
        # Preserve the controlled rule and question through prefreeze/import
        # diagnostics instead of aggregating it into an opaque layout error.
        raise
    except ValueError as exc:
        raise TemplateEvidenceError(str(exc)) from exc


def validate_drawing_label_geometry(
    question_schema, question_anchors, template_answer_regions, template_handwriting_regions,
    *, page_width: int, page_height: int, question_crop_plan=None, template_mode: str = "marked",
) -> dict:
    """Pre-freeze geometry gate without student images, file writes or models.

    Blank-template foreground safety must still be rechecked against each
    student's actual difference. Legacy non-graphic schemas bypass this gate.
    Returned local label rectangles use the same renderer as actual evidence.
    """
    schema = normalize_question_schema(question_schema)
    drawing = {item["question_number"]: item for item in schema
               if item.get("answer_kind") in {"drawing", "mixed"}}
    receipt = {"version": "drawing-label-geometry-v1", "placements": [],
               "student_foreground_checked": False}
    if not drawing:
        return receipt
    if (isinstance(page_width, bool) or isinstance(page_height, bool)
            or not isinstance(page_width, int) or not isinstance(page_height, int)
            or min(page_width, page_height) <= 0 or page_width * page_height > MAX_SOURCE_PIXELS):
        raise TemplateEvidenceError("作图定位页面尺寸无效，请教师复核")
    anchors = normalize_question_anchors(question_anchors or [])
    answer_regions = normalize_template_handwriting_regions(template_answer_regions or [])
    handwriting = normalize_template_handwriting_regions(template_handwriting_regions or [])
    anchor_by_number = {item["question_number"]: item for item in anchors}
    _validate_printed_solve_anchors(schema, anchors, answer_regions)
    plan = validate_anchor_crop_plan(question_crop_plan) if question_crop_plan is not None else None
    columns = _frozen_column_bounds(plan, page_width, page_height) if plan is not None else [(0, page_width)]
    # A graphical fill/choice item has its one Q at the drawing anchor, just
    # like a graphical solve item. Do not add another ordinary answer-region Q.
    region_numbers = [item["question_number"] for item in schema
                      if item["question_type"] != "solve" and item["question_number"] not in drawing]
    region_answers = [item for item in answer_regions if item["question_number"] in region_numbers]
    if template_mode == "blank" and ({item["question_number"] for item in region_answers} != set(region_numbers)
                                      or any(not item["regions"] for item in region_answers)):
        raise TemplateEvidenceError("模板作答框未覆盖全部题号，请教师复核")
    blocking_issues: list[str] = []
    for column_index, (left, right) in enumerate(columns, 1):
        canvas = Image.new("RGB", (right - left, page_height), "white")
        placements = []
        try:
            _, placements, label_layout = _render_column_question_labels(
                np.asarray(canvas), question_numbers=[q["question_number"] for q in schema],
                region_question_numbers=region_numbers, answer_regions=answer_regions,
                handwriting=handwriting, anchor_by_number=anchor_by_number,
                solve_numbers={q["question_number"] for q in schema if q["question_type"] == "solve" and q["question_number"] not in drawing},
                drawing_questions=drawing, column_left=left, page_width=page_width,
                reuse_question_anchor_labels=template_mode == "blank")
        except TemplateEvidenceError as exc:
            issues = (exc.diagnostics or {}).get("issues")
            if not isinstance(issues, list) or not issues:
                issues = [line.strip(" -") for line in str(exc).splitlines() if line.strip()]
            blocking_issues.extend(f"第 {column_index} 栏：{issue}" for issue in issues)
            continue
        receipt["placements"].extend({**item, "column_index": column_index,
                                       "label_layout": label_layout} for item in placements)
    if blocking_issues:
        unique = list(dict.fromkeys(blocking_issues))
        raise TemplateEvidenceError(
            "模板题号标记存在多个阻断项，请一次处理：\n"
            + "\n".join(f"- {issue}" for issue in unique)
            + "\n请教师复核作答框和题号标记位置。",
            {"issues": unique},
        )
    placed = [item["question_number"] for item in receipt["placements"] if item["source"] == "drawing_region_left"]
    if set(placed) != set(drawing) or len(placed) != len(drawing):
        raise TemplateEvidenceError("作图题标签不完整或重复，请教师复核定位")
    return receipt


def _new_ink_extent(student_rgb, template_rgb, window, band, exclude=()):
    """New dark ink on template-white pixels, kept by component centroid.

    Returns (pixels, [left, top, right, bottom] or None). A component whose
    centroid lies in an ``exclude`` rectangle belongs to another slot's zone.
    """
    wx1, wy1, wx2, wy2 = window
    if wx2 <= wx1 or wy2 <= wy1:
        return 0, None
    student = cv2.cvtColor(np.ascontiguousarray(student_rgb[wy1:wy2, wx1:wx2]), cv2.COLOR_RGB2GRAY)
    template = cv2.cvtColor(np.ascontiguousarray(template_rgb[wy1:wy2, wx1:wx2]), cv2.COLOR_RGB2GRAY)
    white = template >= 225
    if int(np.count_nonzero(white)) < 25:
        return 0, None
    shift = max(-35.0, min(35.0, float(np.median(template[white]) - np.median(student[white]))))
    adjusted = np.clip(student.astype(np.float32) + shift, 0, 255)
    mask = white & (adjusted <= 190) & (template.astype(np.float32) - adjusted >= 30)
    count, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    pixels, extent = 0, None
    for index in range(1, count):
        area = int(stats[index, cv2.CC_STAT_AREA])
        cx, cy = centroids[index][0] + wx1, centroids[index][1] + wy1
        if area < 3 or not (band[0] <= cx < band[2] and band[1] <= cy < band[3]) or any(
                r[0] <= cx < r[2] and r[1] <= cy < r[3] for r in exclude):
            continue
        x, y = int(stats[index, cv2.CC_STAT_LEFT]) + wx1, int(stats[index, cv2.CC_STAT_TOP]) + wy1
        box = [x, y, x + int(stats[index, cv2.CC_STAT_WIDTH]), y + int(stats[index, cv2.CC_STAT_HEIGHT])]
        pixels += area
        extent = box if extent is None else [min(extent[0], box[0]), min(extent[1], box[1]),
                                             max(extent[2], box[2]), max(extent[3], box[3])]
    return pixels, extent


def _unmarked_zones(number_px, box_px, column, template_gray, obstacles, h):
    """Blank margin before the printed number and after the stem's last line.

    Both stop at printed text, a fixed answer area or the column edge; a gap
    shared with a neighbouring question on the same row is split at its middle.
    """
    nx1, ny1, nx2, ny2 = number_px
    left_edge, right_edge = column
    min_width = max(8, h // 2)

    def overlaps_box(zone):
        # The frozen box already owns that side, e.g. a marked template's
        # handwriting written before the number; never count its ink twice.
        return (zone[0] < box_px[2] and zone[2] > box_px[0]
                and zone[1] < box_px[3] and zone[3] > box_px[1])

    def printed(y1, y2, x1, x2):
        return (template_gray[y1:y2, x1:x2] < 200).any(axis=0) if x2 > x1 and y2 > y1 else np.zeros(0, bool)

    def blocking(y1, y2):
        return [box for box in obstacles if box[1] < y2 and box[3] > y1]

    # Before the number: step over a tight model box's cut glyph first.
    ink = printed(ny1, ny2, left_edge, nx1)
    join = max(2, round(0.35 * h))
    right, x = nx1, nx1 - 1
    while x >= left_edge and right - x <= join:
        if ink[x - left_edge]:
            right = x
        x -= 1
    left = left_edge
    for x in range(right - 1, left_edge - 1, -1):
        if ink[x - left_edge]:
            left = x + 1 + (right - x - 1) // 2
            break
    left = max([left, right - 10 * h] + [box[2] for box in blocking(ny1, ny2) if box[2] <= right])
    before = [left, ny1 - h // 2, right, ny2 + h // 2]
    if right - left < min_width or overlaps_box(before):
        before = None

    # After the stem: its last printed line above a box on a later row
    # (a choice box framing the options), otherwise the number's own row.
    line = [ny1, ny2]
    if box_px[1] >= ny2:
        rows = (template_gray[ny1:box_px[1], left_edge:right_edge] < 200).any(axis=1)
        y = len(rows) - 1
        while y >= 0 and not rows[y]:
            y -= 1
        if y >= 0:
            bottom = y + 1
            while y >= 0 and rows[y]:
                y -= 1
            if bottom - (y + 1) >= h // 2:
                line = [ny1 + y + 1, ny1 + bottom]
    start = nx2 if line == [ny1, ny2] else left_edge
    ink = printed(line[0], line[1], start, right_edge)
    first = next((i for i, value in enumerate(ink) if value), None)
    after = None
    if first is not None:
        end, gap = start + first, 0
        for i in range(first, len(ink)):
            if ink[i]:
                end, gap = start + i + 1, 0
            else:
                gap += 1
                if gap >= round(1.5 * h):
                    break
        stop = right_edge
        following = next((end + i for i, value in enumerate(ink[end - start:]) if value), None)
        if following is not None:
            stop = end + (following - end) // 2
        stop = min([stop, end + 10 * h] + [box[0] for box in blocking(*line) if box[0] >= end])
        candidate = [end, line[0] - h // 2, stop, line[1] + h // 2]
        if stop - end >= min_width and not overlaps_box(candidate):
            after = candidate
    return before, after


def _route_unmarked_answer_positions(
    schema, answer_regions, handwriting, anchor_by_number, drawing_questions,
    student_rgb, template_rgb, columns,
):
    """Pick the ink-bearing side for a slot whose page prints no position.

    Candidates are the frozen model box, the blank margin before the printed
    number and the blank space after the stem's last line. Ink in exactly one
    candidate decides where the slot label goes; ink in more than one is
    returned for teacher review. Exemptions and frozen geometry never move.
    Ink is counted against the template baseline, so printed text inside an
    exemption box (a bracket, a row of options) is never mistaken for ink.
    """
    height, width = student_rgb.shape[:2]

    def px(box):
        return [round(box[0] * width / 1000), round(box[1] * height / 1000),
                round(box[2] * width / 1000), round(box[3] * height / 1000)]

    by_number = {item["question_number"]: item for item in answer_regions}
    unmarked = [q["question_number"] for q in schema if q.get("answer_position") == "unmarked"]
    # Boxes of unmarked neighbours share a gap at its middle instead of
    # blocking; every other answer, handwriting or drawing area is fixed.
    obstacles = [px(box) for item in answer_regions + handwriting if item["question_number"] not in unmarked
                 for box in item["regions"]]
    obstacles += [px(box) for question in drawing_questions.values() for box in question["drawing_regions"]]
    template_gray = cv2.cvtColor(template_rgb, cv2.COLOR_RGB2GRAY)
    records, plans = [], []
    for number in unmarked:
        item = by_number.get(number)
        record = {"question_number": number}
        records.append(record)
        if (number in drawing_questions or item is None or len(item["regions"]) != 1
                or item.get("region_kinds", ["answer"]) != ["answer"]):
            record["decision"] = "not_single_text_slot"
            continue
        number_box = anchor_by_number.get(number, {}).get("question_number_bbox")
        if not number_box:
            record["decision"] = "no_printed_number"
            continue
        number_px, box_px = px(number_box), px(item["regions"][0])
        h = max(8, number_px[3] - number_px[1])
        center = (number_px[0] + number_px[2]) / 2
        column = next(((left, right) for left, right in columns if left <= center < right), None)
        record.update(slot_id=(item.get("slot_ids") or [None])[0], number_bbox_px=number_px, box_px=box_px)
        if column is None:
            record["decision"] = "number_outside_columns"
            continue
        before, after = _unmarked_zones(number_px, box_px, column, template_gray, obstacles, h)
        record.update(before_number_px=before, after_stem_px=after)
        plans.append((record, item, h, {"box": box_px, "before_number": before, "after_stem": after}))
    review = []
    routed = dict(by_number)
    for record, item, h, zones in plans:
        # Another slot's margin zone owns its ink, even inside this box.
        others = [zone for other in plans if other[0] is not record
                  for side, zone in other[3].items() if side != "box" and zone]
        pad = round(0.3 * h)
        ink, extents = {}, {}
        for side, zone in zones.items():
            if zone is None:
                continue
            ink[side], extents[side] = _new_ink_extent(
                student_rgb, template_rgb,
                [max(0, zone[0] - h), max(0, zone[1] - h), min(width, zone[2] + h), min(height, zone[3] + h)],
                [zone[0] - (pad if side == "box" else 0), zone[1] - pad, zone[2] + (pad if side == "box" else 0),
                 zone[3] + pad], others)
        threshold = max(12, round(0.05 * h * h))
        sides = [side for side, value in ink.items() if value >= threshold]
        record.update(ink_px=ink, min_ink_px=threshold)
        if len(sides) > 1:
            record["decision"] = "ambiguous"
            review.append(record["question_number"])
        elif sides and sides[0] != "box":
            side = sides[0]
            # Frame the ink itself so the label sits level with it.
            x1, y1, x2, y2 = extents[side]
            target = [max(0, x1 - pad), max(0, y1 - pad), min(width, x2 + pad), min(height, y2 + pad)]
            moved = [math.floor(target[0] * 1000 / width), math.floor(target[1] * 1000 / height),
                     math.ceil(target[2] * 1000 / width), math.ceil(target[3] * 1000 / height)]
            routed[record["question_number"]] = {**item, "regions": [moved]}
            record.update(decision=side, label_region_1000=moved)
        else:
            record["decision"] = "box"
    return [routed[item["question_number"]] for item in answer_regions], records, review


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


def build_evidence_from_aligned(
    aligned_rgb: np.ndarray,
    source_mask: np.ndarray,
    template_rgb: np.ndarray,
    layout_snapshot: dict[str, Any],
    output_dir: str | os.PathLike[str],
    *,
    diagnostics: dict[str, Any] | None = None,
) -> EvidenceResult:
    """Build the existing OCR evidence artifact from a verified aligned page.

    This entry point deliberately performs no feature extraction and estimates
    no geometry.  Accelerator validation supplies a server-replayed aligned
    page and binary source mask, while this function keeps the established
    answer-crop, blank-detection, and evidence-montage semantics unchanged.
    """

    if not isinstance(layout_snapshot, dict):
        raise TemplateEvidenceError("layout snapshot is invalid")
    page_type_id = str(layout_snapshot.get("page_type_id") or "").strip()
    if not _PAGE_TYPE_RE.fullmatch(page_type_id):
        raise TemplateEvidenceError("layout snapshot page_type_id is invalid")
    # Keep the old focus-sheet contract readable for jobs created before the
    # answer-box contract was removed. This fallback is intentionally private
    # and is never written into a new template artifact or layout snapshot.
    regions = normalize_answer_regions(
        layout_snapshot.get("answer_regions")
        or layout_snapshot.get("_legacy_answer_regions")
    )
    blank_detection_enabled = layout_snapshot.get("blank_detection_enabled", False)
    if not isinstance(blank_detection_enabled, bool):
        raise TemplateEvidenceError("layout snapshot blank_detection_enabled is invalid")
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

    frozen_columns = _frozen_column_bounds(
        layout_snapshot.get("question_crop_plan"),
        int(aligned_rgb.shape[1]),
        int(aligned_rgb.shape[0]),
    )
    answer_source_coverage, incomplete_answer_regions = (
        _answer_crop_source_coverage(
            source_mask,
            regions,
            column_bounds=frozen_columns,
        )
    )
    if blank_detection_enabled:
        blank_numbers, new_ink_counts = _confident_blank_questions(
            aligned_rgb,
            template_rgb,
            regions,
        )
    else:
        blank_numbers, new_ink_counts = [], {}
    question_crop_plan = layout_snapshot.get("question_crop_plan")
    # Coarse top anchors are useful for a teacher-facing page overview, but they
    # are not authoritative answer ownership. Real papers have highly uneven
    # question heights; fixed vertical slices made later right-column hints drift
    # onto unrelated questions and even the page footer. OCR must consume the
    # teacher-confirmed answer regions already frozen in this layout snapshot.
    evidence = _build_evidence_table(
        aligned_rgb,
        regions,
        column_bounds=frozen_columns,
    )
    evidence_ownership = "confirmed_bbox_neighbor_clamped"

    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    evidence_path = output_root / f"evidence-{uuid.uuid4().hex}.png"
    try:
        Image.fromarray(evidence, mode="RGB").save(evidence_path, format="PNG")
    except Exception:
        evidence_path.unlink(missing_ok=True)
        raise

    result_diagnostics = dict(diagnostics or {})
    result_diagnostics.update({
        "selected_page_type_id": page_type_id,
        "template_width": int(aligned_rgb.shape[1]),
        "template_height": int(aligned_rgb.shape[0]),
        "evidence_width": int(evidence.shape[1]),
        "evidence_height": int(evidence.shape[0]),
        "ownership": evidence_ownership,
        "blank_detection_enabled": blank_detection_enabled,
        "new_ink_pixels": new_ink_counts,
        "program_blank_count": len(blank_numbers),
        "answer_source_coverage": answer_source_coverage,
        "incomplete_answer_regions": incomplete_answer_regions,
        "answer_source_complete": not incomplete_answer_regions,
    })
    if question_crop_plan is not None:
        result_diagnostics["question_crop_plan"] = {
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
        evidence_path=str(evidence_path.resolve()),
        question_numbers=[region["question_number"] for region in regions],
        blank_question_numbers=blank_numbers,
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
