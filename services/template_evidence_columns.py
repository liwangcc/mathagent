"""Full-column difference evidence: one labelled, full-height difference image per frozen column.

``build_full_column_difference_evidence_from_aligned`` renders it from an aligned
page, with question ownership labelled locally in each column:

- ``_full_column_layout`` validates the layout snapshot and decides where every
  question is labelled (``_FullColumnLayout``, ``_ColumnLabelSets``,
  ``_layout_question_sequence``, ``_layout_question_kinds``,
  ``_expected_column_labels``, ``_route_layout_answer_positions``).
- ``_full_column_answer_pixels`` gives the difference pixels, with drawing regions
  restored to the student's original (``_require_aligned_pixels``).
- ``_render_full_columns`` saves one labelled PNG per column;
  ``_verify_rendered_labels`` checks every expected label landed exactly once.
- ``_full_column_diagnostics`` is the evidence contract the grader and the
  artifact verifier read back (``_FROZEN_COLUMN_EVIDENCE_KEYS``,
  ``_FROZEN_COLUMN_DIAGNOSTIC_KEYS``).

Moved unchanged from services/template_evidence.py, which still exposes these names.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.template_anchor_crops import validate_anchor_crop_plan
from services.template_evidence_base import (
    EvidenceResult,
    Image,
    TemplateEvidenceError,
    _PAGE_TYPE_RE,
    np,
)
from services.template_evidence_crops import _frozen_column_bounds
from services.template_evidence_labels import (
    _drawing_region_pixels,
    _render_column_question_labels,
    _route_unmarked_answer_positions,
)
from services.template_evidence_pixels import _answer_only_pixels_v2
from services.template_evidence_residue import _merge_template_exemption_regions
from services.template_evidence_schema import (
    _bind_slot_geometry,
    _has_text_slot_labels,
    normalize_question_anchors,
    normalize_question_schema,
    normalize_template_handwriting_regions,
)


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
