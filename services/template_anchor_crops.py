"""Build validated full-height column crops for rectified paper images.

The column-layout step finds one, two, or three columns. The teacher may adjust the vertical
boundaries before confirmation; every aligned student page then reuses those
same frozen full-height columns.
"""

from __future__ import annotations

import re
from typing import Any


PLAN_VERSION = 2
PLAN_STRATEGY = "full_height_columns"
COORDINATE_SPACE = "template_1000"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_PIXEL_ENDPOINT_VERSION = "rounded_source_pixels_v1"
_COLUMN_METADATA_KEYS = (
    "column_id",
    "column_order",
    "order",
    "ordinal",
    "layout_revision",
    "crop_left",
    "crop_right",
    "crop_left_px",
    "crop_right_px",
    "pixel_crop_left",
    "pixel_crop_right",
    "crop_box_px",
    "actual_pixel_endpoints",
    "crop_endpoint_version",
    "crop_coordinate_space",
    "source_width",
    "source_height",
    "source_dimensions",
    "image_sha256",
    "image_hash",
    "source_sha256",
)


class AnchorCropPlanError(ValueError):
    """Raised when a column crop plan is invalid or could omit page pixels."""


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _bounded_int(value: object, *, name: str, minimum: int, maximum: int) -> int:
    if not _is_int(value) or not minimum <= value <= maximum:
        raise AnchorCropPlanError(
            f"{name} must be an integer in {minimum}..{maximum}"
        )
    return int(value)


def _question_numbers(values: object) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise AnchorCropPlanError("expected_question_numbers must be a list")
    result: list[str] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise AnchorCropPlanError("question numbers must be text or integers")
        number = str(value).strip()
        if not number or len(number) > 64 or any(ord(char) < 32 for char in number):
            raise AnchorCropPlanError("question number is invalid")
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
    """Preserve the optional frozen-layout identity without trusting it blindly."""
    metadata: dict[str, Any] = {}
    if "column_id" in raw:
        value = raw.get("column_id")
        if not isinstance(value, str) or not value.strip():
            raise AnchorCropPlanError("column_id must be non-empty text")
        metadata["column_id"] = value.strip()
    order_values = [
        raw[key] for key in ("column_order", "order", "ordinal") if key in raw
    ]
    if order_values:
        if any(not _is_int(value) or int(value) != index for value in order_values):
            raise AnchorCropPlanError("column order does not match column_index")
        metadata["column_order"] = index

    if "layout_revision" in raw:
        revision = raw.get("layout_revision")
        if not _is_int(revision) or int(revision) < 1:
            raise AnchorCropPlanError("layout_revision must be a positive integer")
        metadata["layout_revision"] = int(revision)

    for key in ("source_width", "source_height"):
        if key in raw:
            value = raw.get(key)
            if not _is_int(value) or int(value) <= 0:
                raise AnchorCropPlanError(f"{key} must be a positive integer")
            metadata[key] = int(value)
    if "source_dimensions" in raw:
        dimensions = raw.get("source_dimensions")
        if (
            not isinstance(dimensions, dict)
            or not _is_int(dimensions.get("width"))
            or not _is_int(dimensions.get("height"))
            or dimensions["width"] <= 0
            or dimensions["height"] <= 0
        ):
            raise AnchorCropPlanError("source_dimensions are invalid")
        metadata["source_dimensions"] = {
            "width": int(dimensions["width"]),
            "height": int(dimensions["height"]),
        }
        if "source_width" in metadata and metadata["source_width"] != dimensions["width"]:
            raise AnchorCropPlanError("source_width disagrees with source_dimensions")
        if "source_height" in metadata and metadata["source_height"] != dimensions["height"]:
            raise AnchorCropPlanError("source_height disagrees with source_dimensions")

    hashes: list[str] = []
    for key in ("image_sha256", "image_hash", "source_sha256"):
        if key not in raw:
            continue
        value = str(raw.get(key) or "").strip().lower()
        if not _SHA256_RE.fullmatch(value):
            raise AnchorCropPlanError(f"{key} is invalid")
        hashes.append(value)
    if hashes and len(set(hashes)) != 1:
        raise AnchorCropPlanError("column image hashes disagree")
    if hashes:
        metadata["image_sha256"] = hashes[0]

    endpoint_version = raw.get("crop_endpoint_version")
    if endpoint_version is not None:
        if str(endpoint_version) != _PIXEL_ENDPOINT_VERSION:
            raise AnchorCropPlanError("unsupported crop endpoint version")
        metadata["crop_endpoint_version"] = _PIXEL_ENDPOINT_VERSION
    endpoint_values: dict[str, int] = {}
    for canonical, aliases in {
        "left": ("crop_left_px", "crop_left", "pixel_crop_left"),
        "right": ("crop_right_px", "crop_right", "pixel_crop_right"),
    }.items():
        seen: list[int] = []
        for key in aliases:
            if key in raw:
                value = raw.get(key)
                if not _is_int(value):
                    raise AnchorCropPlanError(f"{key} must be an integer")
                seen.append(int(value))
        if seen and len(set(seen)) != 1:
            raise AnchorCropPlanError("column pixel endpoint aliases disagree")
        if seen:
            endpoint_values[canonical] = seen[0]
    actual = raw.get("actual_pixel_endpoints")
    if actual is not None:
        if not isinstance(actual, dict):
            raise AnchorCropPlanError("actual_pixel_endpoints are invalid")
        for canonical in ("left", "right"):
            value = actual.get(canonical)
            if not _is_int(value):
                raise AnchorCropPlanError("actual_pixel_endpoints are incomplete")
            value = int(value)
            if canonical in endpoint_values and endpoint_values[canonical] != value:
                raise AnchorCropPlanError("actual pixel endpoints disagree")
            endpoint_values[canonical] = value
    if endpoint_values:
        if (
            set(endpoint_values) != {"left", "right"}
            or endpoint_values["left"] < 0
            or endpoint_values["right"] <= endpoint_values["left"]
        ):
            raise AnchorCropPlanError("actual pixel endpoints are invalid")
        source_width = metadata.get("source_width")
        if source_width is None and isinstance(metadata.get("source_dimensions"), dict):
            source_width = metadata["source_dimensions"]["width"]
        if source_width is not None:
            expected = {
                "left": round(left * source_width / 1000),
                "right": round(right * source_width / 1000),
            }
            if endpoint_values != expected:
                raise AnchorCropPlanError(
                    "actual pixel endpoints disagree with normalized boundaries"
                )
        metadata.update({
            "crop_left_px": endpoint_values["left"],
            "crop_right_px": endpoint_values["right"],
            "actual_pixel_endpoints": dict(endpoint_values),
        })
        crop_box = raw.get("crop_box_px")
        if crop_box is not None:
            if not isinstance(crop_box, (list, tuple)) or len(crop_box) != 4:
                raise AnchorCropPlanError("crop_box_px is invalid")
            values = [_bounded_int(value, name="crop_box_px", minimum=0, maximum=2**31 - 1) for value in crop_box]
            source_height = metadata.get("source_height")
            if source_height is None and isinstance(metadata.get("source_dimensions"), dict):
                source_height = metadata["source_dimensions"]["height"]
            expected_bottom = source_height if source_height is not None else values[3]
            if values != [endpoint_values["left"], 0, endpoint_values["right"], expected_bottom]:
                raise AnchorCropPlanError("crop_box_px disagrees with pixel endpoints")
            metadata["crop_box_px"] = values
        coordinate_space = raw.get("crop_coordinate_space")
        if coordinate_space is not None:
            if str(coordinate_space) != "source_pixels":
                raise AnchorCropPlanError("crop_coordinate_space is invalid")
            metadata["crop_coordinate_space"] = "source_pixels"
    return metadata


def _canonical_columns(
    value: object,
    *,
    column_count: object = None,
) -> list[dict[str, int]]:
    """Validate ordered 1..3 column boundaries covering the whole page."""
    if not isinstance(value, list) or not 1 <= len(value) <= 3:
        raise AnchorCropPlanError("columns must contain between one and three items")
    if column_count is not None:
        count = _bounded_int(
            column_count,
            name="column_count",
            minimum=1,
            maximum=3,
        )
        if count != len(value):
            raise AnchorCropPlanError("column_count does not match columns")

    columns: list[dict[str, Any]] = []
    previous_right = 0
    for position, raw in enumerate(value, 1):
        if not isinstance(raw, dict):
            raise AnchorCropPlanError("column must be an object")
        index = _bounded_int(
            raw.get("column_index"),
            name="column_index",
            minimum=1,
            maximum=3,
        )
        left = _bounded_int(
            raw.get("x1_1000"),
            name="x1_1000",
            minimum=0,
            maximum=999,
        )
        right = _bounded_int(
            raw.get("x2_1000"),
            name="x2_1000",
            minimum=1,
            maximum=1000,
        )
        if index != position or left != previous_right or right <= left:
            raise AnchorCropPlanError(
                "columns must be indexed and cover the page without gaps"
            )
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
        raise AnchorCropPlanError("columns must end at page right")
    # A frozen page must not combine identities from different revisions or
    # source images.  Metadata is optional for legacy plans, but once supplied
    # it is required to agree across every column.
    for key in ("layout_revision", "source_width", "source_height", "image_sha256"):
        values = {column[key] for column in columns if key in column}
        if values and len(values) != 1:
            raise AnchorCropPlanError(f"column {key} values disagree")
    column_ids = [column.get("column_id") for column in columns]
    if any(value is not None for value in column_ids):
        if any(value is None for value in column_ids) or len(set(column_ids)) != len(column_ids):
            raise AnchorCropPlanError("column identities must be complete and unique")
    return columns


def _columns_from_layout(question_result: object) -> list[dict[str, int]]:
    if not isinstance(question_result, dict):
        raise AnchorCropPlanError("question layout result must be an object")
    layout = question_result.get("column_layout")
    if not isinstance(layout, dict):
        raise AnchorCropPlanError(
            "question layout result is missing column_layout"
        )
    return _canonical_columns(
        layout.get("columns"),
        column_count=layout.get("column_count"),
    )


def _layout_context(question_result: object) -> dict[str, Any]:
    """Extract optional layout identity metadata while keeping old plans small."""
    if not isinstance(question_result, dict):
        return {}
    layout = question_result.get("column_layout")
    if not isinstance(layout, dict):
        return {}
    context: dict[str, Any] = {}
    for key in (
        "layout_revision",
        "coordinate_space",
        "crop_endpoint_version",
        "source_width",
        "source_height",
        "image_sha256",
        "image_hash",
        "source_sha256",
        "contract_version",
    ):
        if key in layout:
            context[key] = layout[key]
    dimensions = layout.get("source_dimensions")
    if dimensions is not None:
        if not isinstance(dimensions, dict):
            raise AnchorCropPlanError("layout source_dimensions are invalid")
        context["source_dimensions"] = dict(dimensions)
    source = question_result.get("source")
    if isinstance(source, dict):
        for source_key, context_key in (
            ("width", "source_width"),
            ("height", "source_height"),
            ("sha256", "image_sha256"),
        ):
            if source_key in source and context_key not in context:
                context[context_key] = source[source_key]
    if not context:
        return {}
    # Validate scalar metadata through the same rules used for columns.
    if "layout_revision" in context and (
        not _is_int(context["layout_revision"]) or context["layout_revision"] < 1
    ):
        raise AnchorCropPlanError("layout_revision must be a positive integer")
    for key in ("source_width", "source_height"):
        if key in context and (
            not _is_int(context[key]) or context[key] <= 0
        ):
            raise AnchorCropPlanError(f"{key} must be a positive integer")
    if "crop_endpoint_version" in context and str(context["crop_endpoint_version"]) != _PIXEL_ENDPOINT_VERSION:
        raise AnchorCropPlanError("unsupported crop endpoint version")
    hash_values = [
        str(context[key]).strip().lower()
        for key in ("image_sha256", "image_hash", "source_sha256")
        if key in context
    ]
    if hash_values and any(not _SHA256_RE.fullmatch(value) for value in hash_values):
        raise AnchorCropPlanError("layout image hash is invalid")
    if hash_values and len(set(hash_values)) != 1:
        raise AnchorCropPlanError("layout image hashes disagree")
    return context


def _blocks_for_columns(columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks = []
    for column in columns:
        block = {
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
        for key in _COLUMN_METADATA_KEYS:
            if key in column:
                block[key] = column[key]
        blocks.append(block)
    return blocks


def build_anchor_crop_plan(
    question_result: object,
    *,
    artifact_sha256: str,
    expected_question_numbers: list[object] | None = None,
) -> dict[str, Any]:
    """Create one immutable full-height block per selected column."""
    source_sha256 = str(artifact_sha256 or "").strip().lower()
    if not _SHA256_RE.fullmatch(source_sha256):
        raise AnchorCropPlanError("artifact_sha256 is invalid")
    columns = _columns_from_layout(question_result)
    layout_context = _layout_context(question_result)
    expected = _question_numbers(list(expected_question_numbers or []))
    plan = {
        "version": PLAN_VERSION,
        "strategy": PLAN_STRATEGY,
        "coordinate_space": COORDINATE_SPACE,
        "source_artifact_sha256": source_sha256,
        "layout_context": layout_context,
        "provider_bottom_ignored": True,
        "requires_question_number_gate": True,
        "parameters": {
            "column_count": len(columns),
            "columns": columns,
        },
        "expected_question_numbers": expected,
        "anchors": [],
        "blocks": _blocks_for_columns(columns),
        "warnings": [],
        "source_summary": {
            "trusted_candidate_count": len(columns),
            "rejected_candidate_count": 0,
        },
    }
    return validate_anchor_crop_plan(plan, artifact_sha256=source_sha256)


def validate_anchor_crop_plan(
    raw_plan: object,
    *,
    artifact_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate and canonicalize the current full-height column plan."""
    if not isinstance(raw_plan, dict):
        raise AnchorCropPlanError("question_crop_plan must be an object")
    if (
        raw_plan.get("version") != PLAN_VERSION
        or isinstance(raw_plan.get("version"), bool)
    ):
        raise AnchorCropPlanError("question_crop_plan has unsupported version")
    if raw_plan.get("strategy") != PLAN_STRATEGY:
        raise AnchorCropPlanError("question_crop_plan has unsupported strategy")
    if raw_plan.get("coordinate_space") != COORDINATE_SPACE:
        raise AnchorCropPlanError("question_crop_plan has invalid coordinate space")
    if raw_plan.get("provider_bottom_ignored") is not True:
        raise AnchorCropPlanError("provider bottom must be ignored")
    if raw_plan.get("requires_question_number_gate") is not True:
        raise AnchorCropPlanError("question number gate must remain enabled")

    source_sha256 = str(
        raw_plan.get("source_artifact_sha256") or ""
    ).strip().lower()
    if not _SHA256_RE.fullmatch(source_sha256):
        raise AnchorCropPlanError("question_crop_plan source hash is invalid")
    if (
        artifact_sha256 is not None
        and source_sha256 != str(artifact_sha256).strip().lower()
    ):
        raise AnchorCropPlanError("question_crop_plan source hash mismatch")

    parameters = raw_plan.get("parameters")
    if not isinstance(parameters, dict):
        raise AnchorCropPlanError("question_crop_plan parameters are invalid")
    columns = _canonical_columns(
        parameters.get("columns"),
        column_count=parameters.get("column_count"),
    )
    if raw_plan.get("anchors") != []:
        raise AnchorCropPlanError("full-height column plans cannot contain anchors")

    expected_blocks = _blocks_for_columns(columns)
    raw_blocks = raw_plan.get("blocks")
    if not isinstance(raw_blocks, list) or len(raw_blocks) != len(expected_blocks):
        raise AnchorCropPlanError("crop blocks must match columns")
    blocks: list[dict[str, Any]] = []
    for expected, raw in zip(expected_blocks, raw_blocks):
        if not isinstance(raw, dict):
            raise AnchorCropPlanError("crop block must be an object")
        if (
            str(raw.get("block_id") or "") != expected["block_id"]
            or str(raw.get("column") or "") != expected["column"]
            or raw.get("bbox_1000") != expected["bbox_1000"]
            or raw.get("source_anchor_ids") != []
            or raw.get("fallback_full_column") is not True
        ):
            raise AnchorCropPlanError(
                "crop block does not match its full-height column"
            )
        for key in _COLUMN_METADATA_KEYS:
            if key in expected and raw.get(key) != expected[key]:
                raise AnchorCropPlanError(
                    "crop block metadata does not match its full-height column"
                )
        blocks.append({
            **expected,
            "question_number_hints": _question_numbers(
                raw.get("question_number_hints") or []
            ),
        })

    warnings = raw_plan.get("warnings") or []
    if not isinstance(warnings, list) or any(
        not isinstance(value, str) for value in warnings
    ):
        raise AnchorCropPlanError("question_crop_plan warnings are invalid")
    source_summary = raw_plan.get("source_summary") or {}
    if not isinstance(source_summary, dict):
        raise AnchorCropPlanError("question_crop_plan source summary is invalid")
    trusted_count = _bounded_int(
        source_summary.get("trusted_candidate_count", len(columns)),
        name="trusted_candidate_count",
        minimum=0,
        maximum=10000,
    )
    rejected_count = _bounded_int(
        source_summary.get("rejected_candidate_count", 0),
        name="rejected_candidate_count",
        minimum=0,
        maximum=10000,
    )
    expected_numbers = _question_numbers(
        raw_plan.get("expected_question_numbers") or []
    )
    layout_context = _layout_context({"column_layout": raw_plan.get("layout_context")})
    context_hashes = [
        str(layout_context[key]).strip().lower()
        for key in ("image_sha256", "image_hash", "source_sha256")
        if key in layout_context
    ]
    if context_hashes and context_hashes[0] != source_sha256:
        raise AnchorCropPlanError("layout context source hash mismatch")
    return {
        "version": PLAN_VERSION,
        "strategy": PLAN_STRATEGY,
        "coordinate_space": COORDINATE_SPACE,
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
