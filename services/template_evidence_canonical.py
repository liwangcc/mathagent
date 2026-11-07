"""A template page made canonical: loaded within bounds, its page found and rectified, or a client candidate accepted.

- Loading: ``_load_template_source`` and ``_load_rgb_image`` decode an image
  within ``MAX_IMAGE_EDGE`` / ``MAX_SOURCE_PIXELS`` (``_flatten_to_rgb``,
  ``_resize_long_edge``, ``_validated_source_dimensions``).
- ``canonicalize_template_image`` rectifies one new template before OCR or
  teacher region confirmation: it finds the page (``_detect_page_quad`` and its
  quad and confidence helpers) and warps it onto the canonical page
  (``_canonical_page``; ``CANONICAL_*`` constants), returning a
  ``CanonicalTemplate``.
- ``accept_canonical_template_candidate`` validates and keeps a client-produced
  page without decoding its pixels; ``parse_canonical_metadata`` reads the
  desktop manifest that comes with those uploads; ``_validate_canonicalization``
  checks recorded canonicalization metadata against its source image
  (``_safe_source_image_path``).
- ``_sha256_file``, ``_save_rgb_png_atomic`` and ``_copy_file_atomic`` hash and
  write the files canonical pages are stored as.

Moved unchanged from services/template_evidence.py, which still exposes these names.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from services.accelerator_artifacts import validate_uploaded_artifact_file
from services.template_canonicalization_contract import (
    validate_canonicalization_metadata,
)
from services.template_evidence_base import (
    Image,
    ImageOps,
    MAX_IMAGE_EDGE,
    MAX_SOURCE_PIXELS,
    _SHA256_RE,
    cv2,
    np,
)
from services.template_evidence_schema import _is_int


CANONICALIZATION_VERSION = "paper-canonical-v1"
CANONICAL_COORDINATE_SYSTEM = "canonical_template_v1"
CANONICAL_SOURCE_COORDINATE_SYSTEM = "exif_display_pixels_top_left"
CANONICAL_PAGE_LONG_EDGE = 2000
CANONICAL_PAGE_MARGIN = 32


_STORAGE_NAMESPACE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\Z")


_CANONICAL_CANDIDATE_KEYS = {
    "version",
    "algorithm",
    "source_sha256",
    "canonical_sha256",
    "source_format",
    "exif_orientation",
    "encoded_width",
    "encoded_height",
    "source_width",
    "source_height",
    "canonical_width",
    "canonical_height",
    "source_coordinate_space",
    "canonical_coordinate_space",
    "source_to_canonical",
    "detected_page_quad",
    "detection_method",
    "confidence",
    "page_margin_px",
}


@dataclass(frozen=True)
class CanonicalTemplate:
    """A server-owned canonical page and its source-bound geometry metadata."""

    image_path: str
    canonicalization: dict[str, Any]


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
        raise ValueError("image has invalid dimensions")
    longest = max(width, height)
    if longest <= max_edge:
        return image
    scale = max_edge / float(longest)
    target = (
        max(1, int(round(width * scale))),
        max(1, int(round(height * scale))),
    )
    return image.resize(target, Image.Resampling.LANCZOS)


def _validated_source_dimensions(image: Image.Image) -> tuple[int, int]:
    width, height = image.size
    if width <= 0 or height <= 0 or width * height > MAX_SOURCE_PIXELS:
        raise ValueError("image has invalid dimensions")
    return width, height


def _load_rgb_image(image_path: str | os.PathLike[str]) -> np.ndarray:
    path = Path(image_path)
    if not path.is_file():
        raise ValueError("image file does not exist")
    try:
        with Image.open(path) as source:
            _validated_source_dimensions(source)
            source.load()
            image = _resize_long_edge(_flatten_to_rgb(source))
            return np.ascontiguousarray(np.asarray(image, dtype=np.uint8))
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ValueError("image file is invalid") from exc


def _load_template_source(image_path: str | os.PathLike[str]) -> _LoadedTemplateSource:
    path = Path(image_path)
    if not path.is_file():
        raise ValueError("image file does not exist")
    try:
        with Image.open(path) as source:
            source_format = str(source.format or "").upper()
            if source_format == "MPO":
                # Some phone cameras store an ordinary primary JPEG plus a
                # secondary depth/stereo frame in a .jpg MPO container.  The
                # grading image is the first frame; keep the wire contract as
                # JPEG so desktop and server canonicalization agree.
                source.seek(0)
                source_format = "JPEG"
            if source_format not in {"JPEG", "PNG"}:
                raise ValueError("template source must be JPEG or PNG")
            encoded_width, encoded_height = _validated_source_dimensions(source)
            source.load()
            exif_orientation = int(source.getexif().get(274, 1) or 1)
            if exif_orientation < 1 or exif_orientation > 8:
                exif_orientation = 1
            display = _flatten_to_rgb(source)
            display_width, display_height = display.size
            work = _resize_long_edge(display)
            work_width, work_height = work.size
            return _LoadedTemplateSource(
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
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ValueError("image file is invalid") from exc


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
        raise ValueError("detected page boundary is ambiguous")
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
    """Accept content-bearing scanner/cropped templates without visible corners."""

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
        raise ValueError("template page is too small")

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
        score, quad, area_ratio = max(candidates, key=lambda item: item[0])
        edge_margin = max(2.0, min(gray.shape) * 0.012)
        touches = [
            point[0] <= edge_margin
            or point[1] <= edge_margin
            or point[0] >= gray.shape[1] - 1 - edge_margin
            or point[1] >= gray.shape[0] - 1 - edge_margin
            for point in quad
        ]
        if any(touches):
            # Missing visible paper corners is diagnostic only.  A tightly
            # cropped scanner image can still be the authoritative template
            # coordinate system; downstream source-mask gates protect the
            # actual answer regions on student papers.
            pass
        elif score >= 0.68:
            confidence = min(0.99, 0.68 + max(0.0, score - 0.68) * 1.4)
            return quad / float(detection_scale), "detected_quadrilateral", confidence

    fallback_confidence = max(full_frame_confidence, content_frame_confidence)
    if fallback_confidence > 0.0:
        return np.asarray(
            [[0.0, 0.0], [width - 1.0, 0.0], [width - 1.0, height - 1.0], [0.0, height - 1.0]],
            dtype=np.float32,
        ), "full_frame_scan", fallback_confidence
    raise ValueError("template page boundary confidence is too low")


def _canonical_page(source: _LoadedTemplateSource) -> tuple[np.ndarray, dict[str, Any]]:
    quad = _detect_page_quad(source.rgb)
    work_quad, detection_method, confidence = quad
    top_width = float(np.linalg.norm(work_quad[1] - work_quad[0]))
    bottom_width = float(np.linalg.norm(work_quad[2] - work_quad[3]))
    left_height = float(np.linalg.norm(work_quad[3] - work_quad[0]))
    right_height = float(np.linalg.norm(work_quad[2] - work_quad[1]))
    page_width = max(top_width, bottom_width)
    page_height = max(left_height, right_height)
    if min(page_width, page_height) < 120.0:
        raise ValueError("template page boundary is too small")
    aspect_ratio = page_width / page_height
    if not 0.30 <= aspect_ratio <= 3.30:
        raise ValueError("template page aspect ratio is invalid")

    output_scale = CANONICAL_PAGE_LONG_EDGE / max(page_width, page_height)
    canonical_page_width = max(1, int(round(page_width * output_scale)))
    canonical_page_height = max(1, int(round(page_height * output_scale)))
    output_width = canonical_page_width + 2 * CANONICAL_PAGE_MARGIN
    output_height = canonical_page_height + 2 * CANONICAL_PAGE_MARGIN
    destination = np.asarray(
        [
            [CANONICAL_PAGE_MARGIN, CANONICAL_PAGE_MARGIN],
            [CANONICAL_PAGE_MARGIN + canonical_page_width - 1, CANONICAL_PAGE_MARGIN],
            [CANONICAL_PAGE_MARGIN + canonical_page_width - 1, CANONICAL_PAGE_MARGIN + canonical_page_height - 1],
            [CANONICAL_PAGE_MARGIN, CANONICAL_PAGE_MARGIN + canonical_page_height - 1],
        ],
        dtype=np.float32,
    )
    work_to_canonical = cv2.getPerspectiveTransform(work_quad, destination)
    if not np.isfinite(work_to_canonical).all() or abs(float(np.linalg.det(work_to_canonical))) < 1e-9:
        raise ValueError("template canonical transform is invalid")
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
        "algorithm": "opencv_page_quad_v1",
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


def _sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _save_rgb_png_atomic(image: np.ndarray, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.parent / f".{output_path.name}.{uuid.uuid4().hex}.tmp"
    try:
        Image.fromarray(image, mode="RGB").save(temporary_path, format="PNG")
        os.replace(temporary_path, output_path)
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass


def _copy_file_atomic(source_path: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.parent / f".{output_path.name}.{uuid.uuid4().hex}.tmp"
    try:
        shutil.copyfile(source_path, temporary_path)
        os.replace(temporary_path, output_path)
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass


def parse_canonical_metadata(raw: str, expected_count: int) -> list[dict[str, Any]]:
    """Parse the path-free desktop manifest paired with canonical PNG uploads."""

    if not _is_int(expected_count) or expected_count <= 0:
        raise ValueError("canonical metadata expected_count is invalid")
    if not isinstance(raw, str) or not raw.strip() or len(raw.encode("utf-8")) > 1024 * 1024:
        raise ValueError("canonical_metadata must be a bounded JSON array")

    def reject_constant(value: str) -> None:
        raise ValueError(f"canonical_metadata contains invalid number {value}")

    try:
        parsed = json.loads(raw, parse_constant=reject_constant)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("canonical_metadata is malformed") from exc
    if not isinstance(parsed, list) or len(parsed) != expected_count:
        raise ValueError("canonical metadata count must match files")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(parsed):
        if not isinstance(item, dict) or set(item) != _CANONICAL_CANDIDATE_KEYS:
            raise ValueError(f"canonical metadata item {index} has invalid fields")
        result.append(dict(item))
    return result


def accept_canonical_template_candidate(
    source_image_path,
    canonical_image_path,
    raw_metadata: object,
    template_dir,
    page_index,
    *,
    storage_namespace: str = "canonical",
) -> CanonicalTemplate:
    """Validate and preserve a client-produced page without decoding its pixels."""

    if not _is_int(page_index) or page_index < 0:
        raise ValueError("page_index must be a non-negative integer")
    if not isinstance(storage_namespace, str) or not _STORAGE_NAMESPACE_RE.fullmatch(
        storage_namespace
    ):
        raise ValueError("storage_namespace is invalid")
    if not isinstance(raw_metadata, dict) or set(raw_metadata) != _CANONICAL_CANDIDATE_KEYS:
        raise ValueError("canonical candidate metadata has invalid fields")

    source_path = Path(source_image_path)
    candidate_path = Path(canonical_image_path)
    if not source_path.is_file():
        raise ValueError("canonical candidate source does not exist")
    source_sha256 = _sha256_file(source_path)
    canonical_manifest = validate_uploaded_artifact_file(
        candidate_path,
        "canonical_template",
    )
    contract_metadata = validate_canonicalization_metadata(
        raw_metadata,
        source_sha256=source_sha256,
        canonical_manifest=canonical_manifest,
    )
    root = Path(template_dir).resolve()
    canonical_dir = root / storage_namespace / f"page_{page_index:03d}"
    source_suffix = ".jpg" if contract_metadata["source_format"] == "jpeg" else ".png"
    preserved_source_path = canonical_dir / f"source{source_suffix}"
    preserved_canonical_path = canonical_dir / "template.png"
    try:
        _copy_file_atomic(source_path, preserved_source_path)
        _copy_file_atomic(candidate_path, preserved_canonical_path)
        metadata_with_server_path = dict(contract_metadata)
        metadata_with_server_path["source_image"] = preserved_source_path.relative_to(
            root
        ).as_posix()
        metadata = _validate_canonicalization(
            root,
            metadata_with_server_path,
            canonical_sha256=canonical_manifest["sha256"],
            canonical_width=canonical_manifest["width"],
            canonical_height=canonical_manifest["height"],
        )
    except Exception:
        shutil.rmtree(canonical_dir, ignore_errors=True)
        raise
    return CanonicalTemplate(
        image_path=str(preserved_canonical_path),
        canonicalization=metadata,
    )


def canonicalize_template_image(
    template_image_path,
    template_dir,
    page_index,
    *,
    storage_namespace: str = "canonical",
) -> CanonicalTemplate:
    """Rectify one new template before OCR or teacher region confirmation."""

    if not _is_int(page_index) or page_index < 0:
        raise ValueError("page_index must be a non-negative integer")
    if not isinstance(storage_namespace, str) or not _STORAGE_NAMESPACE_RE.fullmatch(
        storage_namespace
    ):
        raise ValueError("storage_namespace is invalid")
    source_path = Path(template_image_path)
    source = _load_template_source(source_path)
    canonical_rgb, metadata = _canonical_page(source)

    root = Path(template_dir).resolve()
    canonical_dir = root / storage_namespace / f"page_{page_index:03d}"
    source_suffix = ".jpg" if source.source_format == "JPEG" else ".png"
    preserved_source_path = canonical_dir / f"source{source_suffix}"
    canonical_path = canonical_dir / "template.png"
    _copy_file_atomic(source_path, preserved_source_path)
    _save_rgb_png_atomic(canonical_rgb, canonical_path)

    metadata.update({
        "source_image": preserved_source_path.relative_to(root).as_posix(),
        "source_sha256": _sha256_file(preserved_source_path),
        "canonical_sha256": _sha256_file(canonical_path),
        "canonical_width": int(canonical_rgb.shape[1]),
        "canonical_height": int(canonical_rgb.shape[0]),
    })
    return CanonicalTemplate(
        image_path=str(canonical_path),
        canonicalization=metadata,
    )


def _safe_source_image_path(template_dir, relative_path: object) -> tuple[str, str]:
    if not isinstance(relative_path, str) or not relative_path:
        raise ValueError("canonicalization source_image must be a relative path")
    if "\x00" in relative_path:
        raise ValueError("canonicalization source_image contains an invalid path")
    windows_path = PureWindowsPath(relative_path)
    posix_path = PurePosixPath(relative_path.replace("\\", "/"))
    if (
        windows_path.is_absolute()
        or bool(windows_path.drive)
        or posix_path.is_absolute()
        or any(part in {"", ".", ".."} for part in posix_path.parts)
        or posix_path.suffix.lower() not in {".jpg", ".jpeg", ".png"}
    ):
        raise ValueError("canonicalization source_image must stay inside template_dir")
    root = os.path.realpath(os.fspath(template_dir))
    resolved = os.path.realpath(os.path.join(root, *posix_path.parts))
    try:
        inside = os.path.commonpath((root, resolved)) == root
    except ValueError:
        inside = False
    if not inside or resolved == root:
        raise ValueError("canonicalization source_image must stay inside template_dir")
    return resolved, posix_path.as_posix()


def _validate_canonicalization(
    template_dir,
    raw: object,
    *,
    canonical_sha256: str,
    canonical_width: int,
    canonical_height: int,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("canonicalization must be an object")
    source_path, source_image = _safe_source_image_path(
        template_dir, raw.get("source_image")
    )
    if not os.path.isfile(source_path):
        raise ValueError("canonicalization source_image does not exist")
    source_sha256 = raw.get("source_sha256")
    if not isinstance(source_sha256, str) or not _SHA256_RE.fullmatch(source_sha256):
        raise ValueError("canonicalization source_sha256 is invalid")
    if _sha256_file(source_path) != source_sha256:
        raise ValueError("canonicalization source_sha256 mismatch")

    contract_metadata = dict(raw)
    contract_metadata.pop("source_image", None)
    validated = validate_canonicalization_metadata(
        contract_metadata,
        source_sha256=source_sha256,
        canonical_manifest={
            "sha256": canonical_sha256,
            "width": canonical_width,
            "height": canonical_height,
        },
    )
    validated["source_image"] = source_image
    return validated
