"""Template artifacts: one immutable-by-contract image per template page, its difference baseline, and their verification.

- ``build_template_artifact`` creates one normalized template image artifact
  (``ARTIFACT_VERSION``) with its questions, anchors and answer/handwriting
  regions; ``PRINTED_SOLVE_LABEL_POLICY`` names the printed solve-label policy.
- ``materialize_template_difference_artifact`` creates or refreshes the reusable
  whiteout baseline of an artifact: the template with every approved exemption
  box set to white (``_erase_template_handwriting_rgb``).
- ``verify_template_artifacts`` verifies every artifact's paths and content and
  returns the canonical mapping (``_verified_template_manifest``,
  ``_safe_artifact_path``).

Moved unchanged from services/template_evidence.py, which still exposes these names.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from services.accelerator_artifacts import validate_uploaded_artifact_file
from services.template_anchor_crops import validate_anchor_crop_plan
from services.template_evidence_base import (
    MAX_IMAGE_EDGE,
    TemplateEvidenceError,
    _PAGE_TYPE_RE,
    _SHA256_RE,
    np,
)
from services.template_evidence_canonical import (
    _copy_file_atomic,
    _load_rgb_image,
    _save_rgb_png_atomic,
    _sha256_file,
    _validate_canonicalization,
)
from services.template_evidence_residue import _template_handwriting_region_mask
from services.template_evidence_schema import (
    _bind_slot_geometry,
    _is_int,
    _validate_printed_solve_anchors,
    _validated_template_handwriting_regions,
    normalize_question_anchors,
    normalize_question_schema,
    normalize_template_handwriting_regions,
    validate_template_confidence_gate,
)


ARTIFACT_VERSION = 1


PRINTED_SOLVE_LABEL_POLICY = "printed-solve-v1"


def build_template_artifact(
    template_image_path,
    template_dir,
    page_index,
    questions,
    *,
    blank_detection_enabled: bool = True,
    canonicalization: dict[str, Any] | None = None,
    question_anchors: list[dict] | None = None,
    template_answer_regions: list[dict] | None = None,
    template_handwriting_regions: list[dict] | None = None,
    template_mode: str | None = None,
) -> dict:
    """Create one normalized, immutable-by-contract template image artifact."""

    if not _is_int(page_index) or page_index < 0:
        raise ValueError("page_index must be a non-negative integer")
    if not isinstance(blank_detection_enabled, bool):
        raise ValueError("blank_detection_enabled must be a boolean")
    normalized_template_mode = None
    if template_mode is not None:
        normalized_template_mode = str(template_mode or "").strip().lower()
        if normalized_template_mode not in {"blank", "marked"}:
            raise ValueError("template_mode must be blank or marked")
    question_schema = normalize_question_schema(questions)
    question_numbers = [item["question_number"] for item in question_schema]
    normalized_question_anchors = (
        normalize_question_anchors(question_anchors)
        if question_anchors is not None
        else []
    )
    normalized_handwriting_regions = (
        normalize_template_handwriting_regions(template_handwriting_regions)
        if template_handwriting_regions is not None
        else None
    )
    normalized_answer_regions = (
        normalize_template_handwriting_regions(template_answer_regions)
        if template_answer_regions is not None
        else None
    )
    if normalized_answer_regions is not None:
        normalized_answer_regions = _bind_slot_geometry(question_schema, normalized_answer_regions)
    if question_anchors is not None or normalized_handwriting_regions is not None:
        validate_template_confidence_gate(
            normalized_question_anchors,
            normalized_handwriting_regions or [],
        )
    if normalized_handwriting_regions is not None and normalized_template_mode != "blank":
        handwriting_numbers = {
            item["question_number"] for item in normalized_handwriting_regions
        }
        if handwriting_numbers != set(question_numbers):
            raise ValueError(
                "template_handwriting_regions must cover exactly the question schema"
            )
    if normalized_answer_regions is not None:
        answer_numbers = {
            item["question_number"] for item in normalized_answer_regions
        }
        if answer_numbers != set(question_numbers):
            raise ValueError(
                "template_answer_regions must cover exactly the question schema"
            )
        question_by_number = {
            item["question_number"]: item for item in question_schema
        }
        printed_number_questions = {
            item["question_number"] for item in normalized_question_anchors
            if item.get("question_number_bbox")
        }
        if normalized_template_mode == "blank" and any(
            not item.get("regions")
            and str(question_by_number[item["question_number"]].get("question_type") or "")
            .strip()
            .lower()
            not in {"choice", "multiple_choice", "single_choice"}
            and not (
                question_by_number[item["question_number"]].get("answer_kind") == "drawing"
                or (question_by_number[item["question_number"]].get("question_type") == "solve"
                    and (item["question_number"] in printed_number_questions
                         or question_by_number[item["question_number"]].get("answer_kind") in {"drawing", "mixed"}))
            )
            for item in normalized_answer_regions
        ):
            raise ValueError("blank template fill/solve questions require an answer region")
    if question_anchors is not None:
        anchor_numbers = {
            anchor["question_number"] for anchor in normalized_question_anchors
        }
        if anchor_numbers != set(question_numbers):
            raise ValueError("question_anchors must cover exactly the question schema")
    if normalized_template_mode is not None and question_anchors is not None:
        # Old v1 artifacts did not distinguish an inferred solve type from a
        # declared solve, nor require a printed-number box. Freeze the new
        # policy on newly built paper artifacts instead of reinterpreting old
        # normalized schemas. A review draft omits authoritative anchors and
        # may be stored, but the modern matcher still refuses to grade it.
        _validate_printed_solve_anchors(question_schema, normalized_question_anchors, normalized_answer_regions)
    root = Path(template_dir)
    artifact_dir = root / "artifacts" / f"page_{page_index:03d}"
    output_path = artifact_dir / "template.png"
    validated_canonicalization = None
    if canonicalization is not None:
        source_path = Path(template_image_path)
        source_manifest = _verified_template_manifest(source_path)
        width = source_manifest["width"]
        height = source_manifest["height"]
        source_sha256 = source_manifest["sha256"]
        validated_canonicalization = _validate_canonicalization(
            root,
            canonicalization,
            canonical_sha256=source_sha256,
            canonical_width=width,
            canonical_height=height,
        )
        _copy_file_atomic(source_path, output_path)
    else:
        image = _load_rgb_image(template_image_path)
        height, width = image.shape[:2]
        _save_rgb_png_atomic(image, output_path)

    relative_path = output_path.relative_to(root).as_posix()
    artifact = {
        "version": ARTIFACT_VERSION,
        "template_image": relative_path,
        "sha256": _sha256_file(output_path),
        "width": width,
        "height": height,
        "question_schema": question_schema,
        "question_numbers": question_numbers,
        "blank_detection_enabled": blank_detection_enabled,
    }
    if question_anchors is not None:
        artifact["question_anchors"] = normalized_question_anchors
    if normalized_handwriting_regions is not None:
        artifact["template_handwriting_regions"] = normalized_handwriting_regions
    if normalized_answer_regions is not None:
        artifact["template_answer_regions"] = normalized_answer_regions
    if normalized_template_mode is not None:
        artifact["template_mode"] = normalized_template_mode
        artifact["question_label_policy"] = PRINTED_SOLVE_LABEL_POLICY
    if validated_canonicalization is not None:
        artifact["canonicalization"] = validated_canonicalization
    if normalized_handwriting_regions is not None and normalized_template_mode != "blank":
        # Keep the registration image intact, and materialize a separate
        # baseline with every approved model rectangle painted white.
        artifact = materialize_template_difference_artifact(root, artifact)
    return artifact


def _safe_artifact_path(template_dir, relative_path: object) -> tuple[str, str]:
    if not isinstance(relative_path, str) or not relative_path:
        raise ValueError("template_image must be a relative path")
    if "\x00" in relative_path:
        raise ValueError("template_image contains an invalid path")

    windows_path = PureWindowsPath(relative_path)
    posix_path = PurePosixPath(relative_path.replace("\\", "/"))
    if (
        windows_path.is_absolute()
        or bool(windows_path.drive)
        or posix_path.is_absolute()
        or any(part in {"", ".", ".."} for part in posix_path.parts)
    ):
        raise ValueError("template_image must stay inside template_dir")
    if posix_path.suffix.lower() != ".png":
        raise ValueError("template_image must be a PNG file")

    root = os.path.realpath(os.fspath(template_dir))
    resolved = os.path.realpath(os.path.join(root, *posix_path.parts))
    try:
        inside = os.path.commonpath((root, resolved)) == root
    except ValueError:
        inside = False
    if not inside or resolved == root:
        raise ValueError("template_image must stay inside template_dir")
    return resolved, posix_path.as_posix()


def _erase_template_handwriting_rgb(
    template_rgb: np.ndarray,
    regions: list[dict],
) -> np.ndarray:
    """Return an RGB template with every approved exemption box set to white."""
    if (
        not isinstance(template_rgb, np.ndarray)
        or template_rgb.dtype != np.uint8
        or template_rgb.ndim != 3
        or template_rgb.shape[2] != 3
    ):
        raise TemplateEvidenceError("template handwriting image is invalid")
    normalized = _validated_template_handwriting_regions(regions)
    height, width = template_rgb.shape[:2]
    mask = _template_handwriting_region_mask(normalized, width, height)
    cleaned = template_rgb.copy()
    cleaned[mask] = 255
    return np.ascontiguousarray(cleaned)


def materialize_template_difference_artifact(
    template_dir: str | os.PathLike[str],
    artifact: dict[str, Any],
) -> dict[str, Any]:
    """Create or refresh the reusable whiteout baseline for one artifact.

    The original artifact image remains untouched because its printed features
    are needed for page registration. The returned mapping records the second
    image and hash so verification and matcher reads are bound to the same
    approved handwriting boxes.
    """
    if not isinstance(artifact, dict):
        raise ValueError("template artifact must be an object")
    template_path, _relative_path = _safe_artifact_path(
        template_dir,
        artifact.get("template_image"),
    )
    template_manifest = _verified_template_manifest(template_path)
    expected_template_sha256 = artifact.get("sha256")
    if (
        not isinstance(expected_template_sha256, str)
        or not _SHA256_RE.fullmatch(expected_template_sha256)
        or template_manifest["sha256"] != expected_template_sha256
    ):
        raise ValueError("template artifact sha256 mismatch")
    width = artifact.get("width")
    height = artifact.get("height")
    if (width, height) != (
        template_manifest["width"],
        template_manifest["height"],
    ):
        raise ValueError("template artifact dimensions mismatch")
    regions = normalize_template_handwriting_regions(
        artifact.get("template_handwriting_regions") or []
    )
    validate_template_confidence_gate(
        normalize_question_anchors(artifact.get("question_anchors") or []),
        regions,
    )
    image = _load_rgb_image(template_path)
    if image.shape[1] != width or image.shape[0] != height:
        raise ValueError("template artifact image dimensions mismatch")
    difference_image = _erase_template_handwriting_rgb(image, regions)
    source_path = Path(template_path)
    difference_path = source_path.with_name("difference-template.png")
    _save_rgb_png_atomic(difference_image, difference_path)
    updated = dict(artifact)
    updated["difference_template_image"] = difference_path.relative_to(
        Path(template_dir).resolve()
    ).as_posix()
    updated["difference_template_sha256"] = _sha256_file(difference_path)
    return updated


def _verified_template_manifest(path: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        manifest = validate_uploaded_artifact_file(path, "canonical_template")
    except ValueError as exc:
        raise ValueError("template_image is invalid") from exc
    width = manifest["width"]
    height = manifest["height"]
    if width <= 0 or height <= 0 or max(width, height) > MAX_IMAGE_EDGE:
        raise ValueError("template_image has invalid dimensions")
    return manifest


def verify_template_artifacts(template_dir, raw_artifacts) -> dict:
    """Verify artifact paths and content, returning a canonical mapping."""

    if not isinstance(raw_artifacts, dict) or not raw_artifacts:
        raise ValueError("artifacts must be a non-empty object")

    verified: dict[str, dict] = {}
    for raw_page_type_id, raw in raw_artifacts.items():
        if not isinstance(raw_page_type_id, str):
            raise ValueError("page_type_id must be text")
        page_type_id = raw_page_type_id.strip()
        if not _PAGE_TYPE_RE.fullmatch(page_type_id):
            raise ValueError("page_type_id is invalid")
        if page_type_id in verified:
            raise ValueError(f"duplicate page_type_id: {page_type_id}")
        if not isinstance(raw, dict):
            raise ValueError(f"artifact {page_type_id} must be an object")
        if raw.get("version") != ARTIFACT_VERSION or isinstance(raw.get("version"), bool):
            raise ValueError(f"artifact {page_type_id} has unsupported version")
        if "question_label_policy" in raw and raw["question_label_policy"] != PRINTED_SOLVE_LABEL_POLICY:
            raise ValueError(f"artifact {page_type_id} has unsupported question label policy")

        path, relative_path = _safe_artifact_path(
            template_dir, raw.get("template_image")
        )
        if not os.path.isfile(path):
            raise ValueError(f"artifact {page_type_id} template_image does not exist")

        expected_hash = raw.get("sha256")
        if not isinstance(expected_hash, str) or not _SHA256_RE.fullmatch(expected_hash):
            raise ValueError(f"artifact {page_type_id} has invalid sha256")
        template_manifest = _verified_template_manifest(path)
        if template_manifest["sha256"] != expected_hash:
            raise ValueError(f"artifact {page_type_id} sha256 mismatch")

        width = raw.get("width")
        height = raw.get("height")
        if not _is_int(width) or not _is_int(height) or width <= 0 or height <= 0:
            raise ValueError(f"artifact {page_type_id} has invalid dimensions")
        actual_width = template_manifest["width"]
        actual_height = template_manifest["height"]
        if (width, height) != (actual_width, actual_height):
            raise ValueError(f"artifact {page_type_id} dimensions mismatch")

        raw_schema = raw.get("question_schema")
        if raw_schema is None:
            # Read old artifacts only to migrate their question identity; the
            # legacy answer coordinates are never returned or used.
            legacy_regions = raw.get("answer_regions")
            if not isinstance(legacy_regions, list):
                raise ValueError(
                    f"artifact {page_type_id} is missing question_schema"
                )
            raw_schema = [
                {
                    "question_number": region.get("question_number"),
                    "question_type": region.get("question_type", "solve"),
                    "choice_mode": region.get("choice_mode", "unknown"),
                }
                for region in legacy_regions
                if isinstance(region, dict)
            ]
        question_schema = normalize_question_schema(raw_schema)
        question_numbers = [
            item["question_number"] for item in question_schema
        ]
        blank_detection_enabled = raw.get("blank_detection_enabled", True)
        if not isinstance(blank_detection_enabled, bool):
            raise ValueError(
                f"artifact {page_type_id} has invalid blank_detection_enabled"
            )
        template_mode = raw.get("template_mode")
        if template_mode is not None:
            template_mode = str(template_mode or "").strip().lower()
            if template_mode not in {"blank", "marked"}:
                raise ValueError(
                    f"artifact {page_type_id} has invalid template_mode"
                )
        question_crop_plan = raw.get("question_crop_plan")
        if question_crop_plan is not None:
            question_crop_plan = validate_anchor_crop_plan(
                question_crop_plan,
                artifact_sha256=expected_hash,
            )
        raw_question_anchors = raw.get("question_anchors")
        question_anchors = None
        if raw_question_anchors is not None:
            question_anchors = normalize_question_anchors(raw_question_anchors)
            anchor_numbers = {
                anchor["question_number"] for anchor in question_anchors
            }
            if anchor_numbers != set(question_numbers):
                raise ValueError(
                    f"artifact {page_type_id} question_anchors do not match question_schema"
                )
        if (raw.get("question_label_policy") == PRINTED_SOLVE_LABEL_POLICY
                and raw_question_anchors is not None):
            _validate_printed_solve_anchors(question_schema, question_anchors or [], raw.get("template_answer_regions"))
        raw_handwriting_regions = raw.get("template_handwriting_regions")
        handwriting_regions = None
        if raw_handwriting_regions is not None:
            handwriting_regions = normalize_template_handwriting_regions(
                raw_handwriting_regions
            )
            handwriting_numbers = {
                item["question_number"] for item in handwriting_regions
            }
            if template_mode != "blank" and handwriting_numbers != set(question_numbers):
                raise ValueError(
                    f"artifact {page_type_id} template_handwriting_regions "
                    "do not match question_schema"
                )
        raw_answer_regions = raw.get("template_answer_regions")
        answer_regions = None
        if raw_answer_regions is not None:
            answer_regions = normalize_template_handwriting_regions(raw_answer_regions)
            answer_regions = _bind_slot_geometry(question_schema, answer_regions)
            answer_numbers = {item["question_number"] for item in answer_regions}
            if answer_numbers != set(question_numbers):
                raise ValueError(
                    f"artifact {page_type_id} template_answer_regions do not match question_schema"
                )
            question_by_number = {
                item["question_number"]: item for item in question_schema
            }
            printed_number_questions = {
                item["question_number"] for item in (question_anchors or [])
                if item.get("question_number_bbox")
            }
            if template_mode == "blank" and any(
                not item.get("regions")
                and str(question_by_number[item["question_number"]].get("question_type") or "")
                .strip()
                .lower()
                not in {"choice", "multiple_choice", "single_choice"}
                and not (
                    question_by_number[item["question_number"]].get("answer_kind") == "drawing"
                    or (question_by_number[item["question_number"]].get("question_type") == "solve"
                        and (item["question_number"] in printed_number_questions
                             or question_by_number[item["question_number"]].get("answer_kind") in {"drawing", "mixed"}))
                )
                for item in answer_regions
            ):
                raise ValueError(
                    f"artifact {page_type_id} non-choice question has a missing answer region"
                )

        try:
            validate_template_confidence_gate(
                question_anchors or [],
                handwriting_regions or [],
            )
        except TemplateEvidenceError as exc:
            raise ValueError(
                f"artifact {page_type_id} has untrusted template confidence"
            ) from exc
        difference_template_image = None
        difference_template_sha256 = None
        if raw.get("difference_template_image") is not None:
            difference_template_image, _difference_relative = _safe_artifact_path(
                template_dir,
                raw.get("difference_template_image"),
            )
            if not os.path.isfile(difference_template_image):
                raise ValueError(
                    f"artifact {page_type_id} difference_template_image does not exist"
                )
            difference_template_sha256 = raw.get("difference_template_sha256")
            if (
                not isinstance(difference_template_sha256, str)
                or not _SHA256_RE.fullmatch(difference_template_sha256)
            ):
                raise ValueError(
                    f"artifact {page_type_id} has invalid difference_template_sha256"
                )
            difference_manifest = _verified_template_manifest(difference_template_image)
            if difference_manifest["sha256"] != difference_template_sha256:
                raise ValueError(
                    f"artifact {page_type_id} difference template sha256 mismatch"
                )
            if (difference_manifest["width"], difference_manifest["height"]) != (
                width,
                height,
            ):
                raise ValueError(
                    f"artifact {page_type_id} difference template dimensions mismatch"
                )
            if handwriting_regions is None:
                raise ValueError(
                    f"artifact {page_type_id} difference template requires handwriting regions"
                )
        canonicalization = raw.get("canonicalization")
        if canonicalization is not None:
            canonicalization = _validate_canonicalization(
                template_dir,
                canonicalization,
                canonical_sha256=expected_hash,
                canonical_width=width,
                canonical_height=height,
            )
        verified_artifact = {
            "version": ARTIFACT_VERSION,
            "template_image": relative_path,
            "sha256": expected_hash,
            "width": width,
            "height": height,
            "question_schema": question_schema,
            "question_numbers": question_numbers,
            "blank_detection_enabled": blank_detection_enabled,
        }
        if template_mode is not None:
            verified_artifact["template_mode"] = template_mode
        if "question_label_policy" in raw:
            verified_artifact["question_label_policy"] = raw["question_label_policy"]
        if question_crop_plan is not None:
            verified_artifact["question_crop_plan"] = question_crop_plan
        if question_anchors is not None:
            verified_artifact["question_anchors"] = question_anchors
        if handwriting_regions is not None:
            verified_artifact["template_handwriting_regions"] = handwriting_regions
        if answer_regions is not None:
            verified_artifact["template_answer_regions"] = answer_regions
        if difference_template_image is not None:
            verified_artifact["difference_template_image"] = _difference_relative
            verified_artifact["difference_template_sha256"] = difference_template_sha256
        if canonicalization is not None:
            verified_artifact["canonicalization"] = canonicalization
        verified[page_type_id] = verified_artifact
    return verified
