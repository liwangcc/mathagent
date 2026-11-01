"""Validate bound-node evidence identity and retain legacy server replay.

Versioned local quality reports allow authenticated teacher-bound devices and
registered platform nodes to own full-column pixel processing within their
existing job scope. Older clients still use server replay.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from models_accelerator import AcceleratorJob, AcceleratorNode
from services.accelerator_artifacts import (
    BOUND_CLIENT_FULL_COLUMN_VALIDATOR,
    CANONICAL_TEMPLATE_CONTRACT_VERSION,
    CLIENT_EVIDENCE_CONTRACT_VERSION,
    CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION,
    CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION,
    MIN_AXIS_GRADIENT_COSINE,
    MIN_GRADIENT_COSINE,
    MIN_TEMPLATE_EDGE_RECALL,
    SERVER_REPLAYED_FULL_COLUMN_VALIDATOR,
    TEMPLATE_OCR_EVIDENCE_CONTRACT_VERSION,
    TEMPLATE_OCR_EVIDENCE_KIND,
    candidate_artifact_path,
    candidate_artifact_sha256,
    canonical_json,
    cleanup_candidate_artifacts,
    client_full_column_evidence_manifest,
    materialize_global_artifacts,
    replay_and_verify_artifacts,
    validate_artifact_manifest,
    validate_bound_client_quality_report,
    validate_slot_geometry_receipt,
    validate_uploaded_artifact_file,
)
from services.accelerator_jobs import (
    STUDENT_MULTI_EVIDENCE_STAGE,
    TEMPLATE_OCR_IDENTITY_VERSION,
    TEMPLATE_OCR_EVIDENCE_STAGE,
    _node_can_access_job,
    begin_server_validation,
    finish_server_validation,
    template_ocr_cache_path,
    template_ocr_layout_identity,
    validate_node_slot_geometry,
    utcnow,
)
from services.template_evidence import (
    MIN_PAGE_COVERAGE,
    build_focus_evidence_sheet_layout,
    build_full_column_difference_evidence_from_aligned,
)
from services.template_canonicalization_contract import (
    CANONICALIZATION_STAGE,
    validate_canonicalization_metadata,
)


LOCAL_FIRST_VALIDATION_ENABLED = os.environ.get(
    "ACCELERATOR_LOCAL_FIRST_VALIDATION_ENABLED", "true"
).strip().lower() in {"1", "true", "yes", "on"}
_LOCAL_FIRST_BACKENDS = {
    "windows_nvidia_gpu",
    "windows_cpu_opencv",
    "cvcuda_single_context_v2",
    "opencv_cpu_reference",
}


def _node_for_job(db: Session, job: AcceleratorJob) -> AcceleratorNode | None:
    if not job.assigned_node_id:
        return None
    return db.query(AcceleratorNode).filter(
        AcceleratorNode.id == job.assigned_node_id,
        AcceleratorNode.enabled == 1,
        AcceleratorNode.revoked_at.is_(None),
    ).first()


def _bound_full_column_node(db: Session, job: AcceleratorJob) -> AcceleratorNode:
    """Bind acceptance to the assigned node without widening its job scope."""
    node = _node_for_job(db, job)
    if (
        node is None
        or not _node_can_access_job(node, job)
        or (node.token_expires_at is not None and node.token_expires_at <= utcnow())
    ):
        raise ValueError("full-column local quality requires an authorized node")
    teacher_bound = (
        node.access_scope == "tenant"
        and node.lane == "teacher_desktop"
        and bool(str(node.device_id or "").strip())
        and node.owner_user_id == job.teacher_id
    )
    # Platform registration is admin-only. Keep the scheduler's existing
    # platform scope; do not turn ordinary tenant nodes into shared workers.
    platform_node = node.access_scope == "platform" and node.lane == "web_gpu"
    if not (teacher_bound or platform_node):
        raise ValueError("full-column local quality requires a bound or platform node")
    return node


def _eligible_for_local_first(db: Session, job: AcceleratorJob, candidate: dict[str, Any]) -> bool:
    if not LOCAL_FIRST_VALIDATION_ENABLED:
        return False
    node = _node_for_job(db, job)
    if node is None:
        return False
    try:
        capabilities = json.loads(node.capabilities_json or "{}")
    except (TypeError, ValueError):
        return False
    metrics = candidate.get("worker_metrics") or {}
    backend = str(metrics.get("backend") or capabilities.get("backend") or "")
    if backend not in _LOCAL_FIRST_BACKENDS:
        return False
    # Only the teacher's bound desktop or the platform-owned WSL GPU lane may
    # bypass full server replay. Tenant nodes must belong to the same teacher.
    if node.access_scope == "tenant" and node.owner_user_id != job.teacher_id:
        return False
    if node.access_scope not in {"tenant", "platform"}:
        return False
    incomplete_raw = candidate.get("incomplete_answer_regions")
    if incomplete_raw is None:
        incomplete_regions: set[str] = set()
    elif isinstance(incomplete_raw, list):
        incomplete_regions = {
            str(value).strip()
            for value in incomplete_raw
            if str(value).strip()
        }
    else:
        return False
    layout_json = getattr(job, "layout_snapshot_json", None)
    try:
        layout_payload = json.loads(layout_json) if layout_json else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    question_numbers = layout_payload.get("question_numbers")
    if not isinstance(question_numbers, list) or not question_numbers:
        # Jobs issued before the question-schema contract was introduced may
        # still expose only their legacy answer-region list.  Derive identity
        # for the confidence gate without restoring legacy boxes as trusted
        # geometry.
        legacy_regions = (
            layout_payload.get("_legacy_answer_regions")
            or layout_payload.get("answer_regions")
        )
        if isinstance(legacy_regions, list):
            question_numbers = [
                item.get("question_number")
                for item in legacy_regions
                if isinstance(item, dict)
            ]
        elif isinstance(layout_payload.get("question_schema"), list):
            question_numbers = [
                item.get("question_number")
                for item in layout_payload["question_schema"]
                if isinstance(item, dict)
            ]
    if not isinstance(question_numbers, list) or not question_numbers:
        return False
    known_regions: set[str] = set()
    for raw_number in question_numbers:
        question_number = str(raw_number or "").strip()
        if not question_number or question_number in known_regions:
            return False
        known_regions.add(question_number)
    legacy_regions = (
        layout_payload.get("_legacy_answer_regions")
        or layout_payload.get("answer_regions")
    )
    if isinstance(legacy_regions, list):
        required_regions = {
            str(item.get("question_number") or "").strip()
            for item in legacy_regions
            if isinstance(item, dict)
            and item.get("answer_source_required", True) is True
        }
        if incomplete_regions & required_regions:
            return False
    # Full-height columns have one source gate for the page, not one gate per
    # model-predicted answer box.
    if not incomplete_regions.issubset(known_regions):
        return False
    if not incomplete_regions and candidate.get("answer_source_complete") is not True:
        return False
    return (
        int(candidate.get("good_matches") or 0) >= 25
        and int(candidate.get("inliers") or 0) >= 25
        and float(candidate.get("inlier_ratio") or 0.0) >= 0.65
        # Zero is a valid RMSE for an exact registration; do not treat it as
        # a missing value through truthiness.
        and float(candidate.get("reprojection_rmse", math.inf)) <= 4.0
        and float(candidate.get("coverage") or 0.0) >= MIN_PAGE_COVERAGE
    )


def _candidate(job: AcceleratorJob) -> dict[str, Any]:
    if not job.candidate_json:
        raise ValueError("accelerator candidate is missing")
    value = json.loads(job.candidate_json)
    if not isinstance(value, dict):
        raise ValueError("accelerator candidate is invalid")
    return value


def _layout(job: AcceleratorJob) -> dict[str, Any]:
    value = json.loads(job.layout_snapshot_json)
    if not isinstance(value, dict):
        raise ValueError("layout snapshot is invalid")
    page_type_id = str(value.get("page_type_id") or "").strip()
    question_numbers = value.get("question_numbers")
    if (
        not page_type_id
        or not isinstance(question_numbers, list)
        or not question_numbers
    ):
        raise ValueError("layout snapshot is incomplete")
    return value


def _sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_crop_plan(
    raw_plan: object,
    *,
    artifact_sha256: str,
) -> dict[str, Any]:
    """Require the current full-height column contract exactly."""

    from services.template_anchor_crops import validate_anchor_crop_plan

    normalized = validate_anchor_crop_plan(
        raw_plan,
        artifact_sha256=artifact_sha256,
    )
    if normalized != raw_plan:
        raise ValueError("accelerator crop plan is not canonical")
    return normalized


def _copy_manifest_file(
    source_path: Path,
    target_path: Path,
    manifest: dict[str, Any],
    kind: str,
) -> str:
    """Publish and hash the exact bytes read from one candidate file handle."""

    expected = manifest[kind]
    expected_size = int(expected["byte_size"])
    digest = hashlib.sha256()
    total = 0
    try:
        with source_path.open("rb") as source, target_path.open("xb") as target:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                total += len(chunk)
                if total > expected_size:
                    raise ValueError(
                        f"candidate {kind} size does not match manifest"
                    )
                digest.update(chunk)
                target.write(chunk)
        actual_sha256 = digest.hexdigest()
        if total != expected_size:
            raise ValueError(f"candidate {kind} size does not match manifest")
        if actual_sha256 != expected["sha256"]:
            raise ValueError(f"candidate {kind} hash does not match manifest")
        return actual_sha256
    except Exception:
        target_path.unlink(missing_ok=True)
        raise


def _validate_client_multi_evidence_candidate(
    db: Session,
    job: AcceleratorJob,
    candidate: dict[str, Any],
    layout: dict[str, Any],
) -> AcceleratorJob:
    """Accept trusted ordered evidence images without decoding full page pixels."""

    validation: dict[str, Any] = {
        "validator": "trusted_client_evidence_multi_v3",
        "algorithm_version": job.algorithm_version,
        "worker_claims_trusted": True,
        "server_feature_extraction_performed": False,
        "server_image_decode_performed": False,
        "server_source_transform_replayed": False,
        "server_evidence_rendered": False,
    }
    stable_path: Path | None = None
    source_hash_matches = _sha256_file(job.source_path) == job.source_sha256
    template_hash_matches = _sha256_file(job.template_path) == job.template_sha256
    layout_hash_matches = (
        hashlib.sha256(canonical_json(layout).encode("utf-8")).hexdigest()
        == job.layout_snapshot_sha256
    )
    validation.update({
        "source_hash_matches": source_hash_matches,
        "template_hash_matches": template_hash_matches,
        "layout_hash_matches": layout_hash_matches,
    })
    if not source_hash_matches or not template_hash_matches or not layout_hash_matches:
        raise ValueError("immutable accelerator identity changed")

    page_type_id = layout["page_type_id"]
    if candidate.get("page_type_id") != page_type_id:
        raise ValueError("candidate page_type_id does not match the layout")
    manifest = candidate["artifact_manifest"]
    if manifest.get("contract_version") != CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION:
        raise ValueError("client multi-evidence contract is required")
    expected_identity = candidate_artifact_sha256(
        page_type_id=page_type_id,
        source_sha256=job.source_sha256,
        template_sha256=job.template_sha256,
        layout_snapshot_sha256=job.layout_snapshot_sha256,
        homography=candidate["homography"],
        artifact_manifest=manifest,
    )
    if candidate.get("artifact_sha256") != expected_identity:
        raise ValueError("candidate artifact identity does not match")

    legacy_regions = layout.get("_legacy_answer_regions") or []
    expected_questions = [
        str(region.get("question_number") or "").strip()
        for region in legacy_regions
    ]
    if not expected_questions:
        expected_questions = [
            str(value).strip() for value in layout.get("question_numbers") or []
        ]
    if not expected_questions or any(not value for value in expected_questions):
        raise ValueError("layout question sequence is invalid")
    question_numbers = [str(value) for value in candidate.get("question_numbers") or []]
    blank_question_numbers = [
        str(value) for value in candidate.get("blank_question_numbers") or []
    ]
    if question_numbers != expected_questions:
        raise ValueError("client evidence question sequence does not match layout")
    if not set(blank_question_numbers).issubset(set(expected_questions)):
        raise ValueError("client evidence blank sequence is invalid")

    evidence_images = list(manifest["evidence_images"])
    _canonical_crop_plan(
        layout.get("question_crop_plan"),
        artifact_sha256=job.template_sha256,
    )
    template_width = int(manifest["template_width"])
    template_height = int(manifest["template_height"])
    expected_sheets = build_focus_evidence_sheet_layout(
        list(legacy_regions),
        template_width,
        template_height,
        layout.get("question_crop_plan"),
    )
    if len(evidence_images) != len(expected_sheets):
        raise ValueError("client evidence image count does not match focus layout")
    evidence_locations = []
    public_sheets = []
    for position, (image, sheet) in enumerate(
        zip(evidence_images, expected_sheets),
        1,
    ):
        if (
            image.get("kind") != f"evidence_{position:02d}"
            or image.get("sheet_id") != sheet["sheet_id"]
            or image.get("regions") != sheet["regions"]
            or image.get("width") != sheet["width"]
            or image.get("height") != sheet["height"]
        ):
            raise ValueError("client evidence image does not match focus layout")
        public_sheets.append({
            "sheet_id": sheet["sheet_id"],
            "regions": list(sheet["regions"]),
        })
        evidence_locations.extend({
            "question_number": region["question_number"],
            "image_index": position,
            "answer_bbox_1000": list(region["bbox_1000"]),
        } for region in sheet["regions"])
    if [item["question_number"] for item in evidence_locations] != expected_questions:
        raise ValueError("client focus evidence question order is invalid")

    candidate_diagnostics = dict(candidate.get("evidence_diagnostics") or {})
    if (
        candidate_diagnostics.get("ownership") != "confirmed_bbox_focus_sheets"
        or candidate_diagnostics.get("evidence_sheets") != public_sheets
    ):
        raise ValueError("client focus evidence diagnostics do not match manifest")

    stable_root = Path(job.source_path).resolve().parent / (
        f"evidence-set-{uuid.uuid4().hex}"
    )
    stable_path = stable_root / "manifest.json"
    try:
        stable_root.mkdir(mode=0o700)
        persisted_images = []
        for image in evidence_images:
            kind = image["kind"]
            source = candidate_artifact_path(job.source_path, job.id, kind)
            target = stable_root / f"{kind}.png"
            _copy_manifest_file(source, target, {kind: image}, kind)
            persisted_images.append({"kind": kind, **image})
        index = {
            "contract_version": CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION,
            "evidence_images": persisted_images,
        }
        encoded_index = canonical_json(index).encode("utf-8")
        stable_path.write_bytes(encoded_index)
        artifact_sha256 = hashlib.sha256(encoded_index).hexdigest()
        validation.update({
            "page_type_id": page_type_id,
            "question_numbers": question_numbers,
            "blank_question_numbers": blank_question_numbers,
            "server_answer_source_complete": True,
            "evidence_diagnostics": {
                "ownership": "confirmed_bbox_focus_sheets",
                "answer_source_coverage": list(
                    candidate_diagnostics.get("answer_source_coverage") or []
                ),
                "incomplete_answer_regions": list(
                    candidate_diagnostics.get("incomplete_answer_regions") or []
                ),
                "evidence_sheets": public_sheets,
                "evidence_locations": evidence_locations,
            },
        })
        persisted = finish_server_validation(
            db,
            job.id,
            accepted=True,
            validation=validation,
            artifact_path=str(stable_path.resolve()),
            artifact_sha256=artifact_sha256,
        )
        cleanup_candidate_artifacts(job.source_path, job.id)
        return persisted
    except Exception:
        if stable_path is not None:
            import shutil
            shutil.rmtree(stable_root, ignore_errors=True)
        raise


def _validate_client_full_column_evidence_candidate(
    db: Session,
    job: AcceleratorJob,
    candidate: dict[str, Any],
    layout: dict[str, Any],
) -> AcceleratorJob:
    """Accept new bound-device columns or replay legacy full-column uploads."""

    validation: dict[str, Any] = {
        "validator": SERVER_REPLAYED_FULL_COLUMN_VALIDATOR,
        "algorithm_version": job.algorithm_version,
        "worker_claims_trusted": False,
        "server_feature_extraction_performed": False,
        "server_image_decode_performed": True,
        "server_source_transform_replayed": False,
        "server_evidence_rendered": False,
    }
    source_hash_matches = _sha256_file(job.source_path) == job.source_sha256
    template_hash_matches = _sha256_file(job.template_path) == job.template_sha256
    layout_hash_matches = (
        hashlib.sha256(canonical_json(layout).encode("utf-8")).hexdigest()
        == job.layout_snapshot_sha256
    )
    validation.update({
        "source_hash_matches": source_hash_matches,
        "template_hash_matches": template_hash_matches,
        "layout_hash_matches": layout_hash_matches,
    })
    if not source_hash_matches or not template_hash_matches or not layout_hash_matches:
        raise ValueError("immutable accelerator identity changed")

    page_type_id = layout["page_type_id"]
    manifest = validate_artifact_manifest(candidate["artifact_manifest"])
    if manifest != candidate["artifact_manifest"]:
        raise ValueError("client full-column artifact manifest is not canonical")
    if (
        candidate.get("page_type_id") != page_type_id
        or manifest.get("contract_version")
        != CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION
    ):
        raise ValueError("client full-column evidence contract is required")
    expected_identity = candidate_artifact_sha256(
        page_type_id=page_type_id,
        source_sha256=job.source_sha256,
        template_sha256=job.template_sha256,
        layout_snapshot_sha256=job.layout_snapshot_sha256,
        homography=candidate["homography"],
        artifact_manifest=manifest,
    )
    if candidate.get("artifact_sha256") != expected_identity:
        raise ValueError("candidate artifact identity does not match")

    expected_questions = [
        str(value).strip() for value in layout.get("question_numbers") or []
    ]
    if not expected_questions or any(not value for value in expected_questions):
        raise ValueError("layout question sequence is invalid")
    question_numbers = [str(value) for value in candidate.get("question_numbers") or []]
    blank_question_numbers = [
        str(value) for value in candidate.get("blank_question_numbers") or []
    ]
    if question_numbers != expected_questions:
        raise ValueError("client evidence question sequence does not match layout")
    if not set(blank_question_numbers).issubset(set(expected_questions)):
        raise ValueError("client evidence blank sequence is invalid")

    plan = layout.get("question_crop_plan")
    if plan is None:
        raise ValueError("client full-column evidence requires question_crop_plan")
    normalized_plan = _canonical_crop_plan(
        plan,
        artifact_sha256=job.template_sha256,
    )
    template_width = int(manifest["template_width"])
    template_height = int(manifest["template_height"])
    # Match _anchor_block_crops exactly: both endpoints are rounded before the
    # slice width is calculated. Rounding the width delta directly can be off by
    # one pixel at a frozen column boundary.
    expected_widths = []
    for column in normalized_plan["parameters"]["columns"]:
        left = int(round(column["x1_1000"] * template_width / 1000))
        right = int(round(column["x2_1000"] * template_width / 1000))
        if right <= left:
            raise ValueError("client evidence column is empty")
        expected_widths.append(right - left)
    evidence_images = list(manifest["evidence_images"])
    if len(evidence_images) != len(expected_widths):
        raise ValueError("client evidence column count does not match layout")
    public_sheets = []
    flattened: list[str] = []
    for position, (image, expected_width) in enumerate(
        zip(evidence_images, expected_widths), 1
    ):
        if (
            image.get("kind") != f"evidence_{position:02d}"
            or image.get("sheet_id") != f"column_{position:02d}"
            or image.get("width") != expected_width
            or image.get("height") != template_height
        ):
            raise ValueError("client evidence column dimensions do not match layout")
        column_questions = list(image.get("question_numbers") or [])
        if not column_questions:
            raise ValueError("client evidence column question order is invalid")
        flattened.extend(column_questions)
        public_sheets.append({
            "sheet_id": image["sheet_id"],
            "question_numbers": column_questions,
        })
    if flattened != expected_questions:
        raise ValueError("client evidence column question order is invalid")
    candidate_diagnostics = dict(candidate.get("evidence_diagnostics") or {})
    if (
        candidate_diagnostics.get("ownership") != "full_column_difference"
        or candidate_diagnostics.get("evidence_sheets") != public_sheets
    ):
        raise ValueError("client full-column evidence diagnostics do not match manifest")

    quality_report = None
    if "local_quality_report" in candidate:
        node = _bound_full_column_node(db, job)
        if (
            candidate.get("answer_source_complete") is not True
            or candidate.get("incomplete_answer_regions")
        ):
            raise ValueError("bound client full-column answer source is incomplete")
        quality_report = validate_bound_client_quality_report(
            candidate["local_quality_report"],
            source_sha256=job.source_sha256,
            template_sha256=job.template_sha256,
            layout_snapshot_sha256=job.layout_snapshot_sha256,
            artifact_sha256=expected_identity,
            page_type_id=page_type_id,
            coverage=candidate["coverage"],
        )
        validation.update({
            "validator": BOUND_CLIENT_FULL_COLUMN_VALIDATOR,
            "worker_claims_trusted": True,
            "bound_node_identity_validated": True,
            "bound_node_id": node.id,
            "node_access_scope": node.access_scope,
            "local_quality_report_validated": True,
            "local_quality_report": quality_report,
            "server_image_decode_performed": False,
        })

    source_root = Path(job.source_path).resolve().parent
    stable_root = source_root / (
        f"evidence-set-{uuid.uuid4().hex}"
    )
    stable_path = stable_root / "manifest.json"
    replay_root: Path | None = None
    try:
        # The structural PNG reader checks bounded rows without materializing
        # images. Every published byte is checked against its job-bound hash.
        for image in evidence_images:
            kind = image["kind"]
            source = candidate_artifact_path(job.source_path, job.id, kind)
            actual = validate_uploaded_artifact_file(source, kind)
            expected_file = {
                key: image[key]
                for key in ("sha256", "byte_size", "width", "height", "channels", "format")
            }
            if actual != expected_file:
                raise ValueError("client full-column evidence bytes do not match manifest")

        if quality_report is not None:
            stable_root.mkdir(mode=0o700)
            persisted_images = []
            for image in evidence_images:
                kind = image["kind"]
                source = candidate_artifact_path(job.source_path, job.id, kind)
                _copy_manifest_file(source, stable_root / f"{kind}.png", {kind: image}, kind)
                persisted_images.append({"kind": kind, **image})
            encoded_index = canonical_json({
                "contract_version": CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION,
                "evidence_images": persisted_images,
            }).encode("utf-8")
            stable_path.write_bytes(encoded_index)
            validation.update({
                "page_type_id": page_type_id,
                "question_numbers": expected_questions,
                "blank_question_numbers": blank_question_numbers,
                "client_answer_source_complete": True,
                "client_evidence_manifest_validated": True,
                "client_evidence_pixels_trusted": True,
                "candidate_reused_for_acceptance": True,
                "evidence_diagnostics": {
                    "ownership": "full_column_difference",
                    "answer_source_complete": True,
                    "evidence_sheets": public_sheets,
                },
            })
            persisted = finish_server_validation(
                db,
                job.id,
                accepted=True,
                validation=validation,
                artifact_path=str(stable_path.resolve()),
                artifact_sha256=hashlib.sha256(encoded_index).hexdigest(),
            )
            cleanup_candidate_artifacts(job.source_path, job.id)
            if (
                not persisted.server_artifact_path
                or Path(persisted.server_artifact_path).resolve() != stable_path.resolve()
            ):
                shutil.rmtree(stable_root, ignore_errors=True)
            return persisted

        replay_root = Path(tempfile.mkdtemp(
            prefix=".accelerator-full-column-",
            dir=str(source_root),
        ))
        materialized = materialize_global_artifacts(
            job.source_path,
            job.template_path,
            candidate["homography"],
            replay_root / "transform",
        )
        replay_manifest = materialized.manifest
        for key in (
            "source_width",
            "source_height",
            "template_width",
            "template_height",
        ):
            if manifest.get(key) != replay_manifest.get(key):
                raise ValueError("client evidence dimensions do not match immutable assets")

        replay = replay_and_verify_artifacts(
            source_path=job.source_path,
            template_path=job.template_path,
            homography=candidate["homography"],
            artifact_manifest=replay_manifest,
            aligned_page_path=materialized.aligned_page_path,
            source_mask_path=materialized.source_mask_path,
        )
        # The identity hash was checked before decoding; check it once more
        # after rendering so a concurrent replacement cannot be published.
        if (
            _sha256_file(job.source_path) != job.source_sha256
            or _sha256_file(job.template_path) != job.template_sha256
        ):
            raise ValueError("immutable accelerator identity changed during replay")

        worker_coverage = float(candidate["coverage"])
        coverage_delta = abs(worker_coverage - replay.coverage)
        validation.update({
            "server_coverage": replay.coverage,
            "worker_coverage_delta": coverage_delta,
            "aligned_replay_mae": replay.aligned_mae,
            "aligned_replay_p99": replay.aligned_p99,
            "projected_source_quad": replay.projected_quad,
            "template_edge_recall": replay.template_edge_recall,
            "gradient_cosine": replay.gradient_cosine,
            "gradient_x_cosine": replay.gradient_x_cosine,
            "gradient_y_cosine": replay.gradient_y_cosine,
            "template_edge_pixels": replay.template_edge_pixels,
            "aligned_edge_pixels": replay.aligned_edge_pixels,
            "client_evidence_manifest_validated": True,
            "client_evidence_pixels_trusted": False,
        })
        if replay.coverage < MIN_PAGE_COVERAGE or coverage_delta > 0.02:
            raise ValueError("candidate coverage failed server replay")
        if (
            replay.template_edge_recall < MIN_TEMPLATE_EDGE_RECALL
            or replay.gradient_cosine < MIN_GRADIENT_COSINE
            or replay.gradient_x_cosine < MIN_AXIS_GRADIENT_COSINE
            or replay.gradient_y_cosine < MIN_AXIS_GRADIENT_COSINE
        ):
            raise ValueError("candidate structural alignment failed")

        rendered = build_full_column_difference_evidence_from_aligned(
            replay.aligned_rgb,
            replay.source_mask,
            replay.template_rgb,
            layout,
            replay_root / "evidence",
            diagnostics={
                "validation_mode": SERVER_REPLAYED_FULL_COLUMN_VALIDATOR,
                "raw_pixel_to_template_pixel": candidate["homography"],
                "source_mask_replayed": True,
                "client_preprocessing_accepted": False,
            },
        )
        rendered_paths = tuple(
            Path(path) for path in (rendered.evidence_paths or (rendered.evidence_path,))
            if str(path).strip()
        )
        if (
            rendered.page_type_id != page_type_id
            or list(rendered.question_numbers) != expected_questions
            or not rendered_paths
            or rendered.diagnostics.get("answer_source_complete") is not True
        ):
            raise ValueError("server full-column evidence render is incomplete")
        rendered_columns = [
            {
                "sheet_id": column["sheet_id"],
                "question_numbers": list(column.get("question_numbers") or []),
            }
            for column in rendered.diagnostics.get("evidence_columns") or []
        ]
        if len(rendered_columns) != len(rendered_paths):
            raise ValueError("server full-column evidence column count is invalid")
        rendered_manifest = client_full_column_evidence_manifest(
            replay_manifest,
            list(rendered_paths),
            rendered_columns,
        )

        stable_root.mkdir(mode=0o700)
        persisted_images = []
        for image, source in zip(rendered_manifest["evidence_images"], rendered_paths):
            kind = image["kind"]
            target = stable_root / f"{kind}.png"
            _copy_manifest_file(source, target, {kind: image}, kind)
            persisted_images.append({"kind": kind, **image})
        index = {
            "contract_version": CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION,
            "evidence_images": persisted_images,
        }
        encoded_index = canonical_json(index).encode("utf-8")
        stable_path.write_bytes(encoded_index)
        artifact_sha256 = hashlib.sha256(encoded_index).hexdigest()
        validation.update({
            "page_type_id": page_type_id,
            "question_numbers": list(rendered.question_numbers),
            "blank_question_numbers": list(rendered.blank_question_numbers),
            "server_answer_source_complete": True,
            "server_source_transform_replayed": True,
            "server_evidence_rendered": True,
            "candidate_reused_for_acceptance": False,
            "evidence_diagnostics": {
                "ownership": "full_column_difference",
                "evidence_sheets": [
                    {
                        "sheet_id": image["sheet_id"],
                        "question_numbers": list(image["question_numbers"]),
                    }
                    for image in persisted_images
                ],
            },
        })
        persisted = finish_server_validation(
            db,
            job.id,
            accepted=True,
            validation=validation,
            artifact_path=str(stable_path.resolve()),
            artifact_sha256=artifact_sha256,
        )
        cleanup_candidate_artifacts(job.source_path, job.id)
        persisted_path = (
            Path(persisted.server_artifact_path).resolve()
            if persisted.server_artifact_path
            else None
        )
        if persisted_path != stable_path.resolve():
            shutil.rmtree(stable_root, ignore_errors=True)
        return persisted
    except Exception:
        shutil.rmtree(stable_root, ignore_errors=True)
        raise
    finally:
        if replay_root is not None:
            shutil.rmtree(replay_root, ignore_errors=True)


def _validate_client_evidence_candidate(
    db: Session,
    job: AcceleratorJob,
    candidate: dict[str, Any],
    layout: dict[str, Any],
) -> AcceleratorJob:
    """Keep validation for already queued single-montage compatibility jobs."""

    source_hash_matches = _sha256_file(job.source_path) == job.source_sha256
    template_hash_matches = _sha256_file(job.template_path) == job.template_sha256
    layout_hash_matches = (
        hashlib.sha256(canonical_json(layout).encode("utf-8")).hexdigest()
        == job.layout_snapshot_sha256
    )
    if not source_hash_matches or not template_hash_matches or not layout_hash_matches:
        raise ValueError("immutable accelerator identity changed")
    page_type_id = layout["page_type_id"]
    manifest = candidate["artifact_manifest"]
    if (
        candidate.get("page_type_id") != page_type_id
        or manifest.get("contract_version") != CLIENT_EVIDENCE_CONTRACT_VERSION
    ):
        raise ValueError("client evidence contract is required")
    expected_identity = candidate_artifact_sha256(
        page_type_id=page_type_id,
        source_sha256=job.source_sha256,
        template_sha256=job.template_sha256,
        layout_snapshot_sha256=job.layout_snapshot_sha256,
        homography=candidate["homography"],
        artifact_manifest=manifest,
    )
    if candidate.get("artifact_sha256") != expected_identity:
        raise ValueError("candidate artifact identity does not match")
    expected_questions = [
        str(value).strip() for value in layout.get("question_numbers") or []
    ]
    question_numbers = [str(value) for value in candidate.get("question_numbers") or []]
    blank_question_numbers = [
        str(value) for value in candidate.get("blank_question_numbers") or []
    ]
    if question_numbers != expected_questions:
        raise ValueError("client evidence question sequence does not match layout")
    if not set(blank_question_numbers).issubset(set(expected_questions)):
        raise ValueError("client evidence blank sequence is invalid")

    evidence_path = candidate_artifact_path(job.source_path, job.id, "evidence")
    stable_path = Path(job.source_path).resolve().parent / f"evidence-{uuid.uuid4().hex}.png"
    try:
        artifact_sha256 = _copy_manifest_file(
            evidence_path,
            stable_path,
            manifest,
            "evidence",
        )
        validation = {
            "validator": "trusted_client_evidence_v1",
            "algorithm_version": job.algorithm_version,
            "worker_claims_trusted": True,
            "server_feature_extraction_performed": False,
            "server_image_decode_performed": False,
            "server_source_transform_replayed": False,
            "server_evidence_rendered": False,
            "source_hash_matches": True,
            "template_hash_matches": True,
            "layout_hash_matches": True,
            "page_type_id": page_type_id,
            "question_numbers": question_numbers,
            "blank_question_numbers": blank_question_numbers,
            "server_answer_source_complete": True,
            "evidence_diagnostics": dict(candidate.get("evidence_diagnostics") or {}),
        }
        persisted = finish_server_validation(
            db,
            job.id,
            accepted=True,
            validation=validation,
            artifact_path=str(stable_path),
            artifact_sha256=artifact_sha256,
        )
        cleanup_candidate_artifacts(job.source_path, job.id)
        return persisted
    except Exception:
        stable_path.unlink(missing_ok=True)
        raise


def _validate_canonicalization_candidate(
    db: Session,
    job: AcceleratorJob,
    candidate: dict[str, Any],
) -> AcceleratorJob:
    """Accept a canonical PNG without decoding or replaying its pixels."""

    validation: dict[str, Any] = {
        "validator": "client_canonical_template_v1",
        "algorithm_version": job.algorithm_version,
        "worker_claims_trusted": True,
        "server_image_decode_performed": False,
        "server_source_transform_replayed": False,
    }
    stable_path: Path | None = None
    manifest = candidate.get("artifact_manifest") or {}
    if manifest.get("contract_version") != CANONICAL_TEMPLATE_CONTRACT_VERSION:
        raise ValueError("canonical template contract is required")
    source_hash_matches = _sha256_file(job.source_path) == job.source_sha256
    validation["source_hash_matches"] = source_hash_matches
    if not source_hash_matches:
        raise ValueError("immutable accelerator source changed")

    candidate_path = candidate_artifact_path(
        job.source_path,
        job.id,
        "canonical_template",
    )
    actual_file = validate_uploaded_artifact_file(
        candidate_path,
        "canonical_template",
    )
    if actual_file != manifest.get("canonical_template"):
        raise ValueError("canonical template file does not match manifest")
    canonicalization = validate_canonicalization_metadata(
        candidate.get("canonicalization"),
        source_sha256=job.source_sha256,
        canonical_manifest=actual_file,
    )
    validation.update({
        "canonical_width": canonicalization["canonical_width"],
        "canonical_height": canonicalization["canonical_height"],
        "canonicalization_version": canonicalization["version"],
        "canonicalization_confidence": canonicalization["confidence"],
    })

    stable_path = Path(job.source_path).resolve().parent / (
        f"canonical-{uuid.uuid4().hex}.png"
    )
    try:
        os.replace(candidate_path, stable_path)
        persisted = finish_server_validation(
            db,
            job.id,
            accepted=True,
            validation=validation,
            artifact_path=str(stable_path),
            artifact_sha256=actual_file["sha256"],
        )
        cleanup_candidate_artifacts(job.source_path, job.id)
        persisted_path = (
            Path(persisted.server_artifact_path).resolve()
            if persisted.server_artifact_path
            else None
        )
        if persisted_path != stable_path.resolve():
            stable_path.unlink(missing_ok=True)
        return persisted
    except Exception:
        if stable_path is not None:
            stable_path.unlink(missing_ok=True)
        raise


def _validate_template_ocr_evidence_candidate(
    db: Session,
    job: AcceleratorJob,
    candidate: dict[str, Any],
) -> AcceleratorJob:
    """Publish a bound template OCR montage without decoding its pixels."""

    layout = json.loads(job.layout_snapshot_json)
    if not isinstance(layout, dict) or not isinstance(
        layout.get("question_crop_plan"), dict
    ):
        raise ValueError("template OCR evidence layout snapshot is invalid")
    normalized_plan = _canonical_crop_plan(
        layout["question_crop_plan"],
        artifact_sha256=job.source_sha256,
    )
    layout_hash_matches = (
        hashlib.sha256(canonical_json(layout).encode("utf-8")).hexdigest()
        == job.layout_snapshot_sha256
    )
    stable_identity = template_ocr_layout_identity(
        teacher_id=job.teacher_id,
        source_sha256=job.source_sha256,
        layout_snapshot=layout,
        algorithm_version=job.algorithm_version,
    )
    stable_identity_matches = bool(
        stable_identity
        and stable_identity == layout.get("template_identity")
    )
    reusable = stable_identity_matches
    validation: dict[str, Any] = {
        "validator": "template_ocr_evidence_v3" if reusable else "template_ocr_evidence_v2",
        "algorithm_version": job.algorithm_version,
        "worker_claims_trusted": True,
        "server_image_decode_performed": False,
        "server_source_transform_replayed": False,
        "server_evidence_rendered": False,
        "contract_version": TEMPLATE_OCR_EVIDENCE_CONTRACT_VERSION,
        "layout_hash_matches": layout_hash_matches,
    }
    stable_path: Path | None = None
    source = Path(job.source_path).resolve()
    template = Path(job.template_path).resolve()
    canonical_hash_matches = (
        source == template
        and job.source_sha256 == job.template_sha256
        and _sha256_file(source) == job.source_sha256
    )
    identity = candidate.get("input_identity")
    identity_matches = identity == {
        "canonical_sha256": job.source_sha256,
        "layout_snapshot_sha256": job.layout_snapshot_sha256,
    }
    validation.update({
        "canonical_hash_matches": canonical_hash_matches,
        "layout_hash_matches": layout_hash_matches,
        "worker_input_identity_matches": identity_matches,
        "template_identity_matches": stable_identity_matches,
    })
    if (
        not canonical_hash_matches
        or not layout_hash_matches
        or not identity_matches
        or (layout.get("identity_version") and not stable_identity_matches)
    ):
        raise ValueError("immutable template OCR evidence identity changed")

    manifest = candidate.get("artifact_manifest") or {}
    if manifest.get("contract_version") != TEMPLATE_OCR_EVIDENCE_CONTRACT_VERSION:
        raise ValueError("template OCR evidence contract is required")
    candidate_path = candidate_artifact_path(
        job.source_path,
        job.id,
        TEMPLATE_OCR_EVIDENCE_KIND,
    )
    actual_file = validate_uploaded_artifact_file(
        candidate_path,
        TEMPLATE_OCR_EVIDENCE_KIND,
    )
    if actual_file != manifest.get(TEMPLATE_OCR_EVIDENCE_KIND):
        raise ValueError("template OCR evidence file does not match manifest")

    staging_path: Path | None = None
    published_new = False
    if reusable:
        stable_path = template_ocr_cache_path(
            job.source_path,
            teacher_id=job.teacher_id,
            identity=str(stable_identity),
        ).resolve()
        stable_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path = stable_path.with_name(
            f".{stable_path.name}.{uuid.uuid4().hex}.tmp"
        )
    else:
        stable_path = Path(job.source_path).resolve().parent / (
            f"template-ocr-evidence-{uuid.uuid4().hex}.png"
        )
    try:
        artifact_sha256 = _copy_manifest_file(
            candidate_path,
            staging_path or stable_path,
            manifest,
            TEMPLATE_OCR_EVIDENCE_KIND,
        )
        if reusable and stable_path is not None and staging_path is not None:
            if stable_path.exists():
                existing_file = validate_uploaded_artifact_file(
                    stable_path,
                    TEMPLATE_OCR_EVIDENCE_KIND,
                )
                if existing_file["sha256"] != artifact_sha256:
                    raise ValueError("template OCR cache identity has conflicting bytes")
                staging_path.unlink(missing_ok=True)
                artifact_sha256 = existing_file["sha256"]
            else:
                os.replace(staging_path, stable_path)
                published_new = True
            validation.update({
                "reusable": True,
                "identity_version": TEMPLATE_OCR_IDENTITY_VERSION,
                "template_identity": stable_identity,
                "canonical_sha256": job.source_sha256,
                "layout_snapshot_sha256": job.layout_snapshot_sha256,
                "page_index": layout.get("page_index"),
                "cache_path": str(stable_path.resolve()),
            })
        validation.update({
            "artifact_width": actual_file["width"],
            "artifact_height": actual_file["height"],
            "artifact_byte_size": actual_file["byte_size"],
        })
        persisted = finish_server_validation(
            db,
            job.id,
            accepted=True,
            validation=validation,
            artifact_path=str(stable_path),
            artifact_sha256=artifact_sha256,
        )
        cleanup_candidate_artifacts(job.source_path, job.id)
        persisted_path = (
            Path(persisted.server_artifact_path).resolve()
            if persisted.server_artifact_path
            else None
        )
        if persisted_path != stable_path.resolve():
            if not reusable or published_new:
                stable_path.unlink(missing_ok=True)
        return persisted
    except Exception:
        if staging_path is not None:
            staging_path.unlink(missing_ok=True)
        elif stable_path is not None and (not reusable or published_new):
            stable_path.unlink(missing_ok=True)
        raise


def validate_candidate_job(db: Session, job_id: str) -> AcceleratorJob:
    job = db.query(AcceleratorJob).filter(AcceleratorJob.id == job_id).first()
    if not job:
        raise LookupError("accelerator job not found")
    if job.status == "validated":
        return job
    try:
        begin_server_validation(db, job_id)
    except ValueError:
        # A retry can observe candidate_received immediately before another
        # application worker wins the validation CAS.  Losing that CAS is not
        # a validation failure and must not demote the winner's in-flight row.
        current = (
            db.query(AcceleratorJob)
            .execution_options(populate_existing=True)
            .filter(AcceleratorJob.id == job_id)
            .one()
        )
        if current.status in {
            "server_validating",
            "validated",
            "server_fallback",
            "fallback_running",
            "fallback_completed",
            "fallback_failed",
            "consumed",
            "superseded",
        }:
            return current
        raise
    candidate = _candidate(job)
    if job.stage == CANONICALIZATION_STAGE:
        try:
            return _validate_canonicalization_candidate(db, job, candidate)
        except (ValueError, OSError, KeyError) as exc:
            cleanup_candidate_artifacts(job.source_path, job.id)
            return finish_server_validation(
                db,
                job.id,
                accepted=False,
                validation={
                    "validator": "client_canonical_template_v1",
                    "reason": "canonical_template_contract_rejected",
                    "error_type": type(exc).__name__,
                    "server_image_decode_performed": False,
                    "server_source_transform_replayed": False,
                },
            )
    if job.stage == TEMPLATE_OCR_EVIDENCE_STAGE:
        try:
            return _validate_template_ocr_evidence_candidate(db, job, candidate)
        except (ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
            cleanup_candidate_artifacts(job.source_path, job.id)
            return finish_server_validation(
                db,
                job.id,
                accepted=False,
                validation={
                    "validator": "template_ocr_evidence_v2",
                    "reason": "template_ocr_evidence_contract_rejected",
                    "error_type": type(exc).__name__,
                    "server_image_decode_performed": False,
                    "server_source_transform_replayed": False,
                    "server_evidence_rendered": False,
                },
            )
    contract_version = str(
        (candidate.get("artifact_manifest") or {}).get("contract_version") or ""
    )
    expected_contracts = (
        {
            CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION,
            # Accept already-issued v3 leases until they expire; new jobs are
            # always queued with the full-column contract above.
            CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION,
        }
        if job.stage == STUDENT_MULTI_EVIDENCE_STAGE
        else {CLIENT_EVIDENCE_CONTRACT_VERSION}
        if job.stage == "template_registration"
        else set()
    )
    if contract_version not in expected_contracts:
        cleanup_candidate_artifacts(job.source_path, job.id)
        return finish_server_validation(
            db,
            job.id,
            accepted=False,
            validation={
                "validator": "trusted_client_evidence_v1",
                "reason": "client_evidence_contract_required",
                "server_image_decode_performed": False,
                "server_source_transform_replayed": False,
                "server_evidence_rendered": False,
            },
        )
    try:
        layout = _layout(job)
        # Also cover candidates persisted before this capability gate shipped.
        validate_node_slot_geometry(_node_for_job(db, job), job.stage, layout)
        validate_slot_geometry_receipt(
            candidate, stage=job.stage, layout=layout,
            layout_snapshot_sha256=job.layout_snapshot_sha256,
        )
        if not _eligible_for_local_first(db, job, candidate):
            raise ValueError("client evidence node or confidence is not qualified")
        if job.stage == STUDENT_MULTI_EVIDENCE_STAGE:
            if contract_version == CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION:
                return _validate_client_full_column_evidence_candidate(
                    db, job, candidate, layout
                )
            return _validate_client_multi_evidence_candidate(db, job, candidate, layout)
        return _validate_client_evidence_candidate(db, job, candidate, layout)
    except (ValueError, OSError, KeyError) as exc:
        cleanup_candidate_artifacts(job.source_path, job.id)
        return finish_server_validation(
            db,
            job.id,
            accepted=False,
            validation={
                "validator": (
                    (
                        (
                            BOUND_CLIENT_FULL_COLUMN_VALIDATOR
                            if "local_quality_report" in candidate
                            else SERVER_REPLAYED_FULL_COLUMN_VALIDATOR
                        )
                        if contract_version == CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION
                        else "trusted_client_evidence_multi_v3"
                    )
                    if job.stage == STUDENT_MULTI_EVIDENCE_STAGE
                    else "trusted_client_evidence_v1"
                ),
                "reason": "client_evidence_contract_rejected",
                "error_type": type(exc).__name__,
                "server_image_decode_performed": False,
                "server_source_transform_replayed": False,
                "server_evidence_rendered": False,
            },
        )


def try_validate_candidate_job(db: Session, job_id: str) -> AcceleratorJob:
    """Run metadata validation and fail closed on an unexpected error."""

    try:
        return validate_candidate_job(db, job_id)
    except Exception as exc:
        db.rollback()
        job = db.query(AcceleratorJob).filter(AcceleratorJob.id == job_id).first()
        if job:
            cleanup_candidate_artifacts(job.source_path, job.id)
        if job and job.status == "server_validating":
            return finish_server_validation(
                db,
                job_id,
                accepted=False,
                validation={
                    "reason": "server_validation_internal_error",
                    "error_type": type(exc).__name__,
                    "server_image_decode_performed": False,
                    "server_source_transform_replayed": False,
                    "server_evidence_rendered": False,
                },
            )
        raise
