from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageOps


CANONICALIZATION_VERSION = "paper-canonical-v1"
CANONICALIZATION_ALGORITHM = "opencv_page_quad_v1"
CANONICAL_COORDINATE_SYSTEM = "canonical_template_v1"
CANONICAL_SOURCE_COORDINATE_SYSTEM = "exif_display_pixels_top_left"
CANONICAL_PAGE_LONG_EDGE = 2000
CANONICAL_PAGE_MARGIN = 32
MAX_IMAGE_EDGE = 2200
MAX_SOURCE_PIXELS = 16_000_000

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_SUFFIXES = {".jpg", ".jpeg", ".png"}
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


class TemplateCanonicalizationError(ValueError):
    """A bounded failure that is safe to return over the sidecar protocol."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CanonicalTemplateResult:
    canonical_path: Path
    canonicalization: dict[str, Any]

    def protocol_payload(self) -> dict[str, Any]:
        return {
            "canonicalPath": str(self.canonical_path),
            "canonicalization": self.canonicalization,
        }


@dataclass(frozen=True)
class _LoadedTemplateSource:
    rgb: np.ndarray
    source_format: str
    encoded_width: int
    encoded_height: int
    display_width: int
    display_height: int
    exif_orientation: int
    display_to_work_x: float
    display_to_work_y: float


def _raise(code: str) -> None:
    raise TemplateCanonicalizationError(code)


def _provided_absolute_path(value: object, error_code: str) -> Path:
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 32767
        or "\x00" in value
        or value.startswith("\\\\")
    ):
        _raise(error_code)
    path = Path(value)
    if not path.is_absolute():
        _raise(error_code)
    return path


def _inside(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve_paths(
    workspace_root: object,
    source_path: object,
    canonical_path: object,
) -> tuple[Path, Path, Path]:
    root_value = _provided_absolute_path(workspace_root, "workspace_root_invalid")
    try:
        root = root_value.resolve(strict=True)
    except (OSError, RuntimeError):
        _raise("workspace_root_invalid")
    if not root.is_dir() or root.parent == root:
        _raise("workspace_root_invalid")

    source_value = _provided_absolute_path(source_path, "source_path_invalid")
    try:
        source = source_value.resolve(strict=True)
    except (OSError, RuntimeError):
        _raise("source_path_invalid")
    if not source.is_file() or source.suffix.lower() not in _SOURCE_SUFFIXES:
        _raise("source_path_invalid")

    output_value = _provided_absolute_path(canonical_path, "canonical_path_invalid")
    if output_value.suffix.lower() != ".png":
        _raise("canonical_path_invalid")
    try:
        output = output_value.resolve(strict=False)
    except (OSError, RuntimeError):
        _raise("canonical_path_invalid")

    if not _inside(root, source) or not _inside(root, output):
        _raise("path_outside_workspace")
    if source == output or (output.exists() and not output.is_file()):
        _raise("canonical_path_invalid")
    return root, source, output


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    image = ImageOps.exif_transpose(image)
    if image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        return Image.alpha_composite(background, rgba).convert("RGB")
    return image.convert("RGB")


def _resize_long_edge(image: Image.Image, max_edge: int = MAX_IMAGE_EDGE) -> Image.Image:
    width, height = image.size
    if width <= 0 or height <= 0:
        raise ValueError("invalid_dimensions")
    longest = max(width, height)
    if longest <= max_edge:
        return image
    scale = max_edge / float(longest)
    target = (
        max(1, int(round(width * scale))),
        max(1, int(round(height * scale))),
    )
    return image.resize(target, Image.Resampling.LANCZOS)


def _load_template_source(path: Path) -> tuple[_LoadedTemplateSource, str]:
    try:
        source_sha256 = _sha256_file(path)
        with Image.open(path) as source:
            source_format = str(source.format or "").upper()
            if source_format == "MPO":
                # Phone .jpg files may be MPO containers.  Canonicalize only
                # the primary frame and expose the existing JPEG contract.
                source.seek(0)
                source_format = "JPEG"
            if source_format not in {"JPEG", "PNG"}:
                _raise("template_source_invalid")
            encoded_width, encoded_height = source.size
            if (
                encoded_width <= 0
                or encoded_height <= 0
                or encoded_width * encoded_height > MAX_SOURCE_PIXELS
            ):
                _raise("template_source_invalid")
            exif_orientation = int(source.getexif().get(274, 1) or 1)
            if exif_orientation < 1 or exif_orientation > 8:
                exif_orientation = 1
            source.load()
            display = _flatten_to_rgb(source)
            display_width, display_height = display.size
            work = _resize_long_edge(display)
            work_width, work_height = work.size
            loaded = _LoadedTemplateSource(
                rgb=np.ascontiguousarray(np.asarray(work, dtype=np.uint8)),
                source_format=source_format,
                encoded_width=encoded_width,
                encoded_height=encoded_height,
                display_width=display_width,
                display_height=display_height,
                exif_orientation=exif_orientation,
                display_to_work_x=work_width / float(display_width),
                display_to_work_y=work_height / float(display_height),
            )
        if _sha256_file(path) != source_sha256:
            _raise("template_source_changed")
        return loaded, source_sha256
    except TemplateCanonicalizationError:
        raise
    except (OSError, TypeError, ValueError, Image.DecompressionBombError):
        _raise("template_source_invalid")


def _order_page_quad(points: np.ndarray) -> np.ndarray:
    quad = np.asarray(points, dtype=np.float32).reshape(4, 2)
    ordered = np.empty((4, 2), dtype=np.float32)
    sums = quad.sum(axis=1)
    differences = quad[:, 0] - quad[:, 1]
    ordered[0] = quad[int(np.argmin(sums))]
    ordered[1] = quad[int(np.argmax(differences))]
    ordered[2] = quad[int(np.argmax(sums))]
    ordered[3] = quad[int(np.argmin(differences))]
    if len({tuple(float(value) for value in point) for point in ordered}) != 4:
        raise ValueError("ambiguous_boundary")
    return ordered


def _page_content_metrics(gray: np.ndarray) -> tuple[float, float]:
    inset_y = max(2, gray.shape[0] // 50)
    inset_x = max(2, gray.shape[1] // 50)
    content = gray[inset_y:-inset_y, inset_x:-inset_x]
    if content.size == 0:
        content = gray
    return float(np.mean(content < 220)), float(np.std(content))


def _full_frame_page_confidence(gray: np.ndarray) -> float:
    border_width = max(3, min(gray.shape[:2]) // 100)
    borders = (
        gray[:border_width, :].ravel(),
        gray[-border_width:, :].ravel(),
        gray[:, :border_width].ravel(),
        gray[:, -border_width:].ravel(),
    )
    light_fractions = [float(np.mean(border > 185)) for border in borders]
    border_means = [float(np.mean(border)) for border in borders]
    dark_ratio, contrast = _page_content_metrics(gray)
    if (
        min(light_fractions) < 0.90
        or min(border_means) < 205.0
        or dark_ratio < 0.0015
        or contrast < 5.0
    ):
        return 0.0
    return min(0.94, 0.82 + min(0.10, dark_ratio * 2.0))


def _full_frame_content_confidence(gray: np.ndarray) -> float:
    """Allow an already-cropped template to use the complete image as its page.

    Scanner output and tightly cropped teacher templates often contain no visible
    paper corners or bright border.  The image boundary is still a valid template
    coordinate system as long as the page contains usable printed content.
    """

    inset_y = max(2, int(round(gray.shape[0] * 0.15)))
    inset_x = max(2, int(round(gray.shape[1] * 0.15)))
    interior = gray[inset_y:-inset_y, inset_x:-inset_x]
    if interior.size == 0:
        interior = gray
    dark_ratio = float(np.mean(interior < 220))
    contrast = float(np.std(interior))
    if dark_ratio < 0.0015 or contrast < 5.0:
        return 0.0
    return min(0.80, 0.70 + min(0.10, dark_ratio * 2.0))


def _approximate_quad(contour: np.ndarray) -> np.ndarray | None:
    hull = cv2.convexHull(contour)
    perimeter = cv2.arcLength(hull, True)
    if perimeter <= 0:
        return None
    for ratio in (0.015, 0.02, 0.025, 0.03, 0.04, 0.05):
        approximation = cv2.approxPolyDP(hull, ratio * perimeter, True)
        if len(approximation) == 4 and cv2.isContourConvex(approximation):
            return approximation.reshape(4, 2).astype(np.float32)
    return None


def _detect_page_quad(rgb: np.ndarray) -> tuple[np.ndarray, str, float]:
    height, width = rgb.shape[:2]
    if min(width, height) < 160:
        raise ValueError("page_too_small")

    detection_scale = min(1.0, 1200.0 / max(width, height))
    if detection_scale < 1.0:
        detect_rgb = cv2.resize(
            rgb,
            (
                max(1, int(round(width * detection_scale))),
                max(1, int(round(height * detection_scale))),
            ),
            interpolation=cv2.INTER_AREA,
        )
    else:
        detect_rgb = rgb
    gray = cv2.cvtColor(detect_rgb, cv2.COLOR_RGB2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    _threshold, otsu = cv2.threshold(
        blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )
    fixed = cv2.threshold(blurred, 180, 255, cv2.THRESH_BINARY)[1]
    edges = cv2.Canny(blurred, 40, 140)
    kernel_size = max(5, int(round(min(gray.shape) * 0.012)))
    if kernel_size % 2 == 0:
        kernel_size += 1
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_size, kernel_size))
    masks = [
        cv2.morphologyEx(otsu, cv2.MORPH_CLOSE, kernel),
        cv2.morphologyEx(fixed, cv2.MORPH_CLOSE, kernel),
        cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel),
    ]

    image_area = float(gray.shape[0] * gray.shape[1])
    candidates: list[tuple[float, np.ndarray, float]] = []
    for mask in masks:
        contours, _hierarchy = cv2.findContours(
            mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE
        )
        for contour in contours:
            contour_area = abs(float(cv2.contourArea(contour)))
            area_ratio = contour_area / image_area
            if area_ratio < 0.30 or area_ratio > 0.995:
                continue
            raw_quad = _approximate_quad(contour)
            if raw_quad is None:
                continue
            try:
                quad = _order_page_quad(raw_quad)
            except ValueError:
                continue
            polygon_area = abs(float(cv2.contourArea(quad)))
            if polygon_area / image_area < 0.30:
                continue
            side_lengths = [
                float(np.linalg.norm(quad[(index + 1) % 4] - quad[index]))
                for index in range(4)
            ]
            if min(side_lengths) < min(gray.shape) * 0.28:
                continue
            rectangle = cv2.minAreaRect(quad)
            rectangle_area = max(1.0, float(rectangle[1][0] * rectangle[1][1]))
            rectangularity = min(1.0, polygon_area / rectangle_area)
            interior_mask = np.zeros_like(gray)
            cv2.fillConvexPoly(interior_mask, np.rint(quad).astype(np.int32), 255)
            content_inset = max(3, min(gray.shape) // 100)
            content_kernel = cv2.getStructuringElement(
                cv2.MORPH_RECT,
                (content_inset, content_inset),
            )
            content_mask = cv2.erode(interior_mask, content_kernel)
            interior = gray[content_mask == 255]
            if interior.size == 0:
                continue
            if float(np.mean(interior < 220)) < 0.0015 or float(np.std(interior)) < 5.0:
                continue
            brightness = float(np.mean(interior)) / 255.0
            score = area_ratio * 0.55 + rectangularity * 0.30 + brightness * 0.15
            candidates.append((score, quad, area_ratio))

    full_frame_confidence = _full_frame_page_confidence(gray)
    content_frame_confidence = _full_frame_content_confidence(gray)
    if candidates:
        score, quad, _area_ratio = max(candidates, key=lambda item: item[0])
        edge_margin = max(2.0, min(gray.shape) * 0.012)
        touches = [
            point[0] <= edge_margin
            or point[1] <= edge_margin
            or point[0] >= gray.shape[1] - 1 - edge_margin
            or point[1] >= gray.shape[0] - 1 - edge_margin
            for point in quad
        ]
        if any(touches):
            # A contour touching the image edge does not prove that required
            # template content is missing.  Prefer the complete image as the
            # template coordinate system instead of rejecting scanner output.
            pass
        elif score >= 0.68:
            confidence = min(0.99, 0.68 + max(0.0, score - 0.68) * 1.4)
            return quad / float(detection_scale), "detected_quadrilateral", confidence

    fallback_confidence = max(full_frame_confidence, content_frame_confidence)
    if fallback_confidence > 0.0:
        return np.asarray(
            [
                [0.0, 0.0],
                [width - 1.0, 0.0],
                [width - 1.0, height - 1.0],
                [0.0, height - 1.0],
            ],
            dtype=np.float32,
        ), "full_frame_scan", fallback_confidence
    raise ValueError("page_boundary_low_confidence")


def _canonical_page(source: _LoadedTemplateSource) -> tuple[np.ndarray, dict[str, Any]]:
    work_quad, detection_method, confidence = _detect_page_quad(source.rgb)
    top_width = float(np.linalg.norm(work_quad[1] - work_quad[0]))
    bottom_width = float(np.linalg.norm(work_quad[2] - work_quad[3]))
    left_height = float(np.linalg.norm(work_quad[3] - work_quad[0]))
    right_height = float(np.linalg.norm(work_quad[2] - work_quad[1]))
    page_width = max(top_width, bottom_width)
    page_height = max(left_height, right_height)
    if min(page_width, page_height) < 120.0:
        raise ValueError("page_boundary_too_small")
    aspect_ratio = page_width / page_height
    if not 0.30 <= aspect_ratio <= 3.30:
        raise ValueError("page_aspect_ratio_invalid")

    output_scale = CANONICAL_PAGE_LONG_EDGE / max(page_width, page_height)
    canonical_page_width = max(1, int(round(page_width * output_scale)))
    canonical_page_height = max(1, int(round(page_height * output_scale)))
    output_width = canonical_page_width + 2 * CANONICAL_PAGE_MARGIN
    output_height = canonical_page_height + 2 * CANONICAL_PAGE_MARGIN
    destination = np.asarray(
        [
            [CANONICAL_PAGE_MARGIN, CANONICAL_PAGE_MARGIN],
            [CANONICAL_PAGE_MARGIN + canonical_page_width - 1, CANONICAL_PAGE_MARGIN],
            [
                CANONICAL_PAGE_MARGIN + canonical_page_width - 1,
                CANONICAL_PAGE_MARGIN + canonical_page_height - 1,
            ],
            [CANONICAL_PAGE_MARGIN, CANONICAL_PAGE_MARGIN + canonical_page_height - 1],
        ],
        dtype=np.float32,
    )
    work_to_canonical = cv2.getPerspectiveTransform(work_quad, destination)
    if (
        not np.isfinite(work_to_canonical).all()
        or abs(float(np.linalg.det(work_to_canonical))) < 1e-9
    ):
        raise RuntimeError("canonical_transform_invalid")
    canonical = cv2.warpPerspective(
        source.rgb,
        work_to_canonical,
        (output_width, output_height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255),
    )
    display_to_work = np.asarray(
        [
            [source.display_to_work_x, 0.0, 0.0],
            [0.0, source.display_to_work_y, 0.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    source_to_canonical = np.asarray(work_to_canonical, dtype=np.float64) @ display_to_work
    display_quad = work_quad.astype(np.float64)
    display_quad[:, 0] /= source.display_to_work_x
    display_quad[:, 1] /= source.display_to_work_y
    metadata = {
        "version": CANONICALIZATION_VERSION,
        "algorithm": CANONICALIZATION_ALGORITHM,
        "source_coordinate_space": CANONICAL_SOURCE_COORDINATE_SYSTEM,
        "canonical_coordinate_space": CANONICAL_COORDINATE_SYSTEM,
        "source_format": source.source_format.lower(),
        "exif_orientation": source.exif_orientation,
        "encoded_width": source.encoded_width,
        "encoded_height": source.encoded_height,
        "source_width": source.display_width,
        "source_height": source.display_height,
        "source_to_canonical": source_to_canonical.tolist(),
        "detected_page_quad": display_quad.tolist(),
        "detection_method": detection_method,
        "confidence": round(float(confidence), 6),
        "page_margin_px": CANONICAL_PAGE_MARGIN,
    }
    return np.ascontiguousarray(canonical), metadata


def _encode_png(image: np.ndarray) -> bytes:
    stream = io.BytesIO()
    Image.fromarray(image, mode="RGB").save(stream, format="PNG")
    return stream.getvalue()


def _validate_metadata(metadata: dict[str, Any], canonical_png: bytes) -> None:
    if set(metadata) != _METADATA_KEYS:
        _raise("template_metadata_invalid")
    if (
        metadata.get("version") != CANONICALIZATION_VERSION
        or metadata.get("algorithm") != CANONICALIZATION_ALGORITHM
        or metadata.get("source_coordinate_space")
        != CANONICAL_SOURCE_COORDINATE_SYSTEM
        or metadata.get("canonical_coordinate_space") != CANONICAL_COORDINATE_SYSTEM
    ):
        _raise("template_metadata_invalid")
    if not _SHA256_RE.fullmatch(str(metadata.get("source_sha256") or "")):
        _raise("template_metadata_invalid")
    if hashlib.sha256(canonical_png).hexdigest() != metadata.get("canonical_sha256"):
        _raise("template_metadata_invalid")

    try:
        matrix = np.asarray(metadata.get("source_to_canonical"), dtype=np.float64)
        page_quad = np.asarray(metadata.get("detected_page_quad"), dtype=np.float64)
    except (TypeError, ValueError):
        _raise("template_metadata_invalid")
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all():
        _raise("template_metadata_invalid")
    denominator = float(matrix[2, 2])
    if abs(denominator) < 1e-12:
        _raise("template_metadata_invalid")
    matrix = matrix / denominator
    if abs(float(np.linalg.det(matrix))) < 1e-9:
        _raise("template_metadata_invalid")
    try:
        inverse = np.linalg.inv(matrix)
    except np.linalg.LinAlgError:
        _raise("template_metadata_invalid")
    if (
        not np.isfinite(inverse).all()
        or page_quad.shape != (4, 2)
        or not np.isfinite(page_quad).all()
    ):
        _raise("template_metadata_invalid")

    integer_fields = (
        "encoded_width",
        "encoded_height",
        "source_width",
        "source_height",
        "canonical_width",
        "canonical_height",
        "page_margin_px",
        "exif_orientation",
    )
    for field in integer_fields:
        value = metadata.get(field)
        if not isinstance(value, int) or isinstance(value, bool):
            _raise("template_metadata_invalid")
    if (
        min(int(metadata[field]) for field in integer_fields[:6]) <= 0
        or not 1 <= int(metadata["exif_orientation"]) <= 8
        or int(metadata["page_margin_px"]) != CANONICAL_PAGE_MARGIN
    ):
        _raise("template_metadata_invalid")
    confidence = metadata.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(float(confidence))
        or not 0.0 <= float(confidence) <= 1.0
    ):
        _raise("template_metadata_invalid")
    if metadata.get("detection_method") not in {
        "detected_quadrilateral",
        "full_frame_scan",
    }:
        _raise("template_metadata_invalid")

    source_width = int(metadata["source_width"])
    source_height = int(metadata["source_height"])
    if (
        np.any(page_quad[:, 0] < -1.0)
        or np.any(page_quad[:, 0] > source_width)
        or np.any(page_quad[:, 1] < -1.0)
        or np.any(page_quad[:, 1] > source_height)
    ):
        _raise("template_metadata_invalid")
    contour = page_quad.astype(np.float32).reshape(-1, 1, 2)
    if (
        not cv2.isContourConvex(contour)
        or cv2.contourArea(contour, oriented=True) <= 0
    ):
        _raise("template_metadata_invalid")
    margin = int(metadata["page_margin_px"])
    page_width = int(metadata["canonical_width"]) - 2 * margin
    page_height = int(metadata["canonical_height"]) - 2 * margin
    if (
        min(page_width, page_height) <= 0
        or max(page_width, page_height) != CANONICAL_PAGE_LONG_EDGE
        or not 0.30 <= page_width / float(page_height) <= 3.30
    ):
        _raise("template_metadata_invalid")
    projected = cv2.perspectiveTransform(
        page_quad.astype(np.float32).reshape(-1, 1, 2),
        matrix.astype(np.float64),
    ).reshape(-1, 2)
    expected = np.asarray(
        [
            [margin, margin],
            [int(metadata["canonical_width"]) - margin - 1, margin],
            [
                int(metadata["canonical_width"]) - margin - 1,
                int(metadata["canonical_height"]) - margin - 1,
            ],
            [margin, int(metadata["canonical_height"]) - margin - 1],
        ],
        dtype=np.float32,
    )
    if (
        not np.isfinite(projected).all()
        or float(np.max(np.linalg.norm(projected - expected, axis=1))) > 2.0
    ):
        _raise("template_metadata_invalid")
    try:
        json.dumps(metadata, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError):
        _raise("template_metadata_invalid")


def _write_atomic(path: Path, payload: bytes, root: Path) -> None:
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        resolved_parent = path.parent.resolve(strict=True)
        if not resolved_parent.is_dir() or not _inside(root, resolved_parent):
            _raise("path_outside_workspace")
        path = resolved_parent / path.name
        temporary_path = resolved_parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        with temporary_path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except TemplateCanonicalizationError:
        raise
    except OSError:
        _raise("template_write_failed")
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def canonicalize_template(
    workspace_root: str | os.PathLike[str],
    source_path: str | os.PathLike[str],
    canonical_path: str | os.PathLike[str],
) -> CanonicalTemplateResult:
    """Create a deterministic canonical PNG inside a Rust-owned workspace."""

    root, source, output = _resolve_paths(
        workspace_root,
        source_path,
        canonical_path,
    )
    loaded, source_sha256 = _load_template_source(source)
    try:
        canonical_rgb, metadata = _canonical_page(loaded)
    except (RuntimeError, cv2.error):
        _raise("template_transform_invalid")
    except ValueError:
        _raise("template_page_rejected")
    try:
        canonical_png = _encode_png(canonical_rgb)
    except (OSError, ValueError):
        _raise("template_write_failed")

    metadata.update({
        "source_sha256": source_sha256,
        "canonical_sha256": hashlib.sha256(canonical_png).hexdigest(),
        "canonical_width": int(canonical_rgb.shape[1]),
        "canonical_height": int(canonical_rgb.shape[0]),
    })
    _validate_metadata(metadata, canonical_png)
    _write_atomic(output, canonical_png, root)
    try:
        if _sha256_file(output) != metadata["canonical_sha256"]:
            _raise("template_write_failed")
    except OSError:
        _raise("template_write_failed")
    return CanonicalTemplateResult(output, metadata)
