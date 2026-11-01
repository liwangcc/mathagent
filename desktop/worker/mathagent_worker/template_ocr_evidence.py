from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image


TEMPLATE_OCR_EVIDENCE_STAGE = "template_ocr_evidence"
TEMPLATE_OCR_EVIDENCE_CONTRACT_VERSION = "template-ocr-evidence-v2"
MAX_DECODE_PIXELS = 16_000_000
MAX_OUTPUT_PIXELS = 16_000_000

_PLAN_VERSION = 2
_PLAN_STRATEGY = "full_height_columns"
_LEGACY_PLAN_VERSION = 1
_LEGACY_PLAN_STRATEGY = "top_anchor_continuous_half_columns"
_COORDINATE_SPACE = "template_1000"
_LEGACY_COLUMNS = ("left", "right")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_PIXEL_ENDPOINT_VERSION = "rounded_source_pixels_v1"
_COLUMN_METADATA_KEYS = (
    "column_id",
    "column_order",
    "layout_revision",
    "crop_left_px",
    "crop_right_px",
    "crop_box_px",
    "actual_pixel_endpoints",
    "crop_endpoint_version",
    "crop_coordinate_space",
    "source_width",
    "source_height",
    "source_dimensions",
    "image_sha256",
)
_LAYOUT_CONTEXT_KEYS = (
    "layout_revision",
    "coordinate_space",
    "crop_endpoint_version",
    "source_width",
    "source_height",
    "image_sha256",
    "image_hash",
    "source_sha256",
    "contract_version",
    "source_dimensions",
)


@dataclass(frozen=True)
class TemplateOcrEvidenceResult:
    artifact_path: Path
    candidate: dict[str, Any]


def _fail(code: str) -> None:
    raise RuntimeError(code)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _bounded_int(value: object, minimum: int, maximum: int) -> int:
    if not _is_int(value) or not minimum <= int(value) <= maximum:
        _fail("question_crop_plan_invalid")
    return int(value)


def _question_numbers(values: object) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        _fail("question_crop_plan_invalid")
    result: list[str] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            _fail("question_crop_plan_invalid")
        number = str(value).strip()
        if (
            not number
            or len(number) > 64
            or any(ord(character) < 32 for character in number)
        ):
            _fail("question_crop_plan_invalid")
        if number not in result:
            result.append(number)
    return result


def _normalize_column_metadata(
    raw: dict[str, Any],
    *,
    index: int,
    left: int,
    right: int,
) -> dict[str, Any]:
    """Validate and canonicalize the frozen pixel/identity metadata.

    The server keeps this metadata beside normalized 0..1000 boundaries so a
    worker can prove that it is rendering the exact frozen crop.  Keep the
    worker-side copy deliberately small and deterministic; unknown fields are
    rejected by the surrounding strict plan comparison.
    """
    metadata: dict[str, Any] = {}
    if "column_id" in raw:
        value = raw.get("column_id")
        if not isinstance(value, str) or not value.strip():
            _fail("question_crop_plan_invalid")
        metadata["column_id"] = value.strip()

    order_values = [
        raw[key]
        for key in ("column_order", "order", "ordinal")
        if key in raw
    ]
    if order_values:
        if any(not _is_int(value) or int(value) != index for value in order_values):
            _fail("question_crop_plan_invalid")
        metadata["column_order"] = index

    if "layout_revision" in raw:
        revision = raw.get("layout_revision")
        if not _is_int(revision) or int(revision) < 1:
            _fail("question_crop_plan_invalid")
        metadata["layout_revision"] = int(revision)

    for key in ("source_width", "source_height"):
        if key in raw:
            value = raw.get(key)
            if not _is_int(value) or int(value) <= 0:
                _fail("question_crop_plan_invalid")
            metadata[key] = int(value)
    if "source_dimensions" in raw:
        dimensions = raw.get("source_dimensions")
        if (
            not isinstance(dimensions, dict)
            or not _is_int(dimensions.get("width"))
            or not _is_int(dimensions.get("height"))
            or int(dimensions["width"]) <= 0
            or int(dimensions["height"]) <= 0
        ):
            _fail("question_crop_plan_invalid")
        metadata["source_dimensions"] = {
            "width": int(dimensions["width"]),
            "height": int(dimensions["height"]),
        }
        if (
            "source_width" in metadata
            and metadata["source_width"] != metadata["source_dimensions"]["width"]
        ) or (
            "source_height" in metadata
            and metadata["source_height"] != metadata["source_dimensions"]["height"]
        ):
            _fail("question_crop_plan_invalid")

    hashes: list[str] = []
    for key in ("image_sha256", "image_hash", "source_sha256"):
        if key not in raw:
            continue
        value = str(raw.get(key) or "").strip().lower()
        if not _SHA256_RE.fullmatch(value):
            _fail("question_crop_plan_invalid")
        hashes.append(value)
    if hashes and len(set(hashes)) != 1:
        _fail("question_crop_plan_invalid")
    if hashes:
        metadata["image_sha256"] = hashes[0]

    endpoint_version = raw.get("crop_endpoint_version")
    if endpoint_version is not None:
        if str(endpoint_version) != _PIXEL_ENDPOINT_VERSION:
            _fail("question_crop_plan_invalid")
        metadata["crop_endpoint_version"] = _PIXEL_ENDPOINT_VERSION

    endpoint_values: dict[str, int] = {}
    for canonical, aliases in {
        "left": ("crop_left_px", "crop_left", "pixel_crop_left"),
        "right": ("crop_right_px", "crop_right", "pixel_crop_right"),
    }.items():
        seen: list[int] = []
        for key in aliases:
            if key not in raw:
                continue
            value = raw.get(key)
            if not _is_int(value):
                _fail("question_crop_plan_invalid")
            seen.append(int(value))
        if seen and len(set(seen)) != 1:
            _fail("question_crop_plan_invalid")
        if seen:
            endpoint_values[canonical] = seen[0]

    actual = raw.get("actual_pixel_endpoints")
    if actual is not None:
        if not isinstance(actual, dict):
            _fail("question_crop_plan_invalid")
        for canonical in ("left", "right"):
            value = actual.get(canonical)
            if not _is_int(value):
                _fail("question_crop_plan_invalid")
            value = int(value)
            if canonical in endpoint_values and endpoint_values[canonical] != value:
                _fail("question_crop_plan_invalid")
            endpoint_values[canonical] = value

    if endpoint_values:
        if (
            set(endpoint_values) != {"left", "right"}
            or endpoint_values["left"] < 0
            or endpoint_values["right"] <= endpoint_values["left"]
        ):
            _fail("question_crop_plan_invalid")
        source_width = metadata.get("source_width")
        dimensions = metadata.get("source_dimensions")
        if source_width is None and isinstance(dimensions, dict):
            source_width = dimensions.get("width")
        if source_width is not None:
            expected = {
                "left": round(left * int(source_width) / 1000),
                "right": round(right * int(source_width) / 1000),
            }
            if endpoint_values != expected:
                _fail("question_crop_plan_invalid")
        metadata.update({
            "crop_left_px": endpoint_values["left"],
            "crop_right_px": endpoint_values["right"],
            "actual_pixel_endpoints": dict(endpoint_values),
        })

        crop_box = raw.get("crop_box_px")
        if crop_box is not None:
            if not isinstance(crop_box, (list, tuple)) or len(crop_box) != 4:
                _fail("question_crop_plan_invalid")
            if any(not _is_int(value) or int(value) < 0 for value in crop_box):
                _fail("question_crop_plan_invalid")
            source_height = metadata.get("source_height")
            if source_height is None and isinstance(dimensions, dict):
                source_height = dimensions.get("height")
            expected_bottom = int(source_height) if source_height is not None else int(crop_box[3])
            if [int(value) for value in crop_box] != [
                endpoint_values["left"],
                0,
                endpoint_values["right"],
                expected_bottom,
            ]:
                _fail("question_crop_plan_invalid")
            metadata["crop_box_px"] = [int(value) for value in crop_box]

        coordinate_space = raw.get("crop_coordinate_space")
        if coordinate_space is not None:
            if str(coordinate_space) != "source_pixels":
                _fail("question_crop_plan_invalid")
            metadata["crop_coordinate_space"] = "source_pixels"
    elif "crop_box_px" in raw or "crop_coordinate_space" in raw:
        # These fields only have meaning when exact pixel endpoints are present.
        _fail("question_crop_plan_invalid")
    return metadata


def _normalize_layout_context(raw: object, *, canonical_sha256: str) -> dict[str, Any]:
    """Validate the optional layout identity attached to a frozen plan."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        _fail("question_crop_plan_invalid")
    if any(key not in _LAYOUT_CONTEXT_KEYS for key in raw):
        _fail("question_crop_plan_invalid")
    context = dict(raw)
    if "layout_revision" in context and (
        not _is_int(context["layout_revision"]) or int(context["layout_revision"]) < 1
    ):
        _fail("question_crop_plan_invalid")
    for key in ("source_width", "source_height"):
        if key in context and (
            not _is_int(context[key]) or int(context[key]) <= 0
        ):
            _fail("question_crop_plan_invalid")
    dimensions = context.get("source_dimensions")
    if dimensions is not None:
        if (
            not isinstance(dimensions, dict)
            or not _is_int(dimensions.get("width"))
            or not _is_int(dimensions.get("height"))
            or int(dimensions["width"]) <= 0
            or int(dimensions["height"]) <= 0
        ):
            _fail("question_crop_plan_invalid")
        context["source_dimensions"] = {
            "width": int(dimensions["width"]),
            "height": int(dimensions["height"]),
        }
        if "source_width" in context and context["source_width"] != dimensions["width"]:
            _fail("question_crop_plan_invalid")
        if "source_height" in context and context["source_height"] != dimensions["height"]:
            _fail("question_crop_plan_invalid")
    if "crop_endpoint_version" in context and str(
        context["crop_endpoint_version"]
    ) != _PIXEL_ENDPOINT_VERSION:
        _fail("question_crop_plan_invalid")
    hashes: list[str] = []
    for key in ("image_sha256", "image_hash", "source_sha256"):
        if key not in context:
            continue
        value = str(context[key] or "").strip().lower()
        if not _SHA256_RE.fullmatch(value):
            _fail("question_crop_plan_invalid")
        hashes.append(value)
    if hashes and len(set(hashes)) != 1:
        _fail("question_crop_plan_invalid")
    if hashes and hashes[0] != canonical_sha256:
        _fail("question_crop_plan_source_mismatch")
    return context


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RuntimeError("layout_snapshot_invalid") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RuntimeError("canonical_image_read_failed") from exc
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        _fail("job_identity_invalid")
    return normalized


def _validate_legacy_anchor_crop_plan(
    raw_plan: object,
    *,
    canonical_sha256: str,
) -> dict[str, Any]:
    if not isinstance(raw_plan, dict):
        _fail("question_crop_plan_invalid")
    if (
        raw_plan.get("version") != _LEGACY_PLAN_VERSION
        or isinstance(raw_plan.get("version"), bool)
        or raw_plan.get("strategy") != _LEGACY_PLAN_STRATEGY
        or raw_plan.get("coordinate_space") != _COORDINATE_SPACE
        or raw_plan.get("provider_bottom_ignored") is not True
        or raw_plan.get("requires_question_number_gate") is not True
    ):
        _fail("question_crop_plan_invalid")

    source_sha256 = str(
        raw_plan.get("source_artifact_sha256") or ""
    ).strip().lower()
    if (
        not _SHA256_RE.fullmatch(source_sha256)
        or source_sha256 != canonical_sha256
    ):
        _fail("question_crop_plan_source_mismatch")

    parameters = raw_plan.get("parameters")
    if not isinstance(parameters, dict):
        _fail("question_crop_plan_invalid")
    split_x = _bounded_int(parameters.get("split_x_1000"), 250, 750)
    column_overlap = _bounded_int(
        parameters.get("column_overlap_1000"),
        0,
        min(split_x, 1000 - split_x),
    )
    vertical_overlap = _bounded_int(
        parameters.get("vertical_overlap_1000"), 0, 100
    )
    anchors_per_block = _bounded_int(
        parameters.get("anchors_per_block"), 2, 3
    )
    tolerance = _bounded_int(
        parameters.get("anchor_merge_tolerance_1000"), 0, 40
    )

    raw_anchors = raw_plan.get("anchors")
    if not isinstance(raw_anchors, list):
        _fail("question_crop_plan_invalid")
    anchors: list[dict[str, Any]] = []
    anchor_ids: set[str] = set()
    last_top = {column: -1 for column in _LEGACY_COLUMNS}
    for raw_anchor in raw_anchors:
        if not isinstance(raw_anchor, dict):
            _fail("question_crop_plan_invalid")
        anchor_id = str(raw_anchor.get("anchor_id") or "").strip()
        column = str(raw_anchor.get("column") or "").strip()
        if (
            column not in _LEGACY_COLUMNS
            or not re.fullmatch(rf"{column}-\d+", anchor_id)
            or anchor_id in anchor_ids
        ):
            _fail("question_crop_plan_invalid")
        top = _bounded_int(raw_anchor.get("top_1000"), 0, 999)
        if top <= last_top[column]:
            _fail("question_crop_plan_invalid")
        source_hashes = raw_anchor.get("source_candidate_sha256")
        if not isinstance(source_hashes, list) or not source_hashes:
            _fail("question_crop_plan_invalid")
        canonical_hashes = sorted(
            {str(value).strip().lower() for value in source_hashes}
        )
        if any(not _SHA256_RE.fullmatch(value) for value in canonical_hashes):
            _fail("question_crop_plan_invalid")
        anchors.append({
            "anchor_id": anchor_id,
            "column": column,
            "top_1000": top,
            "question_number_hints": _question_numbers(
                raw_anchor.get("question_number_hints") or []
            ),
            "source_candidate_sha256": canonical_hashes,
            "source_candidate_count": _bounded_int(
                raw_anchor.get("source_candidate_count"), 1, 1000
            ),
        })
        anchor_ids.add(anchor_id)
        last_top[column] = top

    raw_blocks = raw_plan.get("blocks")
    if not isinstance(raw_blocks, list) or not raw_blocks:
        _fail("question_crop_plan_invalid")
    blocks: list[dict[str, Any]] = []
    block_ids: set[str] = set()
    used_anchor_ids: list[str] = []
    anchor_columns = {
        anchor["anchor_id"]: anchor["column"] for anchor in anchors
    }
    for raw_block in raw_blocks:
        if not isinstance(raw_block, dict):
            _fail("question_crop_plan_invalid")
        block_id = str(raw_block.get("block_id") or "").strip()
        column = str(raw_block.get("column") or "").strip()
        if (
            column not in _LEGACY_COLUMNS
            or not re.fullmatch(rf"{column}-\d+", block_id)
            or block_id in block_ids
        ):
            _fail("question_crop_plan_invalid")
        bbox = raw_block.get("bbox_1000")
        if (
            not isinstance(bbox, (list, tuple))
            or len(bbox) != 4
            or any(not _is_int(value) for value in bbox)
        ):
            _fail("question_crop_plan_invalid")
        left, top, right, bottom = (int(value) for value in bbox)
        expected_x = (
            [0, min(1000, split_x + column_overlap)]
            if column == "left"
            else [max(0, split_x - column_overlap), 1000]
        )
        if [left, right] != expected_x or not 0 <= top < bottom <= 1000:
            _fail("question_crop_plan_invalid")
        raw_anchor_ids = raw_block.get("source_anchor_ids")
        if (
            not isinstance(raw_anchor_ids, list)
            or len(raw_anchor_ids) > anchors_per_block
        ):
            _fail("question_crop_plan_invalid")
        source_anchor_ids = [str(value).strip() for value in raw_anchor_ids]
        if (
            len(source_anchor_ids) != len(set(source_anchor_ids))
            or any(
                anchor_id not in anchor_columns
                or anchor_columns[anchor_id] != column
                for anchor_id in source_anchor_ids
            )
        ):
            _fail("question_crop_plan_invalid")
        fallback = raw_block.get("fallback_full_column")
        if not isinstance(fallback, bool) or fallback != (not source_anchor_ids):
            _fail("question_crop_plan_invalid")
        blocks.append({
            "block_id": block_id,
            "column": column,
            "bbox_1000": [left, top, right, bottom],
            "source_anchor_ids": source_anchor_ids,
            "question_number_hints": _question_numbers(
                raw_block.get("question_number_hints") or []
            ),
            "fallback_full_column": fallback,
        })
        block_ids.add(block_id)
        used_anchor_ids.extend(source_anchor_ids)

    if sorted(used_anchor_ids) != sorted(anchor_ids):
        _fail("question_crop_plan_invalid")
    for column in _LEGACY_COLUMNS:
        column_blocks = sorted(
            (block for block in blocks if block["column"] == column),
            key=lambda item: (item["bbox_1000"][1], item["block_id"]),
        )
        if (
            not column_blocks
            or column_blocks[0]["bbox_1000"][1] != 0
            or column_blocks[-1]["bbox_1000"][3] != 1000
        ):
            _fail("question_crop_plan_coverage_invalid")
        if any(
            following["bbox_1000"][1] > previous["bbox_1000"][3]
            for previous, following in zip(column_blocks, column_blocks[1:])
        ):
            _fail("question_crop_plan_coverage_invalid")

    warnings = raw_plan.get("warnings") or []
    if (
        not isinstance(warnings, list)
        or any(not isinstance(value, str) for value in warnings)
    ):
        _fail("question_crop_plan_invalid")
    source_summary = raw_plan.get("source_summary") or {}
    if not isinstance(source_summary, dict):
        _fail("question_crop_plan_invalid")
    trusted_count = _bounded_int(
        source_summary.get("trusted_candidate_count", len(anchors)), 0, 10000
    )
    rejected_count = _bounded_int(
        source_summary.get("rejected_candidate_count", 0), 0, 10000
    )
    expected_numbers = _question_numbers(
        raw_plan.get("expected_question_numbers") or []
    )
    return {
        "source_artifact_sha256": source_sha256,
        "parameters": {"split_x_1000": split_x},
        "expected_question_numbers": expected_numbers,
        "warnings": sorted(set(warnings)),
        "source_summary": {
            "trusted_candidate_count": trusted_count,
            "rejected_candidate_count": rejected_count,
        },
    }


def _canonical_columns(
    value: object,
    *,
    column_count: object,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 3:
        _fail("question_crop_plan_invalid")
    count = _bounded_int(column_count, 1, 3)
    if count != len(value):
        _fail("question_crop_plan_invalid")
    columns: list[dict[str, Any]] = []
    previous_right = 0
    for position, raw in enumerate(value, 1):
        if not isinstance(raw, dict):
            _fail("question_crop_plan_invalid")
        index = _bounded_int(raw.get("column_index"), 1, 3)
        left = _bounded_int(raw.get("x1_1000"), 0, 999)
        right = _bounded_int(raw.get("x2_1000"), 1, 1000)
        if index != position or left != previous_right or right <= left:
            _fail("question_crop_plan_coverage_invalid")
        column: dict[str, Any] = {
            "column_index": index,
            "x1_1000": left,
            "x2_1000": right,
        }
        column.update(
            _normalize_column_metadata(
                raw,
                index=index,
                left=left,
                right=right,
            )
        )
        columns.append(column)
        previous_right = right
    if previous_right != 1000:
        _fail("question_crop_plan_coverage_invalid")

    # A frozen plan must not combine column identities from different source
    # images or layout revisions.  Metadata remains optional for legacy plans.
    for key in ("layout_revision", "source_width", "source_height", "image_sha256"):
        values = {column[key] for column in columns if key in column}
        if values and len(values) != 1:
            _fail("question_crop_plan_invalid")
    column_ids = [column.get("column_id") for column in columns]
    if any(value is not None for value in column_ids):
        if any(value is None for value in column_ids) or len(set(column_ids)) != len(column_ids):
            _fail("question_crop_plan_invalid")
    return columns


def _validate_full_height_column_plan(
    raw_plan: object,
    *,
    canonical_sha256: str,
) -> dict[str, Any]:
    if not isinstance(raw_plan, dict):
        _fail("question_crop_plan_invalid")
    if (
        raw_plan.get("version") != _PLAN_VERSION
        or isinstance(raw_plan.get("version"), bool)
        or raw_plan.get("strategy") != _PLAN_STRATEGY
        or raw_plan.get("coordinate_space") != _COORDINATE_SPACE
        or raw_plan.get("provider_bottom_ignored") is not True
        or raw_plan.get("requires_question_number_gate") is not True
    ):
        _fail("question_crop_plan_invalid")

    source_sha256 = str(
        raw_plan.get("source_artifact_sha256") or ""
    ).strip().lower()
    if (
        not _SHA256_RE.fullmatch(source_sha256)
        or source_sha256 != canonical_sha256
    ):
        _fail("question_crop_plan_source_mismatch")
    parameters = raw_plan.get("parameters")
    if not isinstance(parameters, dict):
        _fail("question_crop_plan_invalid")
    columns = _canonical_columns(
        parameters.get("columns"),
        column_count=parameters.get("column_count"),
    )
    layout_context = _normalize_layout_context(
        raw_plan.get("layout_context"),
        canonical_sha256=source_sha256,
    )
    # Column metadata is part of the immutable source/layout identity.  It is
    # not enough for columns to agree with one another: their hash, revision,
    # and declared dimensions must also agree with the enclosing context and
    # the source artifact named by the job.
    column_hashes = {
        column["image_sha256"]
        for column in columns
        if "image_sha256" in column
    }
    if any(value != source_sha256 for value in column_hashes):
        _fail("question_crop_plan_source_mismatch")
    context_hashes = {
        str(layout_context[key]).strip().lower()
        for key in ("image_sha256", "image_hash", "source_sha256")
        if key in layout_context
    }
    if any(value != source_sha256 for value in context_hashes):
        _fail("question_crop_plan_source_mismatch")

    for key in ("layout_revision", "source_width", "source_height"):
        context_value = layout_context.get(key)
        column_values = {
            column[key] for column in columns if key in column
        }
        if context_value is not None and any(
            value != context_value for value in column_values
        ):
            _fail("question_crop_plan_invalid")

    context_dimensions = layout_context.get("source_dimensions")
    if context_dimensions is not None:
        for column in columns:
            column_dimensions = column.get("source_dimensions")
            if (
                column_dimensions is not None
                and column_dimensions != context_dimensions
            ):
                _fail("question_crop_plan_invalid")
    if raw_plan.get("anchors") != []:
        _fail("question_crop_plan_invalid")

    raw_blocks = raw_plan.get("blocks")
    if not isinstance(raw_blocks, list) or len(raw_blocks) != len(columns):
        _fail("question_crop_plan_invalid")
    blocks: list[dict[str, Any]] = []
    for column, raw in zip(columns, raw_blocks):
        if not isinstance(raw, dict):
            _fail("question_crop_plan_invalid")
        column_name = f"col_{column['column_index']}"
        expected_bbox = [column["x1_1000"], 0, column["x2_1000"], 1000]
        if (
            str(raw.get("block_id") or "") != column_name
            or str(raw.get("column") or "") != column_name
            or raw.get("bbox_1000") != expected_bbox
            or raw.get("source_anchor_ids") != []
            or raw.get("fallback_full_column") is not True
        ):
            _fail("question_crop_plan_invalid")
        expected_metadata = {
            key: column[key]
            for key in _COLUMN_METADATA_KEYS
            if key in column
        }
        for key, expected_value in expected_metadata.items():
            if raw.get(key) != expected_value:
                _fail("question_crop_plan_invalid")
        for key in raw:
            if key in _COLUMN_METADATA_KEYS and key not in expected_metadata:
                _fail("question_crop_plan_invalid")
        block = {
            "block_id": column_name,
            "column": column_name,
            "bbox_1000": expected_bbox,
            "source_anchor_ids": [],
            "question_number_hints": _question_numbers(
                raw.get("question_number_hints") or []
            ),
            "fallback_full_column": True,
        }
        block.update(expected_metadata)
        blocks.append(block)

    warnings = raw_plan.get("warnings") or []
    if (
        not isinstance(warnings, list)
        or any(not isinstance(value, str) for value in warnings)
    ):
        _fail("question_crop_plan_invalid")
    source_summary = raw_plan.get("source_summary") or {}
    if not isinstance(source_summary, dict):
        _fail("question_crop_plan_invalid")
    trusted_count = _bounded_int(
        source_summary.get("trusted_candidate_count", len(columns)), 0, 10000
    )
    rejected_count = _bounded_int(
        source_summary.get("rejected_candidate_count", 0), 0, 10000
    )
    expected_numbers = _question_numbers(
        raw_plan.get("expected_question_numbers") or []
    )
    result = {
        "version": _PLAN_VERSION,
        "strategy": _PLAN_STRATEGY,
        "coordinate_space": _COORDINATE_SPACE,
        "source_artifact_sha256": source_sha256,
        "layout_context": layout_context,
        "provider_bottom_ignored": True,
        "requires_question_number_gate": True,
        "parameters": {
            "column_count": len(columns),
            "columns": columns,
        },
        "expected_question_numbers": expected_numbers,
        "anchors": [],
        "blocks": blocks,
        "warnings": sorted(set(warnings)),
        "hint_coverage": {
            "observed_question_number_hints": [],
            "missing_expected_question_numbers": [],
            "unexpected_question_number_hints": [],
        },
        "source_summary": {
            "trusted_candidate_count": trusted_count,
            "rejected_candidate_count": rejected_count,
        },
        "coverage": {
            "columns": [block["column"] for block in blocks],
            "page_top_to_bottom": True,
            "vertical_gaps": 0,
            "block_count": len(blocks),
            "anchor_count": 0,
        },
    }
    # Older callers omitted this optional identity object.  Preserve their
    # exact shape while retaining it whenever the server supplied it.
    if "layout_context" not in raw_plan:
        result.pop("layout_context", None)
    return result


def _migrate_legacy_plan(plan: dict[str, Any]) -> dict[str, Any]:
    split_x = int(plan["parameters"]["split_x_1000"])
    columns = [
        {"column_index": 1, "x1_1000": 0, "x2_1000": split_x},
        {"column_index": 2, "x1_1000": split_x, "x2_1000": 1000},
    ]
    migrated = {
        "version": _PLAN_VERSION,
        "strategy": _PLAN_STRATEGY,
        "coordinate_space": _COORDINATE_SPACE,
        "source_artifact_sha256": plan["source_artifact_sha256"],
        "provider_bottom_ignored": True,
        "requires_question_number_gate": True,
        "parameters": {"column_count": 2, "columns": columns},
        "expected_question_numbers": list(
            plan.get("expected_question_numbers") or []
        ),
        "anchors": [],
        "blocks": [
            {
                "block_id": f"col_{column['column_index']}",
                "column": f"col_{column['column_index']}",
                "bbox_1000": [
                    column["x1_1000"],
                    0,
                    column["x2_1000"],
                    1000,
                ],
                "source_anchor_ids": [],
                "question_number_hints": [],
                "fallback_full_column": True,
            }
            for column in columns
        ],
        "warnings": sorted(
            set([*(plan.get("warnings") or []), "migrated_from_v1"])
        ),
        "source_summary": dict(plan.get("source_summary") or {}),
    }
    return _validate_full_height_column_plan(
        migrated,
        canonical_sha256=plan["source_artifact_sha256"],
    )


def _validate_anchor_crop_plan(
    raw_plan: object,
    *,
    canonical_sha256: str,
) -> dict[str, Any]:
    if not isinstance(raw_plan, dict):
        _fail("question_crop_plan_invalid")
    version = raw_plan.get("version")
    if version == _LEGACY_PLAN_VERSION and not isinstance(version, bool):
        legacy = _validate_legacy_anchor_crop_plan(
            raw_plan,
            canonical_sha256=canonical_sha256,
        )
        return _migrate_legacy_plan(legacy)
    plan = _validate_full_height_column_plan(
        raw_plan,
        canonical_sha256=canonical_sha256,
    )
    if plan != raw_plan:
        _fail("question_crop_plan_invalid")
    return plan


def _validate_plan_source_geometry(
    plan: dict[str, Any],
    *,
    source_width: int,
    source_height: int,
) -> None:
    """Bind optional frozen pixel metadata to the decoded source dimensions.

    Normalized boundaries are safe to resolve against the image that was
    actually downloaded, but an issued plan may also carry pixel endpoints
    from the image used when the layout was frozen.  Do not silently rescale
    those endpoints: doing so would make a stale plan look valid while moving
    the crop.  The server's evidence renderer applies the same invariant.
    """
    context = plan.get("layout_context") or {}
    metadata_items = [context]
    columns = plan.get("parameters", {}).get("columns", [])
    metadata_items.extend(columns)

    for metadata in metadata_items:
        if not isinstance(metadata, dict):
            _fail("question_crop_plan_invalid")
        for key, actual in (
            ("source_width", source_width),
            ("source_height", source_height),
        ):
            if key in metadata and metadata[key] != actual:
                _fail("question_crop_plan_invalid")
        dimensions = metadata.get("source_dimensions")
        if dimensions is not None:
            if (
                not isinstance(dimensions, dict)
                or dimensions.get("width") != source_width
                or dimensions.get("height") != source_height
            ):
                _fail("question_crop_plan_invalid")

    for column in columns:
        left = int(column["x1_1000"])
        right = int(column["x2_1000"])
        expected = {
            "left": round(left * source_width / 1000),
            "right": round(right * source_width / 1000),
        }
        actual_endpoints = column.get("actual_pixel_endpoints")
        if actual_endpoints is not None and actual_endpoints != expected:
            _fail("question_crop_plan_invalid")
        for key, expected_value in (
            ("crop_left_px", expected["left"]),
            ("crop_right_px", expected["right"]),
        ):
            if key in column and column[key] != expected_value:
                _fail("question_crop_plan_invalid")
        crop_box = column.get("crop_box_px")
        if crop_box is not None and crop_box != [
            expected["left"],
            0,
            expected["right"],
            source_height,
        ]:
            _fail("question_crop_plan_invalid")


def validate_template_ocr_evidence_job(
    job: dict[str, Any],
) -> tuple[dict[str, Any], str, str]:
    if job.get("stage") != TEMPLATE_OCR_EVIDENCE_STAGE:
        _fail("unsupported_accelerator_stage")
    source_sha256 = _sha256_text(str(job.get("source_sha256") or ""))
    layout_sha256 = _sha256_text(
        str(job.get("layout_snapshot_sha256") or "")
    )
    layout_snapshot = job.get("layout_snapshot")
    if not isinstance(layout_snapshot, dict) or not layout_snapshot:
        _fail("layout_snapshot_invalid")
    actual_layout_sha256 = hashlib.sha256(
        _canonical_json(layout_snapshot)
    ).hexdigest()
    if actual_layout_sha256 != layout_sha256:
        _fail("layout_snapshot_hash_mismatch")
    raw_plan = (
        layout_snapshot.get("question_crop_plan")
        if "question_crop_plan" in layout_snapshot
        else layout_snapshot
    )
    plan = _validate_anchor_crop_plan(
        raw_plan,
        canonical_sha256=source_sha256,
    )
    return plan, source_sha256, layout_sha256


def _artifact_sha256(path: Path) -> str:
    return _sha256_file(path)


def _render_montage(
    source: Image.Image,
    plan: dict[str, Any],
) -> Image.Image:
    page_width, page_height = source.size
    crop_boxes: list[tuple[int, int, int, int]] = []
    canvas_width = 0
    canvas_height = 0
    for block in plan["blocks"]:
        left, top, right, bottom = block["bbox_1000"]
        x1 = max(
            0,
            min(page_width - 1, int(round(left * page_width / 1000))),
        )
        y1 = max(
            0,
            min(page_height - 1, int(round(top * page_height / 1000))),
        )
        x2 = max(
            x1 + 1,
            min(page_width, int(round(right * page_width / 1000))),
        )
        y2 = max(
            y1 + 1,
            min(page_height, int(round(bottom * page_height / 1000))),
        )
        crop_boxes.append((x1, y1, x2, y2))
        canvas_width += x2 - x1
        canvas_height = max(canvas_height, y2 - y1)
    if any((y2 - y1) != canvas_height for _x1, y1, _x2, y2 in crop_boxes):
        _fail("question_crop_plan_coverage_invalid")
    if (
        canvas_width <= 0
        or canvas_height <= 0
        or canvas_width * canvas_height > MAX_OUTPUT_PIXELS
    ):
        _fail("template_ocr_evidence_output_too_large")
    canvas = Image.new("RGB", (canvas_width, canvas_height), "white")
    try:
        cursor_x = 0
        for crop_box in crop_boxes:
            crop = source.crop(crop_box)
            try:
                canvas.paste(crop, (cursor_x, 0))
                cursor_x += crop.width
            finally:
                crop.close()
    except Exception:
        canvas.close()
        raise
    return canvas


def render_template_ocr_evidence(
    source_path: str | Path,
    job: dict[str, Any],
    output_path: str | Path,
) -> TemplateOcrEvidenceResult:
    plan, canonical_sha256, layout_sha256 = validate_template_ocr_evidence_job(job)
    source_file = Path(source_path)
    if _sha256_file(source_file) != canonical_sha256:
        _fail("canonical_image_hash_mismatch")
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    try:
        with Image.open(source_file) as source:
            width, height = source.size
            if (
                source.format != "PNG"
                or source.mode != "RGB"
                or width <= 0
                or height <= 0
                or width * height > MAX_DECODE_PIXELS
            ):
                _fail("canonical_image_invalid")
            _validate_plan_source_geometry(
                plan,
                source_width=width,
                source_height=height,
            )
            source.load()
            montage = _render_montage(source, plan)
            try:
                montage.save(target, format="PNG")
                output_width, output_height = montage.size
            finally:
                montage.close()
    except RuntimeError:
        target.unlink(missing_ok=True)
        raise
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        target.unlink(missing_ok=True)
        raise RuntimeError("canonical_image_decode_failed") from exc

    try:
        byte_size = target.stat().st_size
        sha256 = _artifact_sha256(target)
    except OSError as exc:
        target.unlink(missing_ok=True)
        raise RuntimeError("template_ocr_evidence_write_failed") from exc
    if byte_size <= 0:
        target.unlink(missing_ok=True)
        _fail("template_ocr_evidence_write_failed")
    candidate = {
        "status": "succeeded",
        "artifact_manifest": {
            "contract_version": TEMPLATE_OCR_EVIDENCE_CONTRACT_VERSION,
            "template_ocr_evidence": {
                "sha256": sha256,
                "byte_size": byte_size,
                "width": output_width,
                "height": output_height,
                "channels": 3,
                "format": "png",
            },
        },
        "input_identity": {
            "canonical_sha256": canonical_sha256,
            "layout_snapshot_sha256": layout_sha256,
        },
    }
    return TemplateOcrEvidenceResult(
        artifact_path=target,
        candidate=candidate,
    )
