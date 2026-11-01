"""Versioned, replayable artifacts for independent accelerator lanes.

The worker is allowed to choose its own feature implementation, but the wire
artifact is deliberately narrow: one finite homography from raw decoded source
pixels to raw decoded template pixels, one rectified PNG, and one binary source
mask PNG. The server replays the submitted homography from immutable bytes and
never trusts worker-reported completeness.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib
import json
import math
import os
import re
import shutil
import struct
import uuid
import zlib
from pathlib import Path
from typing import Any

from services.template_canonicalization_contract import (
    CANONICAL_TEMPLATE_CONTRACT_VERSION,
)


class _LazyModule:
    """Load optional image runtimes only when pixel work actually starts."""

    def __init__(self, module_name: str):
        object.__setattr__(self, "_module_name", module_name)
        object.__setattr__(self, "_module", None)

    def _load(self):
        module = object.__getattribute__(self, "_module")
        if module is None:
            module = importlib.import_module(
                object.__getattribute__(self, "_module_name")
            )
            object.__setattr__(self, "_module", module)
        return module

    def __getattr__(self, name: str):
        return getattr(self._load(), name)

    def __setattr__(self, name: str, value) -> None:
        setattr(self._load(), name, value)


cv2 = _LazyModule("cv2")
np = _LazyModule("numpy")
Image = _LazyModule("PIL.Image")

ARTIFACT_CONTRACT_VERSION = "aligned-page-v1"
CLIENT_EVIDENCE_CONTRACT_VERSION = "client-evidence-v1"
CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION = "client-evidence-multi-v3"
# Current ordinary-paper contract.  It intentionally carries whole-height
# column images and question order only; per-question answer boxes are not a
# trusted or required artifact anymore.
CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION = "client-evidence-columns-v1"
# Additive artifact capability, not a sidecar version. Old column renderers do
# not understand frozen slot ownership even though their PNG contract matches.
CLIENT_EVIDENCE_SLOT_GEOMETRY_CONTRACT_VERSION = "client-evidence-slot-geometry-v1"


def layout_requires_slot_geometry(layout: dict[str, Any]) -> bool:
    """Presence is intentional: malformed/null slot metadata must not downgrade."""
    return any(
        isinstance(item, dict) and "slot_ids" in item
        for field in ("question_schema", "template_answer_regions")
        for item in (layout.get(field) or [])
    )


def validate_slot_geometry_receipt(
    result: dict[str, Any], *, stage: str,
    layout: dict[str, Any], layout_snapshot_sha256: str,
) -> None:
    required = stage == "student_multi_evidence" and layout_requires_slot_geometry(layout)
    if not required and "slot_geometry_receipt" not in result:
        return
    manifest = result.get("artifact_manifest")
    if not required or not isinstance(manifest, dict) or manifest.get(
        "contract_version"
    ) != CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION:
        raise ValueError("slot geometry requires full-column evidence")
    drawing = {item["question_number"] for item in layout.get("question_schema", [])
               if item.get("answer_kind") in {"drawing", "mixed"}}
    expected_labels = {}
    regions = layout.get("template_answer_regions")
    if not isinstance(regions, list):
        raise ValueError("slot geometry requires frozen answer regions")
    by_number = {item["question_number"]: item for item in regions}
    for question in layout.get("question_schema", []):
        if "slot_ids" in question and question["slot_ids"] != by_number.get(
            question["question_number"], {}
        ).get("slot_ids"):
            raise ValueError("slot geometry frozen schema and regions disagree")
    for item in regions:
        ids = item.get("slot_ids", [])
        boxes = item.get("regions", [])
        kinds = item.get("region_kinds", [])
        if not isinstance(ids, list) or (ids and len(ids) != len(boxes)):
            raise ValueError("slot geometry frozen mapping is invalid")
        for index, slot_id in enumerate(ids):
            if item["question_number"] in drawing and (
                index >= len(kinds) or kinds[index] != "answer"
            ):
                continue
            if slot_id in expected_labels:
                raise ValueError("slot geometry frozen mapping is duplicated")
            expected_labels[slot_id] = boxes[index]
    expected = {
        "contract_version": CLIENT_EVIDENCE_SLOT_GEOMETRY_CONTRACT_VERSION,
        "layout_snapshot_sha256": layout_snapshot_sha256,
        "labels": expected_labels,
    }
    # Compare the renderer's actual slot-to-source-box mapping, not just a
    # claimed capability. The layout hash also binds drawing geometry/policy.
    if result.get("slot_geometry_receipt") != expected:
        raise ValueError("slot geometry receipt does not match frozen layout")

# The wire contract remains client-evidence-columns-v1 for compatibility, but
# current acceptance is server-owned.  Keep the validator name centralized so
# validation and business consumption cannot drift.
SERVER_REPLAYED_FULL_COLUMN_VALIDATOR = "server_replayed_full_column_v1"
BOUND_CLIENT_FULL_COLUMN_VALIDATOR = "bound_client_full_column_v2"
LOCAL_FULL_COLUMN_CHECK_VERSION = "local-full-column-check-v2"
TEMPLATE_OCR_EVIDENCE_CONTRACT_VERSION = "template-ocr-evidence-v2"
TEMPLATE_OCR_EVIDENCE_KIND = "template_ocr_evidence"
COORDINATE_SPACE = "raw_source_pixel_to_template_pixel"
CLIENT_EVIDENCE_COORDINATE_SPACE = "answer_region_evidence_montage_v1"
CLIENT_EVIDENCE_MULTI_COORDINATE_SPACE = "answer_region_focus_sheets_v3"
TRANSFORM_GRAPH = "global_homography_only_v1"
RENDERER = "opencv_warp_v1"
CLIENT_EVIDENCE_RENDERER = "template_evidence_v1"
CLIENT_EVIDENCE_MULTI_RENDERER = "template_evidence_focus_sheets_v3"
CLIENT_EVIDENCE_FULL_COLUMN_COORDINATE_SPACE = "full_height_columns_v1"
CLIENT_EVIDENCE_FULL_COLUMN_RENDERER = "template_evidence_full_column_difference_v1"
MAX_MULTI_EVIDENCE_IMAGES = 12
MAX_MULTI_EVIDENCE_REGIONS = 200
MAX_UPLOAD_BYTES = 30 * 1024 * 1024
MAX_SOURCE_PIXELS = 16_000_000
MAX_TEMPLATE_PIXELS = 16_000_000
MAX_ALIGNED_MAE = 1.0
MAX_ALIGNED_P99 = 4.0
STRUCTURAL_MAX_EDGE = 1600
_ABS_DIFF_CHUNK_BYTES = 4 * 1024 * 1024
MIN_TEMPLATE_EDGE_PIXELS = 1000
MIN_TEMPLATE_EDGE_RECALL = 0.70
MIN_GRADIENT_COSINE = 0.46
MIN_AXIS_GRADIENT_COSINE = 0.38
_ALLOWED_KINDS = {
    "aligned_page",
    "source_mask",
    "evidence",
    "canonical_template",
    TEMPLATE_OCR_EVIDENCE_KIND,
}
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_JOB_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}\Z")
_MANIFEST_KEYS = {
    "contract_version",
    "coordinate_space",
    "transform_graph",
    "renderer",
    "source_width",
    "source_height",
    "template_width",
    "template_height",
    "aligned_page",
    "source_mask",
}
_CLIENT_EVIDENCE_MANIFEST_KEYS = {
    "contract_version",
    "coordinate_space",
    "transform_graph",
    "renderer",
    "source_width",
    "source_height",
    "template_width",
    "template_height",
    "evidence",
}
_CLIENT_EVIDENCE_MULTI_MANIFEST_KEYS = {
    "contract_version",
    "coordinate_space",
    "transform_graph",
    "renderer",
    "source_width",
    "source_height",
    "template_width",
    "template_height",
    "evidence_images",
}
_CLIENT_EVIDENCE_FULL_COLUMN_MANIFEST_KEYS = {
    "contract_version",
    "coordinate_space",
    "transform_graph",
    "renderer",
    "source_width",
    "source_height",
    "template_width",
    "template_height",
    "evidence_images",
}
_CANONICAL_TEMPLATE_MANIFEST_KEYS = {
    "contract_version",
    "canonical_template",
}
_TEMPLATE_OCR_EVIDENCE_MANIFEST_KEYS = {
    "contract_version",
    TEMPLATE_OCR_EVIDENCE_KIND,
}
_FILE_KEYS = {"sha256", "byte_size", "width", "height", "channels", "format"}
_MULTI_FILE_KEYS = _FILE_KEYS | {
    "kind",
    "sheet_id",
    "regions",
}
_FULL_COLUMN_FILE_KEYS = _FILE_KEYS | {
    "kind",
    "sheet_id",
    "question_numbers",
}
_MULTI_EVIDENCE_KIND_RE = re.compile(r"evidence_(?:0[1-9]|1[0-2])\Z")


@dataclass(frozen=True)
class MaterializedArtifacts:
    aligned_page_path: Path
    source_mask_path: Path
    manifest: dict[str, Any]
    evidence_path: Path | None = None
    evidence_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ReplayedArtifacts:
    aligned_rgb: np.ndarray
    source_mask: np.ndarray
    template_rgb: np.ndarray
    coverage: float
    aligned_mae: float
    aligned_p99: float
    projected_quad: list[list[float]]
    template_edge_recall: float
    gradient_cosine: float
    template_edge_pixels: int
    aligned_edge_pixels: int
    gradient_x_cosine: float
    gradient_y_cosine: float


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_raw_dimensions(
    path: str | os.PathLike[str],
    *,
    limit: int,
) -> tuple[int, int]:
    try:
        with Image.open(path) as encoded:
            width, height = encoded.size
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ValueError("artifact source image is invalid") from exc
    if height < 32 or width < 32 or width * height > limit:
        raise ValueError("artifact source image dimensions are invalid")
    return height, width


def _raw_decode(path: str | os.PathLike[str], *, source: bool) -> np.ndarray:
    limit = MAX_SOURCE_PIXELS if source else MAX_TEMPLATE_PIXELS
    expected_shape = _validated_raw_dimensions(path, limit=limit)
    flags = cv2.IMREAD_COLOR | getattr(cv2, "IMREAD_IGNORE_ORIENTATION", 0)
    image = cv2.imread(os.fspath(path), flags)
    if image is None or image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("artifact source image is invalid")
    height, width = image.shape[:2]
    pixels = int(height) * int(width)
    if (
        height < 32
        or width < 32
        or pixels > limit
        or (height, width) != expected_shape
    ):
        raise ValueError("artifact source image dimensions are invalid")
    return np.ascontiguousarray(image)


def decode_raw_source(path: str | os.PathLike[str]) -> np.ndarray:
    return _raw_decode(path, source=True)


def decode_raw_template(path: str | os.PathLike[str]) -> np.ndarray:
    return _raw_decode(path, source=False)


def normalize_homography(value: Any) -> np.ndarray:
    try:
        matrix = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ValueError("homography is invalid") from exc
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all():
        raise ValueError("homography must be a finite 3x3 matrix")
    denominator = float(matrix[2, 2])
    if abs(denominator) < 1e-12:
        raise ValueError("homography is singular")
    matrix = matrix / denominator
    determinant = float(np.linalg.det(matrix))
    if not math.isfinite(determinant) or abs(determinant) < 1e-12:
        raise ValueError("homography is singular")
    try:
        inverse = np.linalg.inv(matrix)
    except np.linalg.LinAlgError as exc:
        raise ValueError("homography is singular") from exc
    if not np.isfinite(inverse).all():
        raise ValueError("homography inverse is invalid")
    return np.ascontiguousarray(matrix)


def _encode_png(path: Path, image: np.ndarray) -> tuple[str, int]:
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise ValueError("artifact PNG encoding failed")
    raw = encoded.tobytes()
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("artifact PNG is outside size bounds")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(raw)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(raw).hexdigest(), len(raw)


def _file_manifest(
    *,
    sha256: str,
    byte_size: int,
    width: int,
    height: int,
    channels: int,
) -> dict[str, Any]:
    return {
        "sha256": sha256,
        "byte_size": int(byte_size),
        "width": int(width),
        "height": int(height),
        "channels": int(channels),
        "format": "png",
    }


def materialize_global_artifacts(
    source_path: str | os.PathLike[str],
    template_path: str | os.PathLike[str],
    homography: Any,
    output_dir: str | os.PathLike[str],
) -> MaterializedArtifacts:
    source = decode_raw_source(source_path)
    template = decode_raw_template(template_path)
    matrix = normalize_homography(homography)
    source_height, source_width = source.shape[:2]
    template_height, template_width = template.shape[:2]
    aligned = cv2.warpPerspective(
        source,
        matrix,
        (template_width, template_height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255),
    )
    source_mask = cv2.warpPerspective(
        np.full((source_height, source_width), 255, dtype=np.uint8),
        matrix,
        (template_width, template_height),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    root = Path(output_dir)
    aligned_path = root / "aligned_page.png"
    mask_path = root / "source_mask.png"
    aligned_sha, aligned_size = _encode_png(aligned_path, aligned)
    mask_sha, mask_size = _encode_png(mask_path, source_mask)
    manifest = {
        "contract_version": ARTIFACT_CONTRACT_VERSION,
        "coordinate_space": COORDINATE_SPACE,
        "transform_graph": TRANSFORM_GRAPH,
        "renderer": RENDERER,
        "source_width": source_width,
        "source_height": source_height,
        "template_width": template_width,
        "template_height": template_height,
        "aligned_page": _file_manifest(
            sha256=aligned_sha,
            byte_size=aligned_size,
            width=template_width,
            height=template_height,
            channels=3,
        ),
        "source_mask": _file_manifest(
            sha256=mask_sha,
            byte_size=mask_size,
            width=template_width,
            height=template_height,
            channels=1,
        ),
    }
    return MaterializedArtifacts(aligned_path, mask_path, manifest)


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"artifact manifest {field} is invalid")
    return value


def validate_artifact_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("artifact_manifest has invalid fields")
    if value.get("contract_version") == CLIENT_EVIDENCE_CONTRACT_VERSION:
        return _validate_client_evidence_manifest(value)
    if value.get("contract_version") == CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION:
        return _validate_client_evidence_multi_manifest(value)
    if value.get("contract_version") == CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION:
        return _validate_client_evidence_full_column_manifest(value)
    if value.get("contract_version") == CANONICAL_TEMPLATE_CONTRACT_VERSION:
        return _validate_canonical_template_manifest(value)
    if value.get("contract_version") == TEMPLATE_OCR_EVIDENCE_CONTRACT_VERSION:
        return _validate_template_ocr_evidence_manifest(value)
    if set(value) != _MANIFEST_KEYS:
        raise ValueError("artifact_manifest has invalid fields")
    if value.get("contract_version") != ARTIFACT_CONTRACT_VERSION:
        raise ValueError("artifact contract version is unsupported")
    if value.get("coordinate_space") != COORDINATE_SPACE:
        raise ValueError("artifact coordinate space is unsupported")
    if value.get("transform_graph") != TRANSFORM_GRAPH:
        raise ValueError("artifact transform graph is unsupported")
    if value.get("renderer") != RENDERER:
        raise ValueError("artifact renderer is unsupported")
    normalized = {
        "contract_version": ARTIFACT_CONTRACT_VERSION,
        "coordinate_space": COORDINATE_SPACE,
        "transform_graph": TRANSFORM_GRAPH,
        "renderer": RENDERER,
        "source_width": _positive_int(value.get("source_width"), "source_width"),
        "source_height": _positive_int(value.get("source_height"), "source_height"),
        "template_width": _positive_int(value.get("template_width"), "template_width"),
        "template_height": _positive_int(value.get("template_height"), "template_height"),
    }
    if normalized["source_width"] * normalized["source_height"] > MAX_SOURCE_PIXELS:
        raise ValueError("artifact manifest source dimensions are too large")
    if normalized["template_width"] * normalized["template_height"] > MAX_TEMPLATE_PIXELS:
        raise ValueError("artifact manifest template dimensions are too large")
    for kind, channels in (("aligned_page", 3), ("source_mask", 1)):
        raw = value.get(kind)
        if not isinstance(raw, dict) or set(raw) != _FILE_KEYS:
            raise ValueError(f"artifact manifest {kind} has invalid fields")
        sha = raw.get("sha256")
        if not isinstance(sha, str) or not _HASH_RE.fullmatch(sha):
            raise ValueError(f"artifact manifest {kind} hash is invalid")
        byte_size = _positive_int(raw.get("byte_size"), f"{kind}.byte_size")
        if byte_size > MAX_UPLOAD_BYTES:
            raise ValueError(f"artifact manifest {kind} is too large")
        width = _positive_int(raw.get("width"), f"{kind}.width")
        height = _positive_int(raw.get("height"), f"{kind}.height")
        if (
            width != normalized["template_width"]
            or height != normalized["template_height"]
            or raw.get("channels") != channels
            or raw.get("format") != "png"
        ):
            raise ValueError(f"artifact manifest {kind} dimensions or format are invalid")
        normalized[kind] = _file_manifest(
            sha256=sha,
            byte_size=byte_size,
            width=width,
            height=height,
            channels=channels,
        )
    return normalized


def _validate_canonical_template_manifest(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != _CANONICAL_TEMPLATE_MANIFEST_KEYS:
        raise ValueError("artifact_manifest has invalid fields")
    raw = value.get("canonical_template")
    if not isinstance(raw, dict) or set(raw) != _FILE_KEYS:
        raise ValueError("artifact manifest canonical_template has invalid fields")
    sha = raw.get("sha256")
    if not isinstance(sha, str) or not _HASH_RE.fullmatch(sha):
        raise ValueError("artifact manifest canonical_template hash is invalid")
    byte_size = _positive_int(
        raw.get("byte_size"),
        "canonical_template.byte_size",
    )
    if byte_size > MAX_UPLOAD_BYTES:
        raise ValueError("artifact manifest canonical_template is too large")
    width = _positive_int(raw.get("width"), "canonical_template.width")
    height = _positive_int(raw.get("height"), "canonical_template.height")
    if (
        width * height > MAX_TEMPLATE_PIXELS
        or raw.get("channels") != 3
        or raw.get("format") != "png"
    ):
        raise ValueError(
            "artifact manifest canonical_template dimensions or format are invalid"
        )
    return {
        "contract_version": CANONICAL_TEMPLATE_CONTRACT_VERSION,
        "canonical_template": _file_manifest(
            sha256=sha,
            byte_size=byte_size,
            width=width,
            height=height,
            channels=3,
        ),
    }


def _validate_template_ocr_evidence_manifest(
    value: dict[str, Any],
) -> dict[str, Any]:
    if set(value) != _TEMPLATE_OCR_EVIDENCE_MANIFEST_KEYS:
        raise ValueError("artifact_manifest has invalid fields")
    raw = value.get(TEMPLATE_OCR_EVIDENCE_KIND)
    if not isinstance(raw, dict) or set(raw) != _FILE_KEYS:
        raise ValueError("artifact manifest template_ocr_evidence has invalid fields")
    sha = raw.get("sha256")
    if not isinstance(sha, str) or not _HASH_RE.fullmatch(sha):
        raise ValueError("artifact manifest template_ocr_evidence hash is invalid")
    byte_size = _positive_int(
        raw.get("byte_size"),
        "template_ocr_evidence.byte_size",
    )
    if byte_size > MAX_UPLOAD_BYTES:
        raise ValueError("artifact manifest template_ocr_evidence is too large")
    width = _positive_int(raw.get("width"), "template_ocr_evidence.width")
    height = _positive_int(raw.get("height"), "template_ocr_evidence.height")
    if (
        width * height > MAX_TEMPLATE_PIXELS
        or raw.get("channels") != 3
        or raw.get("format") != "png"
    ):
        raise ValueError(
            "artifact manifest template_ocr_evidence dimensions or format are invalid"
        )
    return {
        "contract_version": TEMPLATE_OCR_EVIDENCE_CONTRACT_VERSION,
        TEMPLATE_OCR_EVIDENCE_KIND: _file_manifest(
            sha256=sha,
            byte_size=byte_size,
            width=width,
            height=height,
            channels=3,
        ),
    }


def _validate_client_evidence_manifest(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != _CLIENT_EVIDENCE_MANIFEST_KEYS:
        raise ValueError("artifact_manifest has invalid fields")
    if value.get("coordinate_space") != CLIENT_EVIDENCE_COORDINATE_SPACE:
        raise ValueError("artifact coordinate space is unsupported")
    if value.get("transform_graph") != TRANSFORM_GRAPH:
        raise ValueError("artifact transform graph is unsupported")
    if value.get("renderer") != CLIENT_EVIDENCE_RENDERER:
        raise ValueError("artifact renderer is unsupported")
    normalized = {
        "contract_version": CLIENT_EVIDENCE_CONTRACT_VERSION,
        "coordinate_space": CLIENT_EVIDENCE_COORDINATE_SPACE,
        "transform_graph": TRANSFORM_GRAPH,
        "renderer": CLIENT_EVIDENCE_RENDERER,
        "source_width": _positive_int(value.get("source_width"), "source_width"),
        "source_height": _positive_int(value.get("source_height"), "source_height"),
        "template_width": _positive_int(value.get("template_width"), "template_width"),
        "template_height": _positive_int(value.get("template_height"), "template_height"),
    }
    if normalized["source_width"] * normalized["source_height"] > MAX_SOURCE_PIXELS:
        raise ValueError("artifact manifest source dimensions are too large")
    if normalized["template_width"] * normalized["template_height"] > MAX_TEMPLATE_PIXELS:
        raise ValueError("artifact manifest template dimensions are too large")
    raw = value.get("evidence")
    if not isinstance(raw, dict) or set(raw) != _FILE_KEYS:
        raise ValueError("artifact manifest evidence has invalid fields")
    sha = raw.get("sha256")
    if not isinstance(sha, str) or not _HASH_RE.fullmatch(sha):
        raise ValueError("artifact manifest evidence hash is invalid")
    byte_size = _positive_int(raw.get("byte_size"), "evidence.byte_size")
    if byte_size > MAX_UPLOAD_BYTES:
        raise ValueError("artifact manifest evidence is too large")
    width = _positive_int(raw.get("width"), "evidence.width")
    height = _positive_int(raw.get("height"), "evidence.height")
    if (
        width * height > MAX_TEMPLATE_PIXELS
        or raw.get("channels") != 3
        or raw.get("format") != "png"
    ):
        raise ValueError("artifact manifest evidence dimensions or format are invalid")
    normalized["evidence"] = _file_manifest(
        sha256=sha,
        byte_size=byte_size,
        width=width,
        height=height,
        channels=3,
    )
    return normalized


def _validate_client_evidence_multi_manifest(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != _CLIENT_EVIDENCE_MULTI_MANIFEST_KEYS:
        raise ValueError("artifact_manifest has invalid fields")
    if value.get("coordinate_space") != CLIENT_EVIDENCE_MULTI_COORDINATE_SPACE:
        raise ValueError("artifact coordinate space is unsupported")
    if value.get("transform_graph") != TRANSFORM_GRAPH:
        raise ValueError("artifact transform graph is unsupported")
    if value.get("renderer") != CLIENT_EVIDENCE_MULTI_RENDERER:
        raise ValueError("artifact renderer is unsupported")
    normalized = {
        "contract_version": CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION,
        "coordinate_space": CLIENT_EVIDENCE_MULTI_COORDINATE_SPACE,
        "transform_graph": TRANSFORM_GRAPH,
        "renderer": CLIENT_EVIDENCE_MULTI_RENDERER,
        "source_width": _positive_int(value.get("source_width"), "source_width"),
        "source_height": _positive_int(value.get("source_height"), "source_height"),
        "template_width": _positive_int(value.get("template_width"), "template_width"),
        "template_height": _positive_int(value.get("template_height"), "template_height"),
    }
    if normalized["source_width"] * normalized["source_height"] > MAX_SOURCE_PIXELS:
        raise ValueError("artifact manifest source dimensions are too large")
    if normalized["template_width"] * normalized["template_height"] > MAX_TEMPLATE_PIXELS:
        raise ValueError("artifact manifest template dimensions are too large")
    raw_images = value.get("evidence_images")
    if not isinstance(raw_images, list) or not 1 <= len(raw_images) <= MAX_MULTI_EVIDENCE_IMAGES:
        raise ValueError("artifact manifest evidence_images count is invalid")
    images = []
    total_bytes = 0
    total_pixels = 0
    total_regions = 0
    seen_questions: set[str] = set()
    for position, raw in enumerate(raw_images, 1):
        if not isinstance(raw, dict) or set(raw) != _MULTI_FILE_KEYS:
            raise ValueError("artifact manifest evidence image has invalid fields")
        expected_kind = f"evidence_{position:02d}"
        if raw.get("kind") != expected_kind:
            raise ValueError("artifact manifest evidence image order is invalid")
        sheet_id = str(raw.get("sheet_id") or "").strip()
        raw_regions = raw.get("regions")
        if sheet_id != f"sheet_{position:02d}" or not isinstance(raw_regions, list):
            raise ValueError("artifact manifest evidence image metadata is invalid")
        if not raw_regions or total_regions + len(raw_regions) > MAX_MULTI_EVIDENCE_REGIONS:
            raise ValueError("artifact manifest evidence regions count is invalid")
        regions = []
        for raw_region in raw_regions:
            if not isinstance(raw_region, dict) or set(raw_region) != {
                "question_number",
                "bbox_1000",
            }:
                raise ValueError("artifact manifest evidence region is invalid")
            question_number = str(raw_region.get("question_number") or "").strip()
            bbox = raw_region.get("bbox_1000")
            if (
                not question_number
                or len(question_number) > 64
                or any(ord(char) < 32 for char in question_number)
                or question_number in seen_questions
                or not isinstance(bbox, list)
                or len(bbox) != 4
                or any(
                    isinstance(value, bool) or not isinstance(value, int)
                    for value in bbox
                )
                or not (0 <= bbox[0] < bbox[2] <= 1000)
                or not (0 <= bbox[1] < bbox[3] <= 1000)
            ):
                raise ValueError("artifact manifest evidence region is invalid")
            seen_questions.add(question_number)
            regions.append({
                "question_number": question_number,
                "bbox_1000": list(bbox),
            })
        total_regions += len(regions)
        sha = raw.get("sha256")
        if not isinstance(sha, str) or not _HASH_RE.fullmatch(sha):
            raise ValueError("artifact manifest evidence image hash is invalid")
        byte_size = _positive_int(raw.get("byte_size"), f"{expected_kind}.byte_size")
        width = _positive_int(raw.get("width"), f"{expected_kind}.width")
        height = _positive_int(raw.get("height"), f"{expected_kind}.height")
        total_bytes += byte_size
        total_pixels += width * height
        if (
            total_bytes > MAX_UPLOAD_BYTES
            or total_pixels > MAX_TEMPLATE_PIXELS * 2
            or width * height > MAX_TEMPLATE_PIXELS
            or raw.get("channels") != 3
            or raw.get("format") != "png"
        ):
            raise ValueError("artifact manifest evidence images exceed limits")
        images.append({
            "kind": expected_kind,
            "sheet_id": sheet_id,
            "regions": regions,
            **_file_manifest(
                sha256=sha,
                byte_size=byte_size,
                width=width,
                height=height,
                channels=3,
            ),
        })
    normalized["evidence_images"] = images
    return normalized


def _validate_client_evidence_full_column_manifest(
    value: dict[str, Any],
) -> dict[str, Any]:
    """Validate the ordinary-paper whole-column evidence contract.

    A column owns a contiguous vertical slice of the page.  The manifest
    records only the ordered question numbers visible in that slice; it never
    transports a model-predicted student-answer rectangle.
    """

    if set(value) != _CLIENT_EVIDENCE_FULL_COLUMN_MANIFEST_KEYS:
        raise ValueError("artifact_manifest has invalid fields")
    if value.get("coordinate_space") != CLIENT_EVIDENCE_FULL_COLUMN_COORDINATE_SPACE:
        raise ValueError("artifact coordinate space is unsupported")
    if value.get("transform_graph") != TRANSFORM_GRAPH:
        raise ValueError("artifact transform graph is unsupported")
    if value.get("renderer") != CLIENT_EVIDENCE_FULL_COLUMN_RENDERER:
        raise ValueError("artifact renderer is unsupported")

    normalized = {
        "contract_version": CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION,
        "coordinate_space": CLIENT_EVIDENCE_FULL_COLUMN_COORDINATE_SPACE,
        "transform_graph": TRANSFORM_GRAPH,
        "renderer": CLIENT_EVIDENCE_FULL_COLUMN_RENDERER,
        "source_width": _positive_int(value.get("source_width"), "source_width"),
        "source_height": _positive_int(value.get("source_height"), "source_height"),
        "template_width": _positive_int(value.get("template_width"), "template_width"),
        "template_height": _positive_int(value.get("template_height"), "template_height"),
    }
    if normalized["source_width"] * normalized["source_height"] > MAX_SOURCE_PIXELS:
        raise ValueError("artifact manifest source dimensions are too large")
    if normalized["template_width"] * normalized["template_height"] > MAX_TEMPLATE_PIXELS:
        raise ValueError("artifact manifest template dimensions are too large")

    raw_images = value.get("evidence_images")
    if not isinstance(raw_images, list) or not 1 <= len(raw_images) <= 3:
        raise ValueError("artifact manifest evidence_images count is invalid")
    images: list[dict[str, Any]] = []
    seen_questions: set[str] = set()
    total_bytes = 0
    total_pixels = 0
    for position, raw in enumerate(raw_images, 1):
        if not isinstance(raw, dict) or set(raw) != _FULL_COLUMN_FILE_KEYS:
            raise ValueError("artifact manifest evidence image has invalid fields")
        expected_kind = f"evidence_{position:02d}"
        expected_sheet = f"column_{position:02d}"
        if raw.get("kind") != expected_kind or raw.get("sheet_id") != expected_sheet:
            raise ValueError("artifact manifest evidence column order is invalid")
        raw_questions = raw.get("question_numbers")
        if not isinstance(raw_questions, list) or not raw_questions:
            raise ValueError("artifact manifest evidence column question order is invalid")
        question_numbers: list[str] = []
        for raw_number in raw_questions:
            question_number = str(raw_number or "").strip()
            if (
                not question_number
                or len(question_number) > 64
                or any(ord(char) < 32 for char in question_number)
                or question_number in seen_questions
            ):
                raise ValueError("artifact manifest evidence column question order is invalid")
            seen_questions.add(question_number)
            question_numbers.append(question_number)
        sha = raw.get("sha256")
        if not isinstance(sha, str) or not _HASH_RE.fullmatch(sha):
            raise ValueError("artifact manifest evidence image hash is invalid")
        byte_size = _positive_int(raw.get("byte_size"), f"{expected_kind}.byte_size")
        width = _positive_int(raw.get("width"), f"{expected_kind}.width")
        height = _positive_int(raw.get("height"), f"{expected_kind}.height")
        total_bytes += byte_size
        total_pixels += width * height
        if (
            total_bytes > MAX_UPLOAD_BYTES
            or total_pixels > MAX_TEMPLATE_PIXELS * 2
            or width * height > MAX_TEMPLATE_PIXELS
            or raw.get("channels") != 3
            or raw.get("format") != "png"
        ):
            raise ValueError("artifact manifest evidence columns exceed limits")
        images.append({
            "kind": expected_kind,
            "sheet_id": expected_sheet,
            "question_numbers": question_numbers,
            **_file_manifest(
                sha256=sha,
                byte_size=byte_size,
                width=width,
                height=height,
                channels=3,
            ),
        })
    normalized["evidence_images"] = images
    return normalized


def client_evidence_manifest(
    aligned_manifest: Any,
    evidence_path: str | os.PathLike[str],
) -> dict[str, Any]:
    """Bind a client-rendered evidence montage to the immutable job geometry."""

    aligned = validate_artifact_manifest(aligned_manifest)
    if aligned.get("contract_version") != ARTIFACT_CONTRACT_VERSION:
        raise ValueError("aligned artifact manifest is required")
    evidence = validate_uploaded_artifact_file(evidence_path, "evidence")
    return validate_artifact_manifest({
        "contract_version": CLIENT_EVIDENCE_CONTRACT_VERSION,
        "coordinate_space": CLIENT_EVIDENCE_COORDINATE_SPACE,
        "transform_graph": TRANSFORM_GRAPH,
        "renderer": CLIENT_EVIDENCE_RENDERER,
        "source_width": aligned["source_width"],
        "source_height": aligned["source_height"],
        "template_width": aligned["template_width"],
        "template_height": aligned["template_height"],
        "evidence": evidence,
    })


def client_multi_evidence_manifest(
    aligned_manifest: Any,
    evidence_paths: list[str | os.PathLike[str]],
    sheets: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bind ordered answer-focus sheets to one immutable job geometry."""

    aligned = validate_artifact_manifest(aligned_manifest)
    if aligned.get("contract_version") != ARTIFACT_CONTRACT_VERSION:
        raise ValueError("aligned artifact manifest is required")
    if len(evidence_paths) != len(sheets):
        raise ValueError("evidence image paths and sheets do not match")
    images = []
    for position, (path, sheet) in enumerate(zip(evidence_paths, sheets), 1):
        kind = f"evidence_{position:02d}"
        file_manifest = validate_uploaded_artifact_file(path, kind)
        images.append({
            "kind": kind,
            "sheet_id": str(sheet.get("sheet_id") or ""),
            "regions": list(sheet.get("regions") or []),
            **file_manifest,
        })
    return validate_artifact_manifest({
        "contract_version": CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION,
        "coordinate_space": CLIENT_EVIDENCE_MULTI_COORDINATE_SPACE,
        "transform_graph": TRANSFORM_GRAPH,
        "renderer": CLIENT_EVIDENCE_MULTI_RENDERER,
        "source_width": aligned["source_width"],
        "source_height": aligned["source_height"],
        "template_width": aligned["template_width"],
        "template_height": aligned["template_height"],
        "evidence_images": images,
    })


def client_full_column_evidence_manifest(
    aligned_manifest: Any,
    evidence_paths: list[str | os.PathLike[str]],
    columns: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bind ordered full-height column evidence to one aligned page."""

    aligned = validate_artifact_manifest(aligned_manifest)
    if aligned.get("contract_version") != ARTIFACT_CONTRACT_VERSION:
        raise ValueError("aligned artifact manifest is required")
    if len(evidence_paths) != len(columns):
        raise ValueError("evidence image paths and columns do not match")
    images = []
    for position, (path, column) in enumerate(zip(evidence_paths, columns), 1):
        if not isinstance(column, dict):
            raise ValueError("evidence column metadata is invalid")
        kind = f"evidence_{position:02d}"
        expected_sheet = f"column_{position:02d}"
        question_numbers = column.get("question_numbers")
        if column.get("sheet_id") not in {None, expected_sheet}:
            raise ValueError("evidence column order is invalid")
        if not isinstance(question_numbers, list) or not question_numbers:
            raise ValueError("evidence column question order is invalid")
        file_manifest = validate_uploaded_artifact_file(path, kind)
        images.append({
            "kind": kind,
            "sheet_id": expected_sheet,
            "question_numbers": [str(value).strip() for value in question_numbers],
            **file_manifest,
        })
    return validate_artifact_manifest({
        "contract_version": CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION,
        "coordinate_space": CLIENT_EVIDENCE_FULL_COLUMN_COORDINATE_SPACE,
        "transform_graph": TRANSFORM_GRAPH,
        "renderer": CLIENT_EVIDENCE_FULL_COLUMN_RENDERER,
        "source_width": aligned["source_width"],
        "source_height": aligned["source_height"],
        "template_width": aligned["template_width"],
        "template_height": aligned["template_height"],
        "evidence_images": images,
    })


def candidate_artifact_sha256(
    *,
    page_type_id: str,
    source_sha256: str,
    template_sha256: str,
    layout_snapshot_sha256: str,
    homography: Any,
    artifact_manifest: Any,
) -> str:
    for value in (source_sha256, template_sha256, layout_snapshot_sha256):
        if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
            raise ValueError("candidate identity hash is invalid")
    page_type_id = str(page_type_id or "").strip()
    if not page_type_id or len(page_type_id) > 64:
        raise ValueError("candidate page_type_id is invalid")
    matrix = normalize_homography(homography)
    manifest = validate_artifact_manifest(artifact_manifest)
    payload = {
        "artifact_contract_version": ARTIFACT_CONTRACT_VERSION,
        "page_type_id": page_type_id,
        "source_sha256": source_sha256,
        "template_sha256": template_sha256,
        "layout_snapshot_sha256": layout_snapshot_sha256,
        "homography": [[float(item) for item in row] for row in matrix.tolist()],
        "artifact_manifest": manifest,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def candidate_artifact_root(source_path: str | os.PathLike[str], job_id: str) -> Path:
    if not isinstance(job_id, str) or not _JOB_ID_RE.fullmatch(job_id):
        raise ValueError("accelerator job id is invalid")
    source = Path(source_path).resolve()
    root = (source.parent / ".accelerator-candidates" / job_id).resolve()
    try:
        root.relative_to(source.parent)
    except ValueError as exc:
        raise ValueError("candidate artifact path escaped the source directory") from exc
    return root


def candidate_artifact_path(
    source_path: str | os.PathLike[str],
    job_id: str,
    kind: str,
) -> Path:
    if kind not in _ALLOWED_KINDS and not _MULTI_EVIDENCE_KIND_RE.fullmatch(kind):
        raise ValueError("candidate artifact kind is invalid")
    return candidate_artifact_root(source_path, job_id) / f"{kind}.png"


def cleanup_candidate_artifacts(source_path: str | os.PathLike[str], job_id: str) -> None:
    root = candidate_artifact_root(source_path, job_id)
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    parent = root.parent
    try:
        parent.rmdir()
    except OSError:
        pass


def _validated_png_dimensions(raw: bytes, *, mask: bool) -> tuple[int, int]:
    """Reject oversized or incompatible PNGs before allocating decoded pixels."""

    signature = b"\x89PNG\r\n\x1a\n"
    if (
        len(raw) < 33
        or raw[:8] != signature
        or raw[8:12] != b"\x00\x00\x00\r"
        or raw[12:16] != b"IHDR"
    ):
        raise ValueError("candidate artifact is not a valid PNG image")
    width, height = struct.unpack(">II", raw[16:24])
    bit_depth = raw[24]
    color_type = raw[25]
    compression, filtering, interlace = raw[26:29]
    expected_color_type = 0 if mask else 2
    if (
        width <= 0
        or height <= 0
        or width * height > MAX_TEMPLATE_PIXELS
        or bit_depth != 8
        or color_type != expected_color_type
        or compression != 0
        or filtering != 0
        or interlace != 0
    ):
        kind = "source mask" if mask else "aligned page"
        raise ValueError(f"candidate {kind} has invalid dimensions or PNG format")
    return width, height


def _decode_uploaded_png_bytes(raw: bytes, *, mask: bool) -> np.ndarray:
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("candidate artifact file is missing or too large")
    expected_width, expected_height = _validated_png_dimensions(raw, mask=mask)
    flags = cv2.IMREAD_UNCHANGED
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), flags)
    if image is None or image.dtype != np.uint8:
        raise ValueError("candidate artifact is not a valid PNG image")
    if image.shape[:2] != (expected_height, expected_width):
        raise ValueError("candidate artifact decoded dimensions do not match PNG header")
    if mask:
        if image.ndim != 2 or image.shape[0] * image.shape[1] > MAX_TEMPLATE_PIXELS:
            raise ValueError("candidate source mask has invalid dimensions")
        values = np.unique(image)
        if not set(int(item) for item in values.tolist()).issubset({0, 255}):
            raise ValueError("candidate source mask must be binary")
    elif image.ndim != 3 or image.shape[2] != 3 or image.shape[0] * image.shape[1] > MAX_TEMPLATE_PIXELS:
        raise ValueError("candidate aligned page has invalid dimensions")
    return np.ascontiguousarray(image)


def _decode_uploaded_png(path: Path, *, mask: bool) -> np.ndarray:
    if not path.is_file():
        raise ValueError("candidate artifact file is missing or too large")
    return _decode_uploaded_png_bytes(path.read_bytes(), mask=mask)


def decode_uploaded_aligned_page(path: str | os.PathLike[str]) -> np.ndarray:
    """Decode one bounded RGB worker artifact without replaying its transform."""

    image = _decode_uploaded_png(Path(path), mask=False)
    return image[:, :, ::-1]


def decode_uploaded_source_mask(path: str | os.PathLike[str]) -> np.ndarray:
    """Decode one bounded binary worker source mask."""

    return _decode_uploaded_png(Path(path), mask=True)


def _validate_streaming_rgb_png(
    path: Path,
    *,
    artifact_label: str = "canonical template",
) -> dict[str, Any]:
    """Validate PNG chunks and CRCs without allocating a decoded pixel buffer."""

    size = path.stat().st_size
    if size <= 0 or size > MAX_UPLOAD_BYTES:
        raise ValueError("candidate artifact file is missing or too large")
    digest = hashlib.sha256()
    width = height = 0
    saw_ihdr = False
    saw_idat = False
    saw_iend = False
    try:
        with path.open("rb") as stream:
            signature = stream.read(8)
            digest.update(signature)
            if signature != b"\x89PNG\r\n\x1a\n":
                raise ValueError("candidate artifact is not a valid PNG image")
            while not saw_iend:
                header = stream.read(8)
                if len(header) != 8:
                    raise ValueError("candidate artifact is not a complete PNG image")
                digest.update(header)
                chunk_length, chunk_type = struct.unpack(">I4s", header)
                if chunk_length > MAX_UPLOAD_BYTES or not all(
                    (65 <= value <= 90) or (97 <= value <= 122)
                    for value in chunk_type
                ):
                    raise ValueError("candidate artifact PNG chunk is invalid")
                if not saw_ihdr and chunk_type != b"IHDR":
                    raise ValueError("candidate artifact PNG header is invalid")
                if chunk_type == b"IHDR":
                    if saw_ihdr or chunk_length != 13:
                        raise ValueError("candidate artifact PNG header is invalid")
                    payload = stream.read(13)
                    if len(payload) != 13:
                        raise ValueError("candidate artifact is not a complete PNG image")
                    digest.update(payload)
                    width, height = struct.unpack(">II", payload[:8])
                    bit_depth, color_type, compression, filtering, interlace = payload[8:]
                    if (
                        width <= 0
                        or height <= 0
                        or width * height > MAX_TEMPLATE_PIXELS
                        or bit_depth != 8
                        or color_type != 2
                        or compression != 0
                        or filtering != 0
                        or interlace != 0
                    ):
                        raise ValueError(
                            f"candidate {artifact_label} has invalid dimensions or PNG format"
                        )
                    crc = zlib.crc32(chunk_type)
                    crc = zlib.crc32(payload, crc)
                    saw_ihdr = True
                else:
                    crc = zlib.crc32(chunk_type)
                    remaining = chunk_length
                    while remaining:
                        payload = stream.read(min(1024 * 1024, remaining))
                        if not payload:
                            raise ValueError(
                                "candidate artifact is not a complete PNG image"
                            )
                        digest.update(payload)
                        crc = zlib.crc32(payload, crc)
                        remaining -= len(payload)
                    if chunk_type == b"IDAT":
                        saw_idat = True
                    elif chunk_type == b"IEND":
                        if chunk_length != 0 or not saw_idat:
                            raise ValueError("candidate artifact PNG ending is invalid")
                        saw_iend = True
                raw_crc = stream.read(4)
                if len(raw_crc) != 4:
                    raise ValueError("candidate artifact is not a complete PNG image")
                digest.update(raw_crc)
                if struct.unpack(">I", raw_crc)[0] != (crc & 0xFFFFFFFF):
                    raise ValueError("candidate artifact PNG checksum is invalid")
            if stream.read(1):
                raise ValueError("candidate artifact PNG has trailing bytes")
    except OSError as exc:
        raise ValueError("candidate artifact file is missing or too large") from exc
    return _file_manifest(
        sha256=digest.hexdigest(),
        byte_size=size,
        width=width,
        height=height,
        channels=3,
    )


def validate_uploaded_artifact_file(
    path: str | os.PathLike[str],
    kind: str,
) -> dict[str, Any]:
    """Validate one staged upload before it becomes visible to validation."""

    if kind not in _ALLOWED_KINDS and not _MULTI_EVIDENCE_KIND_RE.fullmatch(kind):
        raise ValueError("candidate artifact kind is invalid")
    candidate = Path(path)
    if not candidate.is_file():
        raise ValueError("candidate artifact file is missing or too large")
    if kind in {
        "evidence",
        "canonical_template",
        TEMPLATE_OCR_EVIDENCE_KIND,
    } or _MULTI_EVIDENCE_KIND_RE.fullmatch(kind):
        return _validate_streaming_rgb_png(
            candidate,
            artifact_label=kind.replace("_", " "),
        )
    raw = candidate.read_bytes()
    image = _decode_uploaded_png_bytes(raw, mask=kind == "source_mask")
    height, width = image.shape[:2]
    return _file_manifest(
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_size=len(raw),
        width=width,
        height=height,
        channels=1 if kind == "source_mask" else 3,
    )


def _projected_quad(matrix: np.ndarray, source_width: int, source_height: int) -> np.ndarray:
    source_corners = np.asarray(
        [[0, 0], [source_width - 1, 0], [source_width - 1, source_height - 1], [0, source_height - 1]],
        dtype=np.float32,
    ).reshape(-1, 1, 2)
    projected = cv2.perspectiveTransform(source_corners, matrix).reshape(-1, 2)
    if not np.isfinite(projected).all():
        raise ValueError("homography projects source corners outside finite space")
    contour = projected.astype(np.float32).reshape(-1, 1, 2)
    if not cv2.isContourConvex(contour):
        raise ValueError("homography projected quadrilateral is not convex")
    signed_area = float(cv2.contourArea(contour, oriented=True))
    if signed_area <= 0:
        raise ValueError("homography reflects the source page")
    return projected


def _structural_alignment_quality(
    aligned_bgr: np.ndarray,
    template_bgr: np.ndarray,
) -> tuple[float, float, float, float, int, int]:
    height, width = template_bgr.shape[:2]
    scale = min(1.0, STRUCTURAL_MAX_EDGE / float(max(height, width)))
    if scale < 1.0:
        output_size = (
            max(1, round(width * scale)),
            max(1, round(height * scale)),
        )
        aligned_bgr = cv2.resize(
            aligned_bgr, output_size, interpolation=cv2.INTER_AREA
        )
        template_bgr = cv2.resize(
            template_bgr, output_size, interpolation=cv2.INTER_AREA
        )
    aligned_gray = cv2.cvtColor(aligned_bgr, cv2.COLOR_BGR2GRAY)
    template_gray = cv2.cvtColor(template_bgr, cv2.COLOR_BGR2GRAY)
    aligned_edges = cv2.Canny(
        cv2.GaussianBlur(aligned_gray, (3, 3), 0), 50, 150
    ) > 0
    template_edges = cv2.Canny(
        cv2.GaussianBlur(template_gray, (3, 3), 0), 50, 150
    ) > 0
    template_edge_pixels = int(np.count_nonzero(template_edges))
    aligned_edge_pixels = int(np.count_nonzero(aligned_edges))
    if template_edge_pixels < MIN_TEMPLATE_EDGE_PIXELS or aligned_edge_pixels <= 0:
        raise ValueError("template has insufficient structure for accelerator validation")
    aligned_nearby = cv2.dilate(
        aligned_edges.astype(np.uint8),
        np.ones((3, 3), dtype=np.uint8),
    ) > 0
    edge_recall = float(
        np.count_nonzero(template_edges & aligned_nearby)
    ) / float(template_edge_pixels)
    aligned_x = cv2.Sobel(aligned_gray, cv2.CV_32F, 1, 0, ksize=3)
    aligned_y = cv2.Sobel(aligned_gray, cv2.CV_32F, 0, 1, ksize=3)
    template_x = cv2.Sobel(template_gray, cv2.CV_32F, 1, 0, ksize=3)
    template_y = cv2.Sobel(template_gray, cv2.CV_32F, 0, 1, ksize=3)

    def cosine(left: np.ndarray, right: np.ndarray) -> float:
        denominator = math.sqrt(
            float(np.sum(left * left)) * float(np.sum(right * right))
        )
        if denominator <= 1e-9 or not math.isfinite(denominator):
            raise ValueError("accelerator structural gradient is invalid")
        return float(np.sum(left * right)) / denominator

    gradient_x_cosine = cosine(np.abs(aligned_x), np.abs(template_x))
    gradient_y_cosine = cosine(np.abs(aligned_y), np.abs(template_y))
    aligned_magnitude = np.sqrt(aligned_x * aligned_x + aligned_y * aligned_y)
    template_magnitude = np.sqrt(template_x * template_x + template_y * template_y)
    gradient_cosine = cosine(aligned_magnitude, template_magnitude)
    if not all(math.isfinite(value) for value in (
        edge_recall,
        gradient_cosine,
        gradient_x_cosine,
        gradient_y_cosine,
    )):
        raise ValueError("accelerator structural metrics are invalid")
    return (
        edge_recall,
        gradient_cosine,
        gradient_x_cosine,
        gradient_y_cosine,
        template_edge_pixels,
        aligned_edge_pixels,
    )


def _uint8_absdiff_metrics(
    left: np.ndarray,
    right: np.ndarray,
) -> tuple[float, float]:
    """Return mean and NumPy-compatible linear p99 without a full-size copy."""

    if left.shape != right.shape or left.dtype != np.uint8 or right.dtype != np.uint8:
        raise ValueError("accelerator replay images are incompatible")
    values_per_row = int(np.prod(left.shape[1:], dtype=np.int64))
    rows_per_chunk = max(1, _ABS_DIFF_CHUNK_BYTES // max(1, values_per_row))
    histogram = np.zeros(256, dtype=np.int64)
    for start in range(0, left.shape[0], rows_per_chunk):
        difference = cv2.absdiff(
            left[start : start + rows_per_chunk],
            right[start : start + rows_per_chunk],
        )
        histogram += np.bincount(difference.reshape(-1), minlength=256)

    total = int(histogram.sum())
    if total <= 0:
        raise ValueError("accelerator replay images are empty")
    weighted_sum = sum(index * int(count) for index, count in enumerate(histogram))
    mean = float(weighted_sum) / float(total)

    rank = (total - 1) * 0.99
    lower_rank = math.floor(rank)
    upper_rank = math.ceil(rank)
    cumulative = np.cumsum(histogram)
    lower_value = int(np.searchsorted(cumulative, lower_rank + 1, side="left"))
    upper_value = int(np.searchsorted(cumulative, upper_rank + 1, side="left"))
    percentile = float(lower_value + (upper_value - lower_value) * (rank - lower_rank))
    return mean, percentile


def replay_and_verify_artifacts(
    *,
    source_path: str | os.PathLike[str],
    template_path: str | os.PathLike[str],
    homography: Any,
    artifact_manifest: Any,
    aligned_page_path: str | os.PathLike[str],
    source_mask_path: str | os.PathLike[str],
) -> ReplayedArtifacts:
    manifest = validate_artifact_manifest(artifact_manifest)
    aligned_path = Path(aligned_page_path)
    mask_path = Path(source_mask_path)
    snapshots: dict[str, bytes] = {}
    for kind, path in (("aligned_page", aligned_path), ("source_mask", mask_path)):
        expected = manifest[kind]
        if not path.is_file():
            raise ValueError(f"candidate {kind} bytes do not match the manifest")
        raw = path.read_bytes()
        if (
            len(raw) != expected["byte_size"]
            or hashlib.sha256(raw).hexdigest() != expected["sha256"]
        ):
            raise ValueError(f"candidate {kind} bytes do not match the manifest")
        snapshots[kind] = raw
    mask_snapshot = snapshots.pop("source_mask")
    uploaded_mask = _decode_uploaded_png_bytes(mask_snapshot, mask=True)
    del mask_snapshot
    source = decode_raw_source(source_path)
    source_height, source_width = source.shape[:2]
    template_height = manifest["template_height"]
    template_width = manifest["template_width"]
    if (
        (source_width, source_height) != (manifest["source_width"], manifest["source_height"])
        or uploaded_mask.shape != (template_height, template_width)
    ):
        raise ValueError("candidate artifact dimensions do not match immutable assets")
    matrix = normalize_homography(homography)
    projected = _projected_quad(matrix, source_width, source_height)
    expected_aligned = cv2.warpPerspective(
        source,
        matrix,
        (template_width, template_height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255),
    )
    del source
    source_mask_input = np.full(
        (source_height, source_width), 255, dtype=np.uint8
    )
    expected_mask = cv2.warpPerspective(
        source_mask_input,
        matrix,
        (template_width, template_height),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    del source_mask_input
    if not np.array_equal(uploaded_mask, expected_mask):
        raise ValueError("candidate source mask does not replay exactly")
    del uploaded_mask

    aligned_snapshot = snapshots.pop("aligned_page")
    uploaded_aligned = _decode_uploaded_png_bytes(aligned_snapshot, mask=False)
    del aligned_snapshot, snapshots
    if uploaded_aligned.shape[:2] != (template_height, template_width):
        raise ValueError("candidate artifact dimensions do not match immutable assets")
    mae, p99 = _uint8_absdiff_metrics(uploaded_aligned, expected_aligned)
    del uploaded_aligned
    if not math.isfinite(mae) or not math.isfinite(p99) or mae > MAX_ALIGNED_MAE or p99 > MAX_ALIGNED_P99:
        raise ValueError("candidate aligned page does not match transform replay")
    template = decode_raw_template(template_path)
    if template.shape[:2] != (template_height, template_width):
        raise ValueError("candidate artifact dimensions do not match immutable assets")
    (
        edge_recall,
        gradient_cosine,
        gradient_x_cosine,
        gradient_y_cosine,
        template_edges,
        aligned_edges,
    ) = (
        _structural_alignment_quality(expected_aligned, template)
    )
    coverage = float(np.count_nonzero(expected_mask)) / float(expected_mask.size)
    return ReplayedArtifacts(
        aligned_rgb=expected_aligned[:, :, ::-1],
        source_mask=expected_mask,
        template_rgb=template[:, :, ::-1],
        coverage=coverage,
        aligned_mae=mae,
        aligned_p99=p99,
        projected_quad=[[float(item) for item in row] for row in projected.tolist()],
        template_edge_recall=edge_recall,
        gradient_cosine=gradient_cosine,
        template_edge_pixels=template_edges,
        aligned_edge_pixels=aligned_edges,
        gradient_x_cosine=gradient_x_cosine,
        gradient_y_cosine=gradient_y_cosine,
    )


def validate_bound_client_quality_report(
    report: Any,
    *,
    source_sha256: str,
    template_sha256: str,
    layout_snapshot_sha256: str,
    artifact_sha256: str,
    page_type_id: str,
    coverage: float,
) -> dict[str, Any]:
    """Check a bound device's versioned declaration, without pixel work.

    The caller must authenticate the bound device and its job ownership. These
    metrics are trusted execution results, not proof against a dishonest node.
    Keep this contract separate from the retired client-owned v1 artifacts.
    """
    from services.template_evidence import MIN_PAGE_COVERAGE

    identities = {
        "source_sha256": source_sha256,
        "template_sha256": template_sha256,
        "layout_snapshot_sha256": layout_snapshot_sha256,
        "artifact_sha256": artifact_sha256,
        "page_type_id": page_type_id,
    }
    if (
        not isinstance(report, dict)
        or set(report) != {"version", "metrics", *identities}
        or report.get("version") != LOCAL_FULL_COLUMN_CHECK_VERSION
        or any(report.get(key) != value for key, value in identities.items())
    ):
        raise ValueError("bound client quality report identity is invalid")
    fields = {
        "coverage", "aligned_replay_mae", "aligned_replay_p99",
        "template_edge_recall", "gradient_cosine", "gradient_x_cosine",
        "gradient_y_cosine", "template_edge_pixels", "aligned_edge_pixels",
    }
    metrics = report.get("metrics")
    if not isinstance(metrics, dict) or set(metrics) != fields:
        raise ValueError("bound client quality metrics are incomplete")
    for name, value in metrics.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("bound client quality metric is not finite")
        if name.endswith("_pixels") and (not isinstance(value, int) or value < 0):
            raise ValueError("bound client quality pixel count is invalid")
    if (
        isinstance(coverage, bool) or not isinstance(coverage, (int, float))
        or not math.isfinite(coverage) or not 0.0 <= coverage <= 1.0
    ):
        raise ValueError("bound client candidate coverage is invalid")
    if (
        not MIN_PAGE_COVERAGE <= metrics["coverage"] <= 1.0
        or abs(metrics["coverage"] - coverage) > 0.02
        or not 0.0 <= metrics["aligned_replay_mae"] <= MAX_ALIGNED_MAE
        or not 0.0 <= metrics["aligned_replay_p99"] <= MAX_ALIGNED_P99
        or not MIN_TEMPLATE_EDGE_RECALL <= metrics["template_edge_recall"] <= 1.0
        or not MIN_GRADIENT_COSINE <= metrics["gradient_cosine"] <= 1.0 + 1e-6
        or not MIN_AXIS_GRADIENT_COSINE <= metrics["gradient_x_cosine"] <= 1.0 + 1e-6
        or not MIN_AXIS_GRADIENT_COSINE <= metrics["gradient_y_cosine"] <= 1.0 + 1e-6
        or metrics["template_edge_pixels"] < MIN_TEMPLATE_EDGE_PIXELS
        or metrics["aligned_edge_pixels"] <= 0
    ):
        raise ValueError("bound client local image quality check failed")
    return {"version": LOCAL_FULL_COLUMN_CHECK_VERSION, **identities, "metrics": dict(metrics)}
