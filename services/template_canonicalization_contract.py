"""Pure-Python contract checks for client-canonicalized template pages."""
from __future__ import annotations

import math
from typing import Any


CANONICAL_TEMPLATE_CONTRACT_VERSION = "client-canonical-template-v1"
CANONICALIZATION_STAGE = "template_canonicalization"
CANONICALIZATION_VERSION = "paper-canonical-v1"
CANONICALIZATION_ALGORITHM = "opencv_page_quad_v1"
CANONICAL_SOURCE_COORDINATE_SPACE = "exif_display_pixels_top_left"
CANONICAL_COORDINATE_SPACE = "canonical_template_v1"
CANONICAL_PAGE_LONG_EDGE = 2000
CANONICAL_PAGE_MARGIN = 32
MAX_SOURCE_PIXELS = 16_000_000

_METADATA_KEYS = {
    "version",
    "algorithm",
    "source_sha256",
    "source_format",
    "exif_orientation",
    "encoded_width",
    "encoded_height",
    "source_width",
    "source_height",
    "source_coordinate_space",
    "canonical_coordinate_space",
    "source_to_canonical",
    "detected_page_quad",
    "detection_method",
    "confidence",
    "page_margin_px",
    "canonical_sha256",
    "canonical_width",
    "canonical_height",
}


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"canonicalization {field} is invalid")
    return value


def _finite_number(value: Any, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"canonicalization {field} is invalid")
    return float(value)


def _matrix3(value: Any) -> list[list[float]]:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("canonicalization source_to_canonical is invalid")
    matrix: list[list[float]] = []
    for row in value:
        if not isinstance(row, list) or len(row) != 3:
            raise ValueError("canonicalization source_to_canonical is invalid")
        matrix.append([
            _finite_number(item, "source_to_canonical") for item in row
        ])
    denominator = matrix[2][2]
    if abs(denominator) < 1e-12:
        raise ValueError("canonicalization source_to_canonical is invalid")
    matrix = [[item / denominator for item in row] for row in matrix]
    determinant = (
        matrix[0][0] * (matrix[1][1] * matrix[2][2] - matrix[1][2] * matrix[2][1])
        - matrix[0][1] * (matrix[1][0] * matrix[2][2] - matrix[1][2] * matrix[2][0])
        + matrix[0][2] * (matrix[1][0] * matrix[2][1] - matrix[1][1] * matrix[2][0])
    )
    if not math.isfinite(determinant) or abs(determinant) < 1e-9:
        raise ValueError("canonicalization source_to_canonical is invalid")
    return matrix


def _quad(value: Any, *, source_width: int, source_height: int) -> list[list[float]]:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError("canonicalization detected_page_quad is invalid")
    points: list[list[float]] = []
    for point in value:
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError("canonicalization detected_page_quad is invalid")
        x = _finite_number(point[0], "detected_page_quad")
        y = _finite_number(point[1], "detected_page_quad")
        if not (-1.0 <= x <= source_width and -1.0 <= y <= source_height):
            raise ValueError("canonicalization detected_page_quad is out of bounds")
        points.append([x, y])

    cross_products: list[float] = []
    for index in range(4):
        current = points[index]
        following = points[(index + 1) % 4]
        after = points[(index + 2) % 4]
        cross_products.append(
            (following[0] - current[0]) * (after[1] - following[1])
            - (following[1] - current[1]) * (after[0] - following[0])
        )
    if any(value <= 1e-6 for value in cross_products):
        raise ValueError("canonicalization detected_page_quad is invalid")
    return points


def _project(matrix: list[list[float]], point: list[float]) -> tuple[float, float]:
    x, y = point
    denominator = matrix[2][0] * x + matrix[2][1] * y + matrix[2][2]
    if not math.isfinite(denominator) or abs(denominator) < 1e-12:
        raise ValueError("canonicalization detected_page_quad projection is invalid")
    projected_x = (matrix[0][0] * x + matrix[0][1] * y + matrix[0][2]) / denominator
    projected_y = (matrix[1][0] * x + matrix[1][1] * y + matrix[1][2]) / denominator
    if not math.isfinite(projected_x) or not math.isfinite(projected_y):
        raise ValueError("canonicalization detected_page_quad projection is invalid")
    return projected_x, projected_y


def validate_canonicalization_metadata(
    value: Any,
    *,
    source_sha256: str,
    canonical_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Validate hashes and geometry without decoding either image."""

    if not isinstance(value, dict) or set(value) != _METADATA_KEYS:
        raise ValueError("canonicalization metadata has invalid fields")
    if (
        value.get("version") != CANONICALIZATION_VERSION
        or value.get("algorithm") != CANONICALIZATION_ALGORITHM
        or value.get("source_coordinate_space")
        != CANONICAL_SOURCE_COORDINATE_SPACE
        or value.get("canonical_coordinate_space") != CANONICAL_COORDINATE_SPACE
    ):
        raise ValueError("canonicalization metadata contract is unsupported")
    if value.get("source_sha256") != source_sha256:
        raise ValueError("canonicalization source_sha256 mismatch")
    if value.get("canonical_sha256") != canonical_manifest.get("sha256"):
        raise ValueError("canonicalization canonical_sha256 mismatch")

    source_format = value.get("source_format")
    if source_format not in {"jpeg", "png"}:
        raise ValueError("canonicalization source_format is invalid")
    exif_orientation = _positive_int(value.get("exif_orientation"), "exif_orientation")
    if exif_orientation > 8:
        raise ValueError("canonicalization exif_orientation is invalid")
    encoded_width = _positive_int(value.get("encoded_width"), "encoded_width")
    encoded_height = _positive_int(value.get("encoded_height"), "encoded_height")
    source_width = _positive_int(value.get("source_width"), "source_width")
    source_height = _positive_int(value.get("source_height"), "source_height")
    if (
        encoded_width * encoded_height > MAX_SOURCE_PIXELS
        or source_width * source_height > MAX_SOURCE_PIXELS
    ):
        raise ValueError("canonicalization source dimensions are too large")
    expected_display = (
        (encoded_height, encoded_width)
        if exif_orientation in {5, 6, 7, 8}
        else (encoded_width, encoded_height)
    )
    if (source_width, source_height) != expected_display:
        raise ValueError("canonicalization source dimensions are inconsistent")

    canonical_width = _positive_int(value.get("canonical_width"), "canonical_width")
    canonical_height = _positive_int(value.get("canonical_height"), "canonical_height")
    if (
        canonical_width != canonical_manifest.get("width")
        or canonical_height != canonical_manifest.get("height")
    ):
        raise ValueError("canonicalization canonical dimensions mismatch")
    margin = value.get("page_margin_px")
    if isinstance(margin, bool) or margin != CANONICAL_PAGE_MARGIN:
        raise ValueError("canonicalization page_margin_px is invalid")
    page_width = canonical_width - 2 * margin
    page_height = canonical_height - 2 * margin
    if (
        min(page_width, page_height) <= 0
        or max(page_width, page_height) != CANONICAL_PAGE_LONG_EDGE
        or not 0.30 <= page_width / float(page_height) <= 3.30
    ):
        raise ValueError("canonicalization canonical dimensions are invalid")

    matrix = _matrix3(value.get("source_to_canonical"))
    page_quad = _quad(
        value.get("detected_page_quad"),
        source_width=source_width,
        source_height=source_height,
    )
    expected_quad = [
        [float(margin), float(margin)],
        [float(canonical_width - margin - 1), float(margin)],
        [
            float(canonical_width - margin - 1),
            float(canonical_height - margin - 1),
        ],
        [float(margin), float(canonical_height - margin - 1)],
    ]
    for point, expected in zip(page_quad, expected_quad, strict=True):
        projected = _project(matrix, point)
        if math.hypot(projected[0] - expected[0], projected[1] - expected[1]) > 2.0:
            raise ValueError("canonicalization detected_page_quad projection is invalid")

    detection_method = value.get("detection_method")
    if detection_method not in {"detected_quadrilateral", "full_frame_scan"}:
        raise ValueError("canonicalization detection_method is invalid")
    confidence = _finite_number(value.get("confidence"), "confidence")
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("canonicalization confidence is invalid")

    return {
        "version": CANONICALIZATION_VERSION,
        "algorithm": CANONICALIZATION_ALGORITHM,
        "source_sha256": source_sha256,
        "source_format": source_format,
        "exif_orientation": exif_orientation,
        "encoded_width": encoded_width,
        "encoded_height": encoded_height,
        "source_width": source_width,
        "source_height": source_height,
        "source_coordinate_space": CANONICAL_SOURCE_COORDINATE_SPACE,
        "canonical_coordinate_space": CANONICAL_COORDINATE_SPACE,
        "source_to_canonical": matrix,
        "detected_page_quad": page_quad,
        "detection_method": detection_method,
        "confidence": confidence,
        "page_margin_px": margin,
        "canonical_sha256": canonical_manifest["sha256"],
        "canonical_width": canonical_width,
        "canonical_height": canonical_height,
    }
