"""Column evidence measured on the template image itself, without a model call.

- ``_infer_local_two_column_payload`` returns a high-confidence two-column split
  from a landscape page's printed gutter, or ``None`` (the image is scaled to at
  most ``_LOCAL_LAYOUT_MAX_WIDTH`` and must be at least
  ``_LOCAL_LAYOUT_MIN_SOURCE_WIDTH`` wide).
- ``_inspect_local_double_evidence`` collects conservative evidence for a
  double-column draft (``_DOUBLE_EVIDENCE_VERSION``,
  ``_DOUBLE_EVIDENCE_THRESHOLDS``); ``_double_layout_draft`` combines the two into
  a code-proposed double layout and its review evidence.
- ``_inspect_local_triple_evidence`` collects per-column content evidence for a
  model-proposed triple layout (``_TRIPLE_EVIDENCE_VERSION``,
  ``_TRIPLE_EVIDENCE_THRESHOLDS``).

Moved unchanged from services/template_column_layout.py, which still exposes these names.
"""

from __future__ import annotations

import os
from typing import Any

from services.template_column_layout_contract import MIN_COLUMN_WIDTH_1000
from services.template_column_layout_prompts import _strict_payload


_LOCAL_LAYOUT_MAX_WIDTH = 1200
_LOCAL_LAYOUT_MIN_SOURCE_WIDTH = 800
# Double-column gate scoring rules.  Bump the version whenever a threshold or
# the reason vocabulary changes; the values are copied into every evidence
# record so a persisted decision stays auditable against its own rules.
_DOUBLE_EVIDENCE_VERSION = "double-gutter-evidence-v4"
_DOUBLE_EVIDENCE_THRESHOLDS: dict[str, float | int] = {
    # Ink fraction in the central band that reads as a printed seam/content.
    "vertical_peak_density_max": 0.55,
    # Rows with ink through the gutter, and the seam ink relative to the sides.
    "seam_active_rows_max": 0.55,
    "seam_density_floor": 0.025,
    "seam_to_side_ratio_max": 0.30,
    # Fraction of body rows containing one long run crossing the gutter.
    "cross_line_fraction_max": 0.002,
    # Two-sided content requirements.
    "side_density_min": 0.012,
    "side_active_rows_min": 0.28,
    "left_right_ratio_max": 3.0,
    # Canonicalization can thicken a page-bottom border into several dark
    # rows.  It is outside the question body and must not look like a printed
    # question crossing the gutter.  Other density checks still inspect the
    # full body down to 95% of the page.
    "cross_line_scan_bottom_fraction": 0.90,
    # Permitted distance of the seam from page centre, in 1000 units.
    "center_offset_1000_max": 35,
    "broad_valley_1000_min": 400,
    "broad_valley_1000_max": 600,
    "broad_valley_density_max": 0.02,
    # Column width floor shared with boundary validation.
    "min_column_width_1000": MIN_COLUMN_WIDTH_1000,
}
# Triple-column content gate.  The layout model proposes the seams; this check
# requires every column to carry printed content and every seam to be a blank
# valley before the proposal may freeze crops.
_TRIPLE_EVIDENCE_VERSION = "triple-content-evidence-v1"
_TRIPLE_EVIDENCE_THRESHOLDS: dict[str, float | int] = {
    "column_density_min": 0.008,
    "column_active_rows_min": 0.20,
    "seam_density_max": 0.030,
    "seam_to_side_ratio_max": 0.45,
    "cross_line_fraction_max": 0.002,
    "min_column_width_1000": MIN_COLUMN_WIDTH_1000,
}


def _infer_local_two_column_payload(
    image_path: str | os.PathLike[str],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Return a high-confidence landscape gutter split without a model call.

    The fast path deliberately recognizes only the common two-page/two-column
    case. Ambiguous pages, single columns and three-column layouts continue to
    the layout model instead of letting a heuristic become the authority.
    """
    import cv2
    import numpy as np
    from PIL import Image

    with Image.open(image_path) as opened:
        source_width, source_height = opened.size
        if (
            source_width < _LOCAL_LAYOUT_MIN_SOURCE_WIDTH
            or source_width / float(source_height) < 1.10
        ):
            return None
        gray = np.asarray(opened.convert("L"))

    scale = min(1.0, _LOCAL_LAYOUT_MAX_WIDTH / float(source_width))
    if scale < 1.0:
        gray = cv2.resize(
            gray,
            (max(1, round(source_width * scale)), max(1, round(source_height * scale))),
            interpolation=cv2.INTER_AREA,
        )
    height, width = gray.shape
    if width < 400 or height < 200:
        return None

    # Subtract slow camera-lighting variation before measuring the vertical
    # ink profile. This keeps a photographed gray page from looking fully dark.
    background = cv2.GaussianBlur(
        gray,
        (0, 0),
        sigmaX=max(4.0, width / 100.0),
        sigmaY=max(4.0, width / 100.0),
    )
    ink = (
        (gray.astype(np.int16) + 18 < background.astype(np.int16))
        & (gray < 210)
    )
    top = round(height * 0.12)
    bottom = round(height * 0.95)
    body = ink[top:bottom]
    if body.size == 0:
        return None

    profile = body.mean(axis=0)
    window = max(9, round(width * 0.014))
    if window % 2 == 0:
        window += 1
    smoothed = np.convolve(
        profile,
        np.ones(window, dtype=np.float64) / window,
        mode="same",
    )
    search_left = round(width * 0.42)
    search_right = round(width * 0.58)
    split = search_left + int(np.argmin(smoothed[search_left:search_right]))
    # A blank gutter is usually a plateau.  ``argmin`` alone chooses its left
    # edge, which needlessly narrows the first crop; use the midpoint of the
    # contiguous low-density valley while it remains inside the search band.
    valley_limit = max(float(smoothed[split]) + 0.003, 0.012)
    valley_left = split
    while valley_left > search_left and smoothed[valley_left - 1] <= valley_limit:
        valley_left -= 1
    valley_right = split
    while valley_right < search_right - 1 and smoothed[valley_right + 1] <= valley_limit:
        valley_right += 1
    split = round((valley_left + valley_right) / 2)
    side_pad = max(2, round(width * 0.015))
    left_body = body[:, : max(1, split - side_pad)]
    right_body = body[:, min(width - 1, split + side_pad):]
    if left_body.size == 0 or right_body.size == 0:
        return None

    gutter_density = float(smoothed[split])
    left_density = float(left_body.mean())
    right_density = float(right_body.mean())
    minimum_side_density = min(left_density, right_density)
    left_active_rows = float((left_body.mean(axis=1) > 0.003).mean())
    right_active_rows = float((right_body.mean(axis=1) > 0.003).mean())
    if (
        minimum_side_density < 0.012
        or min(left_active_rows, right_active_rows) < 0.28
        or gutter_density > 0.020
        or gutter_density > minimum_side_density * 0.38
    ):
        return None

    split_1000 = int(round(split * 1000 / width))
    # Do not turn a broad off-center blank gutter into a nominal 500 split.
    # The valley can easily span the page midpoint even when its actual
    # midpoint is several percent to the left or right.  Only snap when the
    # measured valley is already within a narrow rounding band around center;
    # otherwise retain the measured boundary so the crop cannot clip a column.
    if abs(split_1000 - 500) <= 8:
        center = round(width * 0.5)
        center_radius = max(4, round(width * 0.008))
        center_window = smoothed[
            max(search_left, center - center_radius):
            min(search_right, center + center_radius + 1)
        ]
        # Preserve the convenient 500 boundary only when the page confirms a
        # genuinely clear center window; a narrow off-center gutter keeps its
        # measured midpoint so it cannot clip one column's content.
        if (
            center_window.size
            and float(np.max(center_window)) <= max(valley_limit, 0.015)
        ):
            split_1000 = 500
    payload = _strict_payload({
        "column_count": 2,
        "columns": [
            {"column_index": 1, "x1_1000": 0, "x2_1000": split_1000},
            {"column_index": 2, "x1_1000": split_1000, "x2_1000": 1000},
        ],
    })
    for candidate in payload["candidates"]:
        candidate["source_key"] = "local_column_gutter"
        candidate["coordinate_trust"] = "local_high_confidence"
    return payload, {
        "strategy": "local_gutter_v1",
        "split_1000": split_1000,
        "gutter_density": round(gutter_density, 6),
        "left_density": round(left_density, 6),
        "right_density": round(right_density, 6),
    }


def _inspect_local_double_evidence(
    image_path: str | os.PathLike[str],
) -> dict[str, Any]:
    """Collect conservative, non-model evidence for a double-column draft.

    This is intentionally a review gate rather than a second layout authority.
    It detects common adversarial fixtures (a full-height printed seam, a line
    crossing the gutter, a single-column page, or a strongly off-centre split)
    and records why a candidate is or is not safe to expose to OCR.
    """
    import cv2
    import numpy as np
    from PIL import Image

    evidence: dict[str, Any] = {
        "evidence_version": _DOUBLE_EVIDENCE_VERSION,
        "strategy": "local_gutter_v1",
        # The gate thresholds travel with the evidence so a persisted
        # needs_review/ready decision can be audited against the exact
        # scoring rules that produced it, not the rules of a later release.
        "thresholds": dict(_DOUBLE_EVIDENCE_THRESHOLDS),
        "confidence": 0.0,
        "confidence_level": "low",
        "validation": "failed",
        "reasons": [],
    }
    try:
        with Image.open(image_path) as opened:
            source_width, source_height = opened.size
            gray = np.asarray(opened.convert("L"))
        if source_width < _LOCAL_LAYOUT_MIN_SOURCE_WIDTH:
            evidence["reasons"].append("source_too_narrow")
            return evidence
        scale = min(1.0, _LOCAL_LAYOUT_MAX_WIDTH / float(source_width))
        if scale < 1.0:
            gray = cv2.resize(
                gray,
                (max(1, round(source_width * scale)), max(1, round(source_height * scale))),
                interpolation=cv2.INTER_AREA,
            )
        height, width = gray.shape
        if width < 400 or height < 200:
            evidence["reasons"].append("source_too_small")
            return evidence
        background = cv2.GaussianBlur(
            gray,
            (0, 0),
            sigmaX=max(4.0, width / 100.0),
            sigmaY=max(4.0, width / 100.0),
        )
        ink = (
            (gray.astype(np.int16) + 18 < background.astype(np.int16))
            & (gray < 210)
        )
        top = round(height * 0.12)
        bottom = round(height * 0.95)
        body = ink[top:bottom]
        if body.size == 0:
            evidence["reasons"].append("empty_body")
            return evidence
        profile = body.mean(axis=0)
        window = max(9, round(width * 0.014))
        if window % 2 == 0:
            window += 1
        smoothed = np.convolve(
            profile,
            np.ones(window, dtype=np.float64) / window,
            mode="same",
        )
        search_left = round(width * 0.35)
        search_right = round(width * 0.65)
        split = search_left + int(np.argmin(smoothed[search_left:search_right]))
        valley_limit = max(float(smoothed[split]) + 0.003, 0.012)
        valley_left = split
        while valley_left > search_left and smoothed[valley_left - 1] <= valley_limit:
            valley_left -= 1
        valley_right = split
        while valley_right < search_right - 1 and smoothed[valley_right + 1] <= valley_limit:
            valley_right += 1
        split = round((valley_left + valley_right) / 2)
        radius = max(3, round(width * 0.012))
        seam_start = max(0, split - radius)
        seam_end = min(width, split + radius + 1)
        seam = body[:, seam_start:seam_end]
        left = body[:, :max(1, split - radius)]
        right = body[:, min(width - 1, split + radius):]
        seam_density = float(seam.mean()) if seam.size else 1.0
        left_density = float(left.mean()) if left.size else 0.0
        right_density = float(right.mean()) if right.size else 0.0
        side_min = min(left_density, right_density)
        side_max = max(left_density, right_density)
        side_ratio = side_max / max(side_min, 1e-6)
        seam_active_rows = float((seam.mean(axis=1) > 0.025).mean()) if seam.size else 1.0
        left_active_rows = float((left.mean(axis=1) > 0.003).mean()) if left.size else 0.0
        right_active_rows = float((right.mean(axis=1) > 0.003).mean()) if right.size else 0.0
        central_left = round(width * 0.40)
        central_right = round(width * 0.60)
        central_profile = profile[central_left:central_right]
        vertical_peak = float(central_profile.max()) if central_profile.size else 0.0
        vertical_peak_x = (
            central_left + int(np.argmax(central_profile))
            if central_profile.size
            else split
        )

        # A row with a long contiguous run through the gutter is a printed
        # cross-line, not evidence of a column seam.  Restrict this one check
        # to the question body: page rectification can turn the bottom border
        # into a thick full-width component, while the other evidence below
        # still accounts for content in the bottom margin.
        t = _DOUBLE_EVIDENCE_THRESHOLDS
        cross_line_scan_bottom = max(
            0,
            min(
                body.shape[0],
                round(height * float(t["cross_line_scan_bottom_fraction"])) - top,
            ),
        )
        cross_line_rows = 0
        for row in body[:cross_line_scan_bottom]:
            run = 0
            longest = 0
            for value in row.tolist():
                if value:
                    run += 1
                    longest = max(longest, run)
                else:
                    run = 0
            if longest >= max(50, round(width * 0.55)):
                cross_line_rows += 1
        cross_line_fraction = cross_line_rows / max(1, cross_line_scan_bottom)

        # Connected components that genuinely straddle the proposed gutter are
        # usually a question number, answer box, or other content crossing the
        # seam.  Very wide/full-height components are handled by the line
        # checks above and are not double-counted here.
        crossing_components = 0
        try:
            component_count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
                ink.astype(np.uint8), connectivity=8
            )
            for component_index in range(1, component_count):
                x, _y, component_width, component_height, _area = stats[component_index]
                component_right = int(x + component_width)
                if (
                    int(x) < split
                    and component_right > split
                    and component_width < round(width * 0.45)
                    and component_height < round(height * 0.80)
                ):
                    crossing_components += 1
        except Exception:
            crossing_components = 0

        split_1000 = int(round(split * 1000 / width))
        center_offset = abs(split_1000 - 500)
        broad_left = round(width * 0.20)
        broad_right = round(width * 0.80)
        broad_profile = smoothed[broad_left:broad_right]
        broad_min_x = (
            broad_left + int(np.argmin(broad_profile))
            if broad_profile.size
            else split
        )
        broad_min_1000 = int(round(broad_min_x * 1000 / width))
        reasons: list[str] = []
        if vertical_peak >= t["vertical_peak_density_max"]:
            reasons.append("printed_vertical_line_or_content_crossing_gutter")
        if seam_active_rows >= t["seam_active_rows_max"] and seam_density >= max(
            t["seam_density_floor"], side_min * t["seam_to_side_ratio_max"]
        ):
            if "printed_vertical_line_or_content_crossing_gutter" not in reasons:
                reasons.append("printed_vertical_line_or_content_crossing_gutter")
        if cross_line_fraction > t["cross_line_fraction_max"]:
            reasons.append("printed_line_crosses_gutter")
        if crossing_components:
            reasons.append("question_or_answer_crosses_boundary")
        if (
            side_min < t["side_density_min"]
            or min(left_active_rows, right_active_rows) < t["side_active_rows_min"]
        ):
            reasons.append("insufficient_two_sided_content")
        if side_ratio > t["left_right_ratio_max"]:
            reasons.append("left_right_content_ratio_unbalanced")
        if center_offset > t["center_offset_1000_max"]:
            reasons.append("off_center_boundary")
        # A clear valley well outside the central band is a valid observation
        # but not a safe automatic double split: require teacher confirmation
        # so a non-equal page cannot be silently clipped.
        if (
            broad_min_1000 < t["broad_valley_1000_min"]
            or broad_min_1000 > t["broad_valley_1000_max"]
        ):
            if float(smoothed[broad_min_x]) <= t["broad_valley_density_max"]:
                reasons.append("off_center_boundary")
        safe = not reasons
        # Confidence is deliberately capped below certainty.  The teacher
        # confirmation page remains the authority even for a clear gutter.
        confidence = 0.92
        confidence -= min(0.35, seam_density * 2.0)
        confidence -= min(0.20, max(0.0, side_ratio - 1.0) * 0.08)
        confidence -= min(0.20, center_offset / 500.0)
        if not safe:
            confidence = min(confidence, 0.39)
        evidence.update({
            "split_1000": split_1000,
            "gutter_density": round(seam_density, 6),
            "left_density": round(left_density, 6),
            "right_density": round(right_density, 6),
            "left_right_ratio": round(side_ratio, 4),
            "seam_active_rows": round(seam_active_rows, 4),
            "left_active_rows": round(left_active_rows, 4),
            "right_active_rows": round(right_active_rows, 4),
            "cross_line_fraction": round(cross_line_fraction, 6),
            "cross_line_scan_bottom_y": top + cross_line_scan_bottom,
            "crossing_components": crossing_components,
            "center_offset_1000": center_offset,
            "broad_valley_1000": broad_min_1000,
            "vertical_peak_density": round(vertical_peak, 6),
            "vertical_peak_x1000": int(round(vertical_peak_x * 1000 / width)),
            "reasons": reasons,
            "confidence": round(max(0.0, min(1.0, confidence)), 4),
            "confidence_level": "high" if safe else "low",
            "validation": "passed" if safe else "failed",
        })
        return evidence
    except Exception as exc:
        # Do not leak paths or decoder details into a teacher-facing response.
        evidence["reasons"] = ["evidence_unavailable"]
        evidence["error_type"] = type(exc).__name__
        return evidence


def _inspect_local_triple_evidence(
    image_path: str | os.PathLike[str],
    columns: list[dict[str, int]],
) -> dict[str, Any]:
    """Collect per-column content evidence for a model-proposed triple layout.

    The model only proposes the two seams.  Before a proposal may freeze crops,
    every column must contain printed content and every seam must be a
    low-ink valley with no printed line running through it.  The thresholds
    are copied into the record so the decision stays auditable.
    """
    import cv2
    import numpy as np
    from PIL import Image

    t = _TRIPLE_EVIDENCE_THRESHOLDS
    evidence: dict[str, Any] = {
        "evidence_version": _TRIPLE_EVIDENCE_VERSION,
        "strategy": "local_triple_content_v1",
        "thresholds": dict(t),
        "confidence": 0.0,
        "confidence_level": "low",
        "validation": "failed",
        "reasons": [],
        "columns": [],
        "seams": [],
    }
    try:
        with Image.open(image_path) as opened:
            source_width, source_height = opened.size
            gray = np.asarray(opened.convert("L"))
        if source_width < _LOCAL_LAYOUT_MIN_SOURCE_WIDTH:
            evidence["reasons"].append("source_too_narrow")
            return evidence
        scale = min(1.0, _LOCAL_LAYOUT_MAX_WIDTH / float(source_width))
        if scale < 1.0:
            gray = cv2.resize(
                gray,
                (max(1, round(source_width * scale)), max(1, round(source_height * scale))),
                interpolation=cv2.INTER_AREA,
            )
        height, width = gray.shape
        if width < 400 or height < 200:
            evidence["reasons"].append("source_too_small")
            return evidence
        background = cv2.GaussianBlur(
            gray,
            (0, 0),
            sigmaX=max(4.0, width / 100.0),
            sigmaY=max(4.0, width / 100.0),
        )
        ink = (
            (gray.astype(np.int16) + 18 < background.astype(np.int16))
            & (gray < 210)
        )
        body = ink[round(height * 0.12):round(height * 0.95)]
        if body.size == 0:
            evidence["reasons"].append("empty_body")
            return evidence

        reasons: list[str] = []
        pad = max(2, round(width * 0.012))
        side_densities: list[float] = []
        for column in columns:
            left = round(int(column["x1_1000"]) * width / 1000)
            right = round(int(column["x2_1000"]) * width / 1000)
            inner = body[:, min(right - 1, left + pad):max(left + 1, right - pad)]
            density = float(inner.mean()) if inner.size else 0.0
            active_rows = float((inner.mean(axis=1) > 0.003).mean()) if inner.size else 0.0
            side_densities.append(density)
            evidence["columns"].append({
                "column_index": int(column["column_index"]),
                "density": round(density, 6),
                "active_rows": round(active_rows, 4),
            })
            if density < t["column_density_min"] or active_rows < t["column_active_rows_min"]:
                reasons.append(f"insufficient_column_content:col_{int(column['column_index'])}")

        for position, column in enumerate(columns[:-1]):
            boundary = round(int(column["x2_1000"]) * width / 1000)
            seam = body[:, max(0, boundary - pad):min(width, boundary + pad + 1)]
            seam_density = float(seam.mean()) if seam.size else 1.0
            neighbours = min(side_densities[position], side_densities[position + 1])
            # A row whose ink runs continuously across the seam is a printed
            # line, not a gutter.
            cross_rows = 0
            for row in seam:
                if row.size and bool(row.all()):
                    cross_rows += 1
            cross_fraction = cross_rows / max(1, seam.shape[0])
            evidence["seams"].append({
                "boundary_1000": int(column["x2_1000"]),
                "density": round(seam_density, 6),
                "cross_line_fraction": round(cross_fraction, 6),
            })
            if seam_density > t["seam_density_max"] or (
                neighbours > 0 and seam_density > neighbours * t["seam_to_side_ratio_max"]
            ):
                reasons.append(f"seam_not_blank:{int(column['x2_1000'])}")
            if cross_fraction > t["cross_line_fraction_max"]:
                reasons.append("printed_line_crosses_gutter")

        safe = not reasons
        confidence = 0.9 if safe else 0.3
        if side_densities:
            spread = max(side_densities) / max(min(side_densities), 1e-6)
            confidence -= min(0.2, max(0.0, spread - 1.0) * 0.05)
        evidence.update({
            "reasons": reasons,
            "confidence": round(max(0.0, min(1.0, confidence)), 4),
            "confidence_level": "high" if safe else "low",
            "validation": "passed" if safe else "failed",
        })
        return evidence
    except Exception as exc:
        evidence["reasons"] = ["evidence_unavailable"]
        evidence["error_type"] = type(exc).__name__
        return evidence


def _double_layout_draft(
    image_path: str | os.PathLike[str],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Return a code-proposed double layout and its review evidence."""
    evidence = _inspect_local_double_evidence(image_path)
    try:
        inferred = _infer_local_two_column_payload(image_path)
    except Exception:
        inferred = None
    if inferred is None:
        return None, evidence
    payload, diagnostics = inferred
    merged = {**diagnostics, **evidence}
    if evidence.get("validation") != "passed":
        return payload, merged
    for candidate in payload.get("candidates", []):
        candidate["coordinate_trust"] = "local_high_confidence"
    return payload, merged
