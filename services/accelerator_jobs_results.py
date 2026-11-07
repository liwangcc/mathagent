"""What a node's submitted result must look like, per stage, before the server takes it as a candidate.

- ``_validate_candidate_payload`` dispatches by stage to the canonicalization,
  template OCR evidence and vision model validators
  (``_validate_canonical_candidate_payload``,
  ``_validate_template_ocr_evidence_candidate_payload``,
  ``_validate_vision_model_result_payload``) and checks the other stages'
  results against the allowed and required keys and size limits
  (``_ALLOWED_RESULT_KEYS``, ``_REQUIRED_RESULT_KEYS``, ``_MAX_RESULT_*``).
- ``_normalize_evidence_diagnostics`` and ``_normalize_result_sequence`` bound
  and normalize evidence diagnostics and result lists
  (``_EVIDENCE_DIAGNOSTIC_KEYS``, ``_COVERAGE_ITEM_KEYS``,
  ``_MAX_EVIDENCE_DIAGNOSTICS_BYTES``).

Moved unchanged from services/accelerator_jobs.py, which still exposes these names.
"""

from __future__ import annotations

import math
from typing import Any

from services.accelerator_artifacts import (
    CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION,
    CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION,
    TEMPLATE_OCR_EVIDENCE_CONTRACT_VERSION,
    normalize_homography,
    validate_artifact_manifest,
    validate_slot_geometry_receipt,
)
from services.accelerator_jobs_base import (
    STUDENT_MULTI_EVIDENCE_STAGE,
    TEMPLATE_OCR_EVIDENCE_STAGE,
    VISION_MODEL_RESPONSE_CONTRACT_VERSION,
    VISION_MODEL_STAGE,
    canonical_json,
)
from services.accelerator_jobs_identity import _is_sha256
from services.accelerator_jobs_nodes import _STAGE_ARTIFACT_CONTRACTS
from services.template_canonicalization_contract import (
    CANONICALIZATION_STAGE,
    CANONICAL_TEMPLATE_CONTRACT_VERSION,
    validate_canonicalization_metadata,
)


_ALLOWED_RESULT_KEYS = {
    "status", "page_type_id", "homography", "good_matches", "inliers",
    "inlier_ratio", "reprojection_rmse", "coverage", "answer_source_complete",
    "incomplete_answer_regions", "artifact_sha256", "artifact_manifest",
    "worker_metrics", "question_numbers", "blank_question_numbers",
    "evidence_diagnostics", "local_quality_report", "slot_geometry_receipt",
}
_REQUIRED_RESULT_KEYS = {
    "status", "page_type_id", "homography", "good_matches", "inliers",
    "inlier_ratio", "reprojection_rmse", "coverage", "artifact_sha256",
    "artifact_manifest",
}
_CLIENT_EVIDENCE_RESULT_KEYS = {
    "question_numbers",
    "blank_question_numbers",
    "evidence_diagnostics",
}
_CANONICAL_RESULT_KEYS = {
    "status",
    "artifact_manifest",
    "canonicalization",
}
_TEMPLATE_OCR_EVIDENCE_RESULT_KEYS = {
    "status",
    "artifact_manifest",
    "input_identity",
}
_TEMPLATE_OCR_EVIDENCE_INPUT_IDENTITY_KEYS = {
    "canonical_sha256",
    "layout_snapshot_sha256",
}
_EVIDENCE_DIAGNOSTIC_KEYS = {
    "ownership",
    "answer_source_coverage",
    "incomplete_answer_regions",
}
_OPTIONAL_EVIDENCE_DIAGNOSTIC_KEYS = {"evidence_sheets"}
_COVERAGE_ITEM_KEYS = {
    "question_number",
    "covered_pixels",
    "total_pixels",
    "coverage",
    "complete",
    "crop_box",
}
_MAX_RESULT_QUESTION_COUNT = 200
_MAX_RESULT_QUESTION_LENGTH = 64
_MAX_EVIDENCE_DIAGNOSTICS_BYTES = 16 * 1024
_MAX_MODEL_OUTPUT_BYTES = 512 * 1024


def _normalize_result_sequence(
    raw: Any,
    field: str,
    *,
    allow_empty: bool,
) -> list[str]:
    if not isinstance(raw, list) or len(raw) > _MAX_RESULT_QUESTION_COUNT:
        raise ValueError(f"result {field} is invalid")
    values: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError(f"result {field} is invalid")
        value = item.strip()
        if (
            not value
            or len(value) > _MAX_RESULT_QUESTION_LENGTH
            or value in values
        ):
            raise ValueError(f"result {field} is invalid")
        values.append(value)
    if not allow_empty and not values:
        raise ValueError(f"result {field} is invalid")
    return values


def _normalize_evidence_diagnostics(
    raw: Any,
    *,
    question_numbers: list[str],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("result evidence_diagnostics is invalid")
    if manifest.get("contract_version") == CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION:
        if set(raw) != {"ownership", "evidence_sheets"}:
            raise ValueError("result evidence_diagnostics is invalid")
        ownership = raw.get("ownership")
        raw_sheets = raw.get("evidence_sheets")
        if ownership != "full_column_difference" or not isinstance(raw_sheets, list):
            raise ValueError("result evidence_diagnostics is invalid")
        expected_sheets = []
        flattened: list[str] = []
        for image in manifest.get("evidence_images") or []:
            expected_sheets.append({
                "sheet_id": image["sheet_id"],
                "question_numbers": list(image["question_numbers"]),
            })
            flattened.extend(image["question_numbers"])
        if raw_sheets != expected_sheets or flattened != question_numbers:
            raise ValueError("result evidence_diagnostics is invalid")
        return {
            "ownership": "full_column_difference",
            "evidence_sheets": expected_sheets,
            # Kept as a status list for the generic scheduler contract; it is
            # never a geometry list and is always empty for a complete column.
            "incomplete_answer_regions": [],
        }
    raw_keys = set(raw)
    if (
        not _EVIDENCE_DIAGNOSTIC_KEYS.issubset(raw_keys)
        or raw_keys
        - (_EVIDENCE_DIAGNOSTIC_KEYS | _OPTIONAL_EVIDENCE_DIAGNOSTIC_KEYS)
    ):
        raise ValueError("result evidence_diagnostics is invalid")
    ownership = raw.get("ownership")
    if not isinstance(ownership, str):
        raise ValueError("result evidence_diagnostics is invalid")
    ownership = ownership.strip()
    if not ownership or len(ownership) > 128:
        raise ValueError("result evidence_diagnostics is invalid")

    raw_coverage = raw.get("answer_source_coverage")
    if (
        not isinstance(raw_coverage, list)
        or len(raw_coverage) != len(question_numbers)
        or len(raw_coverage) > _MAX_RESULT_QUESTION_COUNT
    ):
        raise ValueError("result evidence_diagnostics is invalid")
    template_width = int(manifest["template_width"])
    template_height = int(manifest["template_height"])
    normalized_coverage: list[dict[str, Any]] = []
    derived_incomplete: list[str] = []
    for index, item in enumerate(raw_coverage):
        if not isinstance(item, dict) or set(item) != _COVERAGE_ITEM_KEYS:
            raise ValueError("result evidence_diagnostics is invalid")
        question_number = item.get("question_number")
        if (
            not isinstance(question_number, str)
            or question_number.strip() != question_numbers[index]
        ):
            raise ValueError("result evidence_diagnostics is invalid")
        covered_pixels = item.get("covered_pixels")
        total_pixels = item.get("total_pixels")
        if (
            isinstance(covered_pixels, bool)
            or not isinstance(covered_pixels, int)
            or isinstance(total_pixels, bool)
            or not isinstance(total_pixels, int)
            or total_pixels <= 0
            or covered_pixels < 0
            or covered_pixels > total_pixels
        ):
            raise ValueError("result evidence_diagnostics is invalid")
        coverage = item.get("coverage")
        if (
            isinstance(coverage, bool)
            or not isinstance(coverage, (int, float))
            or not math.isfinite(float(coverage))
            or not 0.0 <= float(coverage) <= 1.0
            or abs(
                float(coverage) - round(covered_pixels / total_pixels, 6)
            ) > 1e-6
        ):
            raise ValueError("result evidence_diagnostics is invalid")
        complete = item.get("complete")
        if not isinstance(complete, bool) or complete != (
            covered_pixels == total_pixels
        ):
            raise ValueError("result evidence_diagnostics is invalid")
        crop_box = item.get("crop_box")
        if (
            not isinstance(crop_box, list)
            or len(crop_box) != 4
            or any(isinstance(value, bool) or not isinstance(value, int) for value in crop_box)
        ):
            raise ValueError("result evidence_diagnostics is invalid")
        left, top, right, bottom = crop_box
        if not (
            0 <= left < right <= template_width
            and 0 <= top < bottom <= template_height
        ):
            raise ValueError("result evidence_diagnostics is invalid")
        if not complete:
            derived_incomplete.append(question_numbers[index])
        normalized_coverage.append({
            "question_number": question_numbers[index],
            "covered_pixels": covered_pixels,
            "total_pixels": total_pixels,
            "coverage": float(coverage),
            "complete": complete,
            "crop_box": list(crop_box),
        })

    incomplete = _normalize_result_sequence(
        raw.get("incomplete_answer_regions"),
        "evidence_diagnostics.incomplete_answer_regions",
        allow_empty=True,
    )
    if incomplete != derived_incomplete:
        raise ValueError("result evidence_diagnostics is invalid")

    raw_sheets = raw.get("evidence_sheets")
    is_multi_evidence = (
        manifest.get("contract_version") == CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION
    )
    expected_sheets: list[dict[str, Any]] = []
    if is_multi_evidence:
        expected_sheets = [
            {
                "sheet_id": image["sheet_id"],
                "regions": [dict(region) for region in image["regions"]],
            }
            for image in manifest["evidence_images"]
        ]
        if raw_sheets != expected_sheets:
            raise ValueError("result evidence_diagnostics is invalid")
    elif raw_sheets not in (None, []):
        raise ValueError("result evidence_diagnostics is invalid")

    normalized = {
        "ownership": ownership,
        "answer_source_coverage": normalized_coverage,
        "incomplete_answer_regions": incomplete,
    }
    if is_multi_evidence:
        normalized["evidence_sheets"] = expected_sheets
    elif "evidence_sheets" in raw:
        normalized["evidence_sheets"] = []
    if len(canonical_json(normalized).encode("utf-8")) > _MAX_EVIDENCE_DIAGNOSTICS_BYTES:
        raise ValueError("result evidence_diagnostics is too large")
    return normalized


def _validate_canonical_candidate_payload(
    result: dict[str, Any],
    *,
    source_sha256: str,
) -> dict[str, Any]:
    if not isinstance(result, dict) or set(result) != _CANONICAL_RESULT_KEYS:
        raise ValueError("canonicalization result has invalid fields")
    if result.get("status") != "succeeded":
        raise ValueError("worker result did not succeed")
    manifest = validate_artifact_manifest(result.get("artifact_manifest"))
    if manifest.get("contract_version") != CANONICAL_TEMPLATE_CONTRACT_VERSION:
        raise ValueError("canonical template artifact contract is required")
    canonicalization = validate_canonicalization_metadata(
        result.get("canonicalization"),
        source_sha256=source_sha256,
        canonical_manifest=manifest["canonical_template"],
    )
    payload = {
        "status": "succeeded",
        "artifact_manifest": manifest,
        "canonicalization": canonicalization,
    }
    if len(canonical_json(payload).encode("utf-8")) > 64 * 1024:
        raise ValueError("result payload is too large")
    return payload


def _validate_template_ocr_evidence_candidate_payload(
    result: dict[str, Any],
    *,
    source_sha256: str,
    layout_snapshot_sha256: str,
) -> dict[str, Any]:
    if (
        not isinstance(result, dict)
        or set(result) != _TEMPLATE_OCR_EVIDENCE_RESULT_KEYS
    ):
        raise ValueError("template OCR evidence result has invalid fields")
    if result.get("status") != "succeeded":
        raise ValueError("worker result did not succeed")
    manifest = validate_artifact_manifest(result.get("artifact_manifest"))
    if manifest.get("contract_version") != TEMPLATE_OCR_EVIDENCE_CONTRACT_VERSION:
        raise ValueError("template OCR evidence artifact contract is required")
    identity = result.get("input_identity")
    if (
        not isinstance(identity, dict)
        or set(identity) != _TEMPLATE_OCR_EVIDENCE_INPUT_IDENTITY_KEYS
        or identity.get("canonical_sha256") != source_sha256
        or identity.get("layout_snapshot_sha256") != layout_snapshot_sha256
    ):
        raise ValueError("template OCR evidence input identity does not match")
    payload = {
        "status": "succeeded",
        "artifact_manifest": manifest,
        "input_identity": {
            "canonical_sha256": source_sha256,
            "layout_snapshot_sha256": layout_snapshot_sha256,
        },
    }
    if len(canonical_json(payload).encode("utf-8")) > 64 * 1024:
        raise ValueError("result payload is too large")
    return payload


def _validate_vision_model_result_payload(
    result: dict[str, Any],
    *,
    layout_snapshot: dict[str, Any],
) -> dict[str, Any]:
    allowed = {
        "contract_version",
        "request_digest",
        "status",
        "output_text",
        "usage",
        "provider_request_id",
    }
    if not isinstance(result, dict) or not set(result).issubset(allowed):
        raise ValueError("vision model result has invalid fields")
    if (
        result.get("contract_version") != VISION_MODEL_RESPONSE_CONTRACT_VERSION
        or result.get("status") != "completed"
    ):
        raise ValueError("vision model result contract is invalid")
    request_digest = result.get("request_digest")
    if (
        not _is_sha256(request_digest)
        or request_digest != layout_snapshot.get("request_digest")
    ):
        raise ValueError("vision model result request digest does not match")
    output_text = result.get("output_text")
    if (
        not isinstance(output_text, str)
        or not output_text.strip()
        or len(output_text.encode("utf-8")) > _MAX_MODEL_OUTPUT_BYTES
    ):
        raise ValueError("vision model output is invalid")
    normalized: dict[str, Any] = {
        "contract_version": VISION_MODEL_RESPONSE_CONTRACT_VERSION,
        "request_digest": request_digest,
        "status": "completed",
        "output_text": output_text,
    }
    provider_request_id = result.get("provider_request_id")
    if provider_request_id is not None:
        if (
            not isinstance(provider_request_id, str)
            or not 1 <= len(provider_request_id) <= 128
            or any(not character.isascii() or character.isspace() for character in provider_request_id)
        ):
            raise ValueError("vision model provider request id is invalid")
        normalized["provider_request_id"] = provider_request_id
    usage = result.get("usage")
    if usage is not None:
        if not isinstance(usage, dict) or not set(usage).issubset({
            "input_tokens", "output_tokens", "total_tokens",
        }):
            raise ValueError("vision model usage is invalid")
        normalized_usage: dict[str, int] = {}
        for key, value in usage.items():
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10_000_000:
                raise ValueError("vision model usage is invalid")
            normalized_usage[key] = value
        if (
            "input_tokens" in normalized_usage
            and "output_tokens" in normalized_usage
            and "total_tokens" in normalized_usage
            and normalized_usage["total_tokens"]
            != normalized_usage["input_tokens"] + normalized_usage["output_tokens"]
        ):
            raise ValueError("vision model usage is invalid")
        normalized["usage"] = normalized_usage
    if len(canonical_json(normalized).encode("utf-8")) > _MAX_MODEL_OUTPUT_BYTES:
        raise ValueError("vision model result is too large")
    return normalized


def _validate_candidate_payload(
    result: dict[str, Any],
    *,
    stage: str = "template_registration",
    source_sha256: str = "",
    layout_snapshot_sha256: str = "",
    layout_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if stage == CANONICALIZATION_STAGE:
        return _validate_canonical_candidate_payload(
            result,
            source_sha256=source_sha256,
        )
    if stage == TEMPLATE_OCR_EVIDENCE_STAGE:
        return _validate_template_ocr_evidence_candidate_payload(
            result,
            source_sha256=source_sha256,
            layout_snapshot_sha256=layout_snapshot_sha256,
        )
    if stage == VISION_MODEL_STAGE:
        if not isinstance(layout_snapshot, dict):
            raise ValueError("vision model request metadata is required")
        return _validate_vision_model_result_payload(
            result,
            layout_snapshot=layout_snapshot,
        )
    if not isinstance(result, dict):
        raise ValueError("result must be an object")
    if stage not in {"template_registration", STUDENT_MULTI_EVIDENCE_STAGE}:
        raise ValueError("result stage is unsupported")
    validate_slot_geometry_receipt(
        result, stage=stage, layout=layout_snapshot or {},
        layout_snapshot_sha256=layout_snapshot_sha256,
    )
    raw_manifest = result.get("artifact_manifest")
    required_contract = _STAGE_ARTIFACT_CONTRACTS[stage]
    accepted_contracts = {required_contract}
    if stage == STUDENT_MULTI_EVIDENCE_STAGE:
        accepted_contracts.add(CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION)
    if (
        not isinstance(raw_manifest, dict)
        or raw_manifest.get("contract_version")
        not in accepted_contracts
    ):
        raise ValueError(f"template registration requires {required_contract}")
    unexpected = set(result) - _ALLOWED_RESULT_KEYS
    if unexpected:
        raise ValueError("result contains unsupported fields")
    missing = _REQUIRED_RESULT_KEYS - set(result)
    if missing:
        raise ValueError("result is missing required fields")
    if result.get("status") != "succeeded":
        raise ValueError("worker result did not succeed")
    try:
        matrix = normalize_homography(result.get("homography"))
    except (TypeError, ValueError) as exc:
        if "finite" in str(exc):
            raise ValueError("result contains non-finite values") from exc
        raise ValueError("homography is invalid") from exc
    try:
        good_matches = int(result["good_matches"])
        inliers = int(result["inliers"])
        inlier_ratio = float(result["inlier_ratio"])
        rmse = float(result["reprojection_rmse"])
        coverage = float(result["coverage"])
    except (TypeError, ValueError) as exc:
        raise ValueError("result metrics are invalid") from exc
    if not all(math.isfinite(value) for value in [inlier_ratio, rmse, coverage]):
        raise ValueError("result contains non-finite values")
    if good_matches < 0 or inliers < 0 or inliers > good_matches:
        raise ValueError("match counts are invalid")
    if not 0.0 <= inlier_ratio <= 1.0 or not 0.0 <= coverage <= 1.0 or rmse < 0.0:
        raise ValueError("result metrics are outside valid ranges")
    artifact_sha = str(result.get("artifact_sha256") or "")
    if len(artifact_sha) != 64 or any(char not in "0123456789abcdef" for char in artifact_sha):
        raise ValueError("artifact_sha256 is invalid")
    payload = dict(result)
    payload["homography"] = [
        [float(value) for value in row]
        for row in matrix.tolist()
    ]
    payload["good_matches"] = good_matches
    payload["inliers"] = inliers
    payload["inlier_ratio"] = inlier_ratio
    payload["reprojection_rmse"] = rmse
    payload["coverage"] = coverage
    payload["artifact_manifest"] = validate_artifact_manifest(raw_manifest)
    client_fields = set(result) & _CLIENT_EVIDENCE_RESULT_KEYS
    if client_fields != _CLIENT_EVIDENCE_RESULT_KEYS:
        raise ValueError("client evidence result fields are incomplete")
    question_numbers = _normalize_result_sequence(
        result.get("question_numbers"),
        "question_numbers",
        allow_empty=False,
    )
    blank_question_numbers = _normalize_result_sequence(
        result.get("blank_question_numbers"),
        "blank_question_numbers",
        allow_empty=True,
    )
    if not set(blank_question_numbers).issubset(set(question_numbers)):
        raise ValueError("result blank_question_numbers is invalid")
    diagnostics = _normalize_evidence_diagnostics(
        result.get("evidence_diagnostics"),
        question_numbers=question_numbers,
        manifest=payload["artifact_manifest"],
    )
    incomplete_answer_regions = _normalize_result_sequence(
        result.get("incomplete_answer_regions"),
        "incomplete_answer_regions",
        allow_empty=True,
    )
    if (
        incomplete_answer_regions
        != diagnostics["incomplete_answer_regions"]
        or result.get("answer_source_complete")
        is not (not incomplete_answer_regions)
    ):
        raise ValueError("result answer source completeness is invalid")
    payload["answer_source_complete"] = not incomplete_answer_regions
    payload["incomplete_answer_regions"] = incomplete_answer_regions
    payload["question_numbers"] = question_numbers
    payload["blank_question_numbers"] = blank_question_numbers
    payload["evidence_diagnostics"] = diagnostics
    if "local_quality_report" in result and (
        stage != STUDENT_MULTI_EVIDENCE_STAGE
        or raw_manifest.get("contract_version")
        != CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION
    ):
        raise ValueError("local quality report requires full-column evidence")
    # Preserve the optional report verbatim in the idempotent candidate. Its
    # complete schema, quality and bound-device identity are checked together
    # by the server validator; an invalid report must never select replay.
    if len(canonical_json(payload).encode("utf-8")) > 64 * 1024:
        raise ValueError("result payload is too large")
    return payload
