"""The OCR evidence artifact of one verified aligned page.

``build_evidence_from_aligned`` builds it without feature extraction or any
geometry estimate: accelerator validation supplies a server-replayed aligned
page and its binary source mask, and the established answer-crop,
blank-detection and evidence-montage semantics apply unchanged.

Moved unchanged from services/template_evidence.py, which still exposes this name.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from services.template_evidence_base import (
    EvidenceResult,
    Image,
    TemplateEvidenceError,
    _PAGE_TYPE_RE,
    np,
)
from services.template_evidence_crops import (
    _answer_crop_source_coverage,
    _frozen_column_bounds,
)
from services.template_evidence_pixels import _confident_blank_questions
from services.template_evidence_schema import normalize_answer_regions
from services.template_evidence_sheets import _build_evidence_table


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
