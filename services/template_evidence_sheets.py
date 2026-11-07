"""Focus evidence sheets: native-resolution answer images, one per frozen template column, for one OCR call.

- ``build_focus_evidence_sheet_layout`` lays out one evidence image per frozen
  column (``_anchor_block_crops`` crops the validated full-height columns in
  left-to-right order), keeping answers at their original vertical positions
  within the ``FOCUS_EVIDENCE_*`` size limits.
- ``build_multi_evidence_from_aligned`` materializes those sheets from an aligned
  page; ``_build_evidence_table`` and ``_machine_label`` lay out and label the
  answer cells.  ``FOCUS_EVIDENCE_PADDING`` is kept with them; no code reads it.

Moved unchanged from services/template_evidence.py, which still exposes these names.
"""

from __future__ import annotations

import math
import os
import uuid
from pathlib import Path
from typing import Any

from services.template_anchor_crops import validate_anchor_crop_plan
from services.template_evidence_base import (
    EvidenceResult,
    Image,
    ImageDraw,
    ImageFont,
    TemplateEvidenceError,
    _PAGE_TYPE_RE,
    np,
)
from services.template_evidence_crops import (
    _answer_crop_boxes,
    _answer_crop_source_coverage,
    _frozen_column_bounds,
    _resolved_frozen_pixel_bounds,
)
from services.template_evidence_pixels import (
    _answer_only_pixels_v2,
    _choice_answer_pixels,
    _confident_blank_questions,
)
from services.template_evidence_residue import _merge_template_exemption_regions
from services.template_evidence_schema import (
    normalize_answer_regions,
    normalize_template_handwriting_regions,
)


FOCUS_EVIDENCE_LABEL_WIDTH = 112


FOCUS_EVIDENCE_PADDING = 14


FOCUS_EVIDENCE_MAX_IMAGE_PIXELS = 16_000_000


FOCUS_EVIDENCE_MAX_TOTAL_PIXELS = 32_000_000


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
