"""Teacher-drawn references, never inferred from student strokes.

All geometry is in the full template's normalized_1000_v1 coordinates.
Alternatives are complete independent answers, not masks to union together.
"""
from __future__ import annotations

import hashlib
import json
import math

import cv2
import numpy as np


REFERENCE_VERSION = "drawing-reference-v1"
COORDINATE_SPACE = "normalized_1000_v1"
OVERLAY_COLOR = (0, 160, 240)
OVERLAY_ALPHA = 0.45


def _coordinates(raw, size: int, name: str) -> list[float]:
    if not isinstance(raw, list) or len(raw) != size:
        raise ValueError(f"{name} must contain {size} coordinates")
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not 0 <= value <= 1000 or not math.isfinite(value) for value in raw):
        raise ValueError(f"{name} coordinates must be finite numbers in 0..1000")
    return [float(value) for value in raw]


def _geometry(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("drawing_reference must be an object")
    allowed = {"version", "coordinate_space", "comparison_mode", "variants", "context_regions",
               "teacher_confirmed", "reference_hash"}
    if set(raw) - allowed:
        raise ValueError("drawing_reference contains unsupported fields")
    if raw.get("version") != REFERENCE_VERSION or raw.get("coordinate_space") != COORDINATE_SPACE:
        raise ValueError("unsupported drawing reference version or coordinate space")
    mode = raw.get("comparison_mode", "manual")
    if mode not in ("fixed", "manual"):
        raise ValueError("comparison_mode must be fixed or manual")
    variants = raw.get("variants")
    if not isinstance(variants, list) or not (0 if mode == "manual" else 1) <= len(variants) <= 4:
        raise ValueError("drawing reference requires at most 4 complete variants; fixed requires one")
    normalized_variants = []
    for variant in variants:
        if not isinstance(variant, dict) or set(variant) != {"strokes"}:
            raise ValueError("each reference variant must contain only strokes")
        strokes = variant["strokes"]
        if not isinstance(strokes, list) or not 1 <= len(strokes) <= 64:
            raise ValueError("each variant requires 1..64 strokes")
        normalized_strokes = []
        for stroke in strokes:
            if not isinstance(stroke, dict):
                raise ValueError("reference stroke must be an object")
            if stroke.get("kind") == "point" and set(stroke) == {"kind", "point"}:
                normalized_strokes.append({"kind": "point", "point": _coordinates(stroke["point"], 2, "point")})
            elif stroke.get("kind") == "polyline" and set(stroke) == {"kind", "points"}:
                points = stroke["points"]
                if not isinstance(points, list) or not 2 <= len(points) <= 128:
                    raise ValueError("polyline requires 2..128 points")
                points = [_coordinates(point, 2, "polyline point") for point in points]
                if any(a == b for a, b in zip(points, points[1:])):
                    raise ValueError("polyline must not contain zero-length segments")
                normalized_strokes.append({"kind": "polyline", "points": points})
            elif stroke.get("kind") == "ellipse" and set(stroke) == {"kind", "center", "radii", "fill"}:
                center = _coordinates(stroke["center"], 2, "ellipse center")
                radii = _coordinates(stroke["radii"], 2, "ellipse radii")
                if (type(stroke["fill"]) is not bool or any(radius <= 0 for radius in radii)
                        or any(c - r < 0 or c + r > 1000 for c, r in zip(center, radii))):
                    raise ValueError("ellipse must have positive radii and fit the template")
                normalized_strokes.append({"kind": "ellipse", "center": center, "radii": radii, "fill": stroke["fill"]})
            elif stroke.get("kind") in {"polygon", "brush"}:
                kind = stroke["kind"]
                fields = {"kind", "points", "width"} if kind == "brush" else {"kind", "points"}
                points = stroke.get("points")
                if set(stroke) != fields or not isinstance(points, list) or not (1 if kind == "brush" else 3) <= len(points) <= 128:
                    raise ValueError("invalid filled reference stroke")
                points = [_coordinates(point, 2, "filled stroke point") for point in points]
                if any(a == b for a, b in zip(points, points[1:])):
                    raise ValueError("filled stroke must not contain repeated adjacent points")
                if kind == "polygon":
                    area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(points, points[1:] + points[:1]))
                    if abs(area) < 0.01:
                        raise ValueError("filled polygon must have positive area")
                    normalized_strokes.append({"kind": kind, "points": points})
                else:
                    width = stroke["width"]
                    if type(width) not in (int, float) or not math.isfinite(width) or not 1 <= width <= 80:
                        raise ValueError("brush width must be in 1..80 normalized short-edge units")
                    normalized_strokes.append({"kind": kind, "points": points, "width": float(width)})
            else:
                raise ValueError("unsupported reference stroke")
        normalized_variants.append({"strokes": normalized_strokes})
    regions = raw.get("context_regions", [])
    if not isinstance(regions, list) or len(regions) > 64:
        raise ValueError("context_regions must contain at most 64 boxes")
    normalized_regions = []
    for box in regions:
        box = _coordinates(box, 4, "context region")
        if box[0] >= box[2] or box[1] >= box[3]:
            raise ValueError("context region must have positive area")
        normalized_regions.append(box)
    return {"version": REFERENCE_VERSION, "coordinate_space": COORDINATE_SPACE,
            "comparison_mode": mode, "variants": normalized_variants, "context_regions": normalized_regions}


def reference_digest(reference: object) -> str:
    """Hash normalized content, never a caller-supplied hash or confirmation."""
    encoded = json.dumps(_geometry(reference), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_reference(raw: object, *, teacher_confirmed: bool = False,
                        trusted_confirmed: bool | None = None) -> dict:
    """Caller owns authorization; model fields can never grant confirmation.

    Candidate callers leave both flags false. Only a server confirmation or a
    previously verified stored contract may explicitly pass trusted_confirmed.
    """
    if not isinstance(teacher_confirmed, bool) or (
        trusted_confirmed is not None and not isinstance(trusted_confirmed, bool)
    ):
        raise ValueError("reference confirmation must be an explicit boolean")
    geometry = _geometry(raw)
    geometry["teacher_confirmed"] = teacher_confirmed if trusted_confirmed is None else trusted_confirmed
    geometry["reference_hash"] = reference_digest(geometry)
    return geometry


def validate_confirmed_reference(raw: object, *, require_fixed: bool = False) -> dict:
    """Read a stored confirmed object without repairing a stale/tampered digest.

    This verifies integrity only. The caller must first verify teacher/page
    ownership and the frozen template contract containing this reference.
    """
    if not isinstance(raw, dict) or raw.get("teacher_confirmed") is not True:
        raise ValueError("drawing reference has not been teacher confirmed")
    normalized = normalize_reference(raw, trusted_confirmed=True)
    if raw.get("reference_hash") != normalized["reference_hash"]:
        raise ValueError("confirmed drawing reference content hash mismatch")
    if require_fixed and normalized["comparison_mode"] != "fixed":
        raise ValueError("drawing reference requires manual comparison")
    return normalized


def _dimensions(width: int, height: int) -> None:
    if any(isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 16384
           for value in (width, height)) or width * height > 40_000_000:
        raise ValueError("reference canvas dimensions are invalid or too large")


def render_reference_mask(reference: object, width: int, height: int, variant_index: int = 0) -> np.ndarray:
    """Render one complete alternative as white ink on a black full-page mask."""
    _dimensions(width, height)
    geometry = _geometry(reference)
    if (isinstance(variant_index, bool) or not isinstance(variant_index, int)
            or not 0 <= variant_index < len(geometry["variants"])):
        raise ValueError("reference variant index is invalid")
    scale = min(width / 1000, height / 820)
    point_radius = max(1, round(5.5 * scale))
    line_width = max(1, round(4.5 * scale))
    mask = np.zeros((height, width), dtype=np.uint8)

    def pixel(point):
        return (min(width - 1, round(point[0] * width / 1000)),
                min(height - 1, round(point[1] * height / 1000)))

    for stroke in geometry["variants"][variant_index]["strokes"]:
        if stroke["kind"] == "point":
            cv2.circle(mask, pixel(stroke["point"]), point_radius, 255, -1, lineType=cv2.LINE_8)
        elif stroke["kind"] == "ellipse":
            radii = (max(1, round(stroke["radii"][0] * width / 1000)),
                     max(1, round(stroke["radii"][1] * height / 1000)))
            cv2.ellipse(mask, pixel(stroke["center"]), radii, 0, 0, 360, 255,
                        -1 if stroke["fill"] else line_width, lineType=cv2.LINE_8)
        elif stroke["kind"] == "polygon":
            cv2.fillPoly(mask, [np.array([pixel(point) for point in stroke["points"]], dtype=np.int32)], 255)
        elif stroke["kind"] == "brush":
            points = [pixel(point) for point in stroke["points"]]
            brush_width = max(1, round(stroke["width"] * min(width, height) / 1000))
            cv2.polylines(mask, [np.array(points, dtype=np.int32)], False, 255, brush_width, lineType=cv2.LINE_8)
            for point in points:
                cv2.circle(mask, point, max(1, brush_width // 2), 255, -1, lineType=cv2.LINE_8)
        else:
            points = [pixel(point) for point in stroke["points"]]
            cv2.polylines(mask, [np.array(points, dtype=np.int32)], False, 255,
                          line_width, lineType=cv2.LINE_8)
    return mask


def render_display_mask(reference: object, width: int, height: int, variant_index: int = 0) -> np.ndarray:
    """Widen only the reference, with the frozen 9x9 elliptical rule at base size."""
    mask = render_reference_mask(reference, width, height, variant_index)
    radius = max(1, round(4 * min(width / 1000, height / 820)))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1))
    return cv2.dilate(mask, kernel, iterations=1, borderType=cv2.BORDER_CONSTANT, borderValue=0)


def render_overlay(student_rgb: np.ndarray, reference: object, variant_index: int = 0,
                   *, alpha: float = OVERLAY_ALPHA, color=OVERLAY_COLOR) -> np.ndarray:
    """Multiply a cyan reference onto a COPY; exact black student ink stays black."""
    if (not isinstance(student_rgb, np.ndarray) or student_rgb.dtype != np.uint8
            or student_rgb.ndim != 3 or student_rgb.shape[2] != 3):
        raise ValueError("student_rgb must be a uint8 RGB image")
    if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) or not math.isfinite(alpha) or not 0 < alpha < 1:
        raise ValueError("overlay alpha must be finite and strictly between zero and one")
    if (not isinstance(color, (list, tuple)) or len(color) != 3
            or any(isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255 for value in color)):
        raise ValueError("overlay color must be three RGB bytes")
    height, width = student_rgb.shape[:2]
    mask = render_display_mask(reference, width, height, variant_index)
    factor = (1 - alpha) + alpha * np.array(color, dtype=np.float64) / 255
    result = student_rgb.copy()
    result[mask != 0] = np.rint(student_rgb[mask != 0].astype(np.float64) * factor).astype(np.uint8)
    return result
