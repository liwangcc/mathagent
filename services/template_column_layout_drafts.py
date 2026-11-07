"""Layout drafts: the result a teacher reviews, its revisions, and the fallback while it is unconfirmed.

- ``_build_layout_result`` turns a layout payload into the draft: columns with
  pixel crops and stable ids (``_enrich_layout_columns``), source metadata, a
  summary and the operation name (``_OPERATION``).  ``_review_layout_result``
  builds the fail-closed draft that needs the teacher's confirmation.
- ``revise_layout_draft`` (also ``update_layout_draft``, ``edit_layout_draft``)
  returns an edited draft; a changed mode or boundary bumps the revision
  (``_layout_signature``, ``_next_layout_revision``, ``_coerce_revision``).
- ``layout_needs_review`` tells whether a draft still awaits review;
  ``whole_page_fallback_layout`` is the one whole-page OCR input used meanwhile.

Moved unchanged from services/template_column_layout.py, which still exposes these names.
"""

from __future__ import annotations

import hashlib
from typing import Any

from services.template_column_layout_contract import (
    DEFAULT_COLUMN_MODE,
    LAYOUT_CONTRACT_VERSION,
    LAYOUT_COORDINATE_SPACE,
    PIXEL_ENDPOINT_VERSION,
    TemplateCutError,
    _as_exact_int,
    _integer_json_number,
    _layout_columns_from_value,
    _payload_from_columns,
    normalize_column_mode,
    validate_layout_boundaries,
)


_OPERATION = "RecognizeColumnLayout"


def _coerce_revision(value: object, *, default: int = 1) -> int:
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    parsed = _integer_json_number(value)
    if parsed is None and isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError:
            parsed = None
    if parsed is None or parsed < 1:
        raise TemplateCutError("LAYOUT_REVISION_INVALID", "layout_revision 必须是正整数")
    return int(parsed)


def _layout_signature(layout: object) -> tuple[object, ...] | None:
    if not isinstance(layout, dict):
        return None
    column_layout = layout.get("column_layout")
    if not isinstance(column_layout, dict):
        column_layout = layout
    raw_columns = column_layout.get("columns")
    if not isinstance(raw_columns, list):
        return None
    signature: list[object] = [
        str(
            layout.get("layout_mode")
            or layout.get("column_mode")
            or column_layout.get("layout_mode")
            or column_layout.get("column_mode")
            or ""
        ).strip().lower(),
        str(layout.get("source", {}).get("sha256") or "")
        if isinstance(layout.get("source"), dict)
        else str(layout.get("image_sha256") or ""),
    ]
    for raw in raw_columns:
        if not isinstance(raw, dict):
            return None
        left = _as_exact_int(raw.get("x1_1000"))
        right = _as_exact_int(raw.get("x2_1000"))
        if left is None or right is None:
            return None
        signature.append((left, right))
    return tuple(signature)


def _next_layout_revision(
    *,
    previous_layout: object = None,
    requested_revision: object = None,
    mode: str,
    columns: list[dict[str, int]],
    source_sha256: str,
) -> int:
    """Return a monotonic draft revision without persisting state."""
    requested = _coerce_revision(requested_revision, default=1)
    if not isinstance(previous_layout, dict):
        return requested
    previous_revision = _coerce_revision(previous_layout.get("layout_revision"), default=1)
    current_signature = (mode, source_sha256, *(
        (item["x1_1000"], item["x2_1000"]) for item in columns
    ))
    previous_signature = _layout_signature(previous_layout)
    if previous_signature is None:
        return max(requested, previous_revision + 1)
    if previous_signature == current_signature:
        return max(requested, previous_revision)
    return max(requested, previous_revision + 1)


def _enrich_layout_columns(
    source: dict[str, Any],
    columns: list[dict[str, int]],
    *,
    layout_revision: int,
    image_sha256: str,
) -> list[dict[str, Any]]:
    width = int(source["width"])
    height = int(source["height"])
    enriched: list[dict[str, Any]] = []
    for position, raw in enumerate(columns, 1):
        left = int(raw["x1_1000"])
        right = int(raw["x2_1000"])
        crop_left = round(left * width / 1000)
        crop_right = round(right * width / 1000)
        identity_material = f"{image_sha256}:{layout_revision}:{position}:{left}:{right}"
        column_id = "col_{}-{}".format(
            position,
            hashlib.sha256(identity_material.encode("utf-8")).hexdigest()[:16],
        )
        enriched.append({
            "column_index": position,
            "column_order": position,
            "order": position,
            "ordinal": position,
            "column_id": column_id,
            "x1_1000": left,
            "x2_1000": right,
            "crop_left": crop_left,
            "crop_right": crop_right,
            "crop_left_px": crop_left,
            "crop_right_px": crop_right,
            "pixel_crop_left": crop_left,
            "pixel_crop_right": crop_right,
            "crop_box_px": [crop_left, 0, crop_right, height],
            "crop_coordinate_space": "source_pixels",
            "crop_endpoint_version": PIXEL_ENDPOINT_VERSION,
            "actual_pixel_endpoints": {
                "left": crop_left,
                "right": crop_right,
            },
            "source_width": width,
            "source_height": height,
            "source_dimensions": {"width": width, "height": height},
            "image_sha256": image_sha256,
            "image_hash": image_sha256,
            "layout_revision": layout_revision,
            "coordinate_space": LAYOUT_COORDINATE_SPACE,
        })
    return enriched


def _build_layout_result(
    source: dict[str, Any],
    payload: dict[str, Any],
    *,
    provider: str,
    request_id: str = "",
    diagnostics: dict[str, Any] | None = None,
    column_mode: str | None = None,
    layout_revision: object = None,
    previous_layout: object = None,
    layout_status: str = "ready",
) -> dict[str, Any]:
    raw_layout = payload.get("column_layout") if isinstance(payload, dict) else None
    if not isinstance(raw_layout, dict):
        raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "布局缺少 column_layout")
    raw_columns = raw_layout.get("columns")
    if (
        layout_status == "needs_review"
        and isinstance(raw_columns, list)
        and not raw_columns
    ):
        columns: list[dict[str, int]] = []
    else:
        columns = _layout_columns_from_value(raw_columns)
    mode = normalize_column_mode(
        column_mode,
        default=("single" if len(columns) == 1 else "triple" if len(columns) == 3 else DEFAULT_COLUMN_MODE),
    )
    if columns and mode == "single" and len(columns) != 1:
        raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "单栏边界数量无效")
    if columns and mode == "double" and len(columns) != 2:
        raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "双栏边界数量无效")
    if columns and mode == "triple" and len(columns) != 3:
        raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "三栏边界数量无效")
    revision = _next_layout_revision(
        previous_layout=previous_layout,
        requested_revision=layout_revision,
        mode=mode,
        columns=columns,
        source_sha256=str(source["sha256"]),
    )
    enriched_columns = _enrich_layout_columns(
        source,
        columns,
        layout_revision=revision,
        image_sha256=str(source["sha256"]),
    )
    candidates: list[dict[str, Any]] = []
    for candidate_index, candidate in enumerate(payload.get("candidates") or []):
        if not isinstance(candidate, dict):
            continue
        item = dict(candidate)
        if candidate_index < len(enriched_columns):
            item.update(enriched_columns[candidate_index])
            item["template_bbox_1000"] = [
                enriched_columns[candidate_index]["x1_1000"],
                0,
                enriched_columns[candidate_index]["x2_1000"],
                1000,
            ]
        item["candidate_index"] = candidate_index
        item["layout_revision"] = revision
        item["image_sha256"] = str(source["sha256"])
        candidates.append(item)
    # A review draft may intentionally omit candidates so downstream OCR cannot
    # mistake an untrusted boundary for an approved crop.
    if layout_status == "needs_review" and not candidates:
        candidates = []
    normalized_layout = {
        "column_count": len(enriched_columns),
        "columns": enriched_columns,
        "boundaries_1000": [item["x2_1000"] for item in enriched_columns[:-1]],
        "layout_mode": mode,
        "column_mode": mode,
        "layout_revision": revision,
        "coordinate_space": LAYOUT_COORDINATE_SPACE,
        "crop_endpoint_version": PIXEL_ENDPOINT_VERSION,
        "source_width": int(source["width"]),
        "source_height": int(source["height"]),
        "image_sha256": str(source["sha256"]),
        "contract_version": LAYOUT_CONTRACT_VERSION,
    }
    result = {
        "layout_contract_version": LAYOUT_CONTRACT_VERSION,
        "coordinate_space": LAYOUT_COORDINATE_SPACE,
        "provider": provider,
        "operation": _OPERATION,
        "cut_type": "question",
        "image_type": "template",
        "subject": "Math",
        "output_oricoord": True,
        "request_id": request_id,
        "layout_mode": mode,
        "column_mode": mode,
        "layout_revision": revision,
        "layout_status": layout_status,
        "status": layout_status,
        "needs_review": layout_status == "needs_review",
        "usable_for_ocr": layout_status != "needs_review",
        "draft": True,
        "source": {
            "sha256": source["sha256"],
            "image_sha256": source["sha256"],
            "image_hash": source["sha256"],
            "width": source["width"],
            "height": source["height"],
            "size_bytes": source["size_bytes"],
            "image_format": source["image_format"],
        },
        "provider_dimensions": {
            "org_width": source["width"],
            "org_height": source["height"],
            "rectified_width": source["width"],
            "rectified_height": source["height"],
        },
        "pages": [{
            "page_index": 0,
            "provider_page_no": 1,
        "layout_revision": revision,
        "source_width": int(source["width"]),
        "source_height": int(source["height"]),
        "source_dimensions": {
            "width": int(source["width"]),
            "height": int(source["height"]),
        },
        "image_sha256": str(source["sha256"]),
        "image_hash": str(source["sha256"]),
            "provider_dimensions": {
                "org_width": source["width"],
                "org_height": source["height"],
                "rectified_width": source["width"],
                "rectified_height": source["height"],
            },
            "candidates": candidates,
        }],
        "summary": {
            "total": len(candidates),
            "original": len(candidates),
            "rectified_untrusted": 0,
            "invalid": 0,
            "missing": 0,
        },
        "column_layout": normalized_layout,
    }
    if diagnostics is not None:
        result["layout_diagnostics"] = {
            "contract_version": LAYOUT_CONTRACT_VERSION,
            "coordinate_space": LAYOUT_COORDINATE_SPACE,
            "layout_revision": revision,
            **diagnostics,
        }
    return result


def _review_layout_result(
    source: dict[str, Any],
    *,
    mode: str,
    diagnostics: dict[str, Any] | None = None,
    layout_revision: object = None,
    previous_layout: object = None,
    proposed_columns: object = None,
    provider: str = "local_projection",
) -> dict[str, Any]:
    """Build a fail-closed draft for the confirmation page."""
    # A proposed boundary may be shown to a teacher, but it is never marked
    # usable until the teacher confirms it.  Invalid/unknown proposals are
    # represented by an empty column list rather than a nominal equal split.
    columns: list[dict[str, int]] = []
    if proposed_columns is not None:
        try:
            columns = _layout_columns_from_value(proposed_columns)
            if (mode == "double" and len(columns) != 2) or (
                mode == "triple" and len(columns) != 3
            ) or (mode == "single" and len(columns) != 1):
                columns = []
        except TemplateCutError:
            columns = []
    if columns:
        payload = _payload_from_columns(
            columns,
            source_key="layout_review_candidate",
            trust="local_review",
        )
        # Keep review candidates visible but explicitly unusable.
        for candidate in payload["candidates"]:
            candidate["usable_for_original_overlay"] = False
    else:
        payload = {
            "column_layout": {
                "column_count": {"single": 1, "double": 2, "triple": 3}[mode],
                "columns": [],
            },
            "candidates": [],
        }
    if not columns:
        result = _build_layout_result(
            source,
            payload,
            provider=provider,
            diagnostics=diagnostics,
            column_mode=mode,
            layout_revision=layout_revision,
            previous_layout=previous_layout,
            layout_status="needs_review",
        )
        return result
    return _build_layout_result(
        source,
        payload,
        provider=provider,
        diagnostics=diagnostics,
        column_mode=mode,
        layout_revision=layout_revision,
        previous_layout=previous_layout,
        layout_status="needs_review",
    )


def revise_layout_draft(
    layout: dict[str, Any],
    *,
    column_mode: object = None,
    layout_mode: object = None,
    column_count: object = None,
    mode: object = None,
    requested_mode: object = None,
    columns: object = None,
    boundaries: object = None,
    evidence: object = None,
) -> dict[str, Any]:
    """Return an edited layout draft without persisting a final contract.

    The source image metadata is copied from the prior draft.  A changed mode
    or boundary increments ``layout_revision``; unchanged edits retain it so
    cached downstream inputs remain identifiable and reusable.
    """
    if not isinstance(layout, dict) or not isinstance(layout.get("source"), dict):
        raise TemplateCutError("LAYOUT_DRAFT_INVALID", "布局草稿缺少源图信息")
    supplied = [
        item
        for item in (column_mode, layout_mode, column_count, mode, requested_mode)
        if item is not None
    ]
    if len(supplied) > 1:
        normalized_values = [normalize_column_mode(item) for item in supplied]
        if len(set(normalized_values)) != 1:
            raise TemplateCutError("LAYOUT_MODE_INVALID", "分栏模式参数冲突")
        mode = normalized_values[0]
    elif supplied:
        mode = normalize_column_mode(supplied[0])
    else:
        mode = normalize_column_mode(
            layout.get("layout_mode") or layout.get("column_mode")
        )
    boundary_value = boundaries if boundaries is not None else columns
    if boundary_value is None:
        old_layout = layout.get("column_layout")
        boundary_value = old_layout.get("columns") if isinstance(old_layout, dict) else None
    validated = validate_layout_boundaries(
        boundary_value,
        column_mode=mode,
        evidence=evidence,
    )
    source = layout["source"]
    source_info = {
        "sha256": str(source.get("sha256") or source.get("image_sha256") or ""),
        "width": int(source.get("width") or source.get("source_width") or 0),
        "height": int(source.get("height") or source.get("source_height") or 0),
        "size_bytes": int(source.get("size_bytes") or 0),
        "image_format": str(source.get("image_format") or ""),
    }
    if not source_info["sha256"] or source_info["width"] <= 0 or source_info["height"] <= 0:
        raise TemplateCutError("LAYOUT_DRAFT_INVALID", "布局草稿源图信息无效")
    payload = _payload_from_columns(
        validated,
        source_key="teacher_layout_draft",
        trust="teacher_confirmed_draft",
    )
    # A source-only stub (a legacy page that never had a layout draft) is not
    # a prior revision: the teacher's first boundary set starts at revision 1
    # instead of pretending an empty draft was edited.
    has_prior_columns = isinstance(layout.get("column_layout"), dict)
    return _build_layout_result(
        source_info,
        payload,
        provider=str(layout.get("provider") or "layout_draft"),
        request_id=str(layout.get("request_id") or ""),
        diagnostics={
            "strategy": "teacher_draft_edit",
            "validation": "passed",
            "confidence": 1.0,
            "confidence_level": "teacher_confirmed_draft",
        },
        column_mode=mode,
        layout_revision=layout.get("layout_revision"),
        previous_layout=layout if has_prior_columns else None,
        layout_status="ready",
    )


def layout_needs_review(layout: object) -> bool:
    """Whether a layout draft is still a teacher-review candidate.

    Legacy plans without a status are treated as usable; only an explicit
    ``needs_review`` status or ``usable_for_ocr=False`` blocks downstream use.
    """
    if not isinstance(layout, dict):
        return False
    if layout.get("usable_for_ocr") is False:
        return True
    return str(layout.get("layout_status") or layout.get("status") or "").strip().lower() == "needs_review"


def whole_page_fallback_layout(layout: dict[str, Any]) -> dict[str, Any]:
    """Return the one whole-page OCR input used while a column draft needs review.

    An unusable or low-confidence column proposal stays the teacher-facing
    draft.  The model routes for that page fall back to a single whole-page
    request instead of unverified column crops; this layout only organises
    that request and is labelled so it cannot be mistaken for an approved
    column result.
    """
    if not isinstance(layout, dict) or not isinstance(layout.get("source"), dict):
        raise TemplateCutError("LAYOUT_DRAFT_INVALID", "布局草稿缺少源图信息")
    source = layout["source"]
    source_info = {
        "sha256": str(source.get("sha256") or source.get("image_sha256") or ""),
        "width": int(source.get("width") or source.get("source_width") or 0),
        "height": int(source.get("height") or source.get("source_height") or 0),
        "size_bytes": int(source.get("size_bytes") or 0),
        "image_format": str(source.get("image_format") or ""),
    }
    if not source_info["sha256"] or source_info["width"] <= 0 or source_info["height"] <= 0:
        raise TemplateCutError("LAYOUT_DRAFT_INVALID", "布局草稿源图信息无效")
    payload = _payload_from_columns(
        [{"column_index": 1, "x1_1000": 0, "x2_1000": 1000}],
        source_key="whole_page_fallback",
        trust="whole_page_fallback",
    )
    review_diagnostics = layout.get("layout_diagnostics")
    result = _build_layout_result(
        source_info,
        payload,
        provider="local_whole_page_fallback",
        diagnostics={
            "strategy": "whole_page_fallback",
            "validation": "passed",
            "confidence": 1.0,
            "confidence_level": "fallback",
            "review_mode": str(layout.get("layout_mode") or layout.get("column_mode") or ""),
            "review_reasons": list(
                (review_diagnostics or {}).get("reasons") or []
            ) if isinstance(review_diagnostics, dict) else [],
        },
        column_mode="single",
        layout_revision=layout.get("layout_revision"),
        previous_layout=None,
        layout_status="ready",
    )
    result["fallback_for_review"] = True
    return result


# Descriptive aliases used by confirmation-page integrations.
update_layout_draft = revise_layout_draft
edit_layout_draft = revise_layout_draft
