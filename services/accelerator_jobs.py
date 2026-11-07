"""Lease-based, fail-closed orchestration for opportunistic preprocessing nodes."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import uuid
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import and_, case, exists, false, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models_accelerator import AcceleratorJob, AcceleratorNode
from services.accelerator_artifacts import (
    CLIENT_EVIDENCE_CONTRACT_VERSION,
    CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION,
    CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION,
    CLIENT_EVIDENCE_SLOT_GEOMETRY_CONTRACT_VERSION,
    layout_requires_slot_geometry,
    validate_slot_geometry_receipt,
    TEMPLATE_OCR_EVIDENCE_CONTRACT_VERSION,
    TEMPLATE_OCR_EVIDENCE_KIND,
    candidate_artifact_path,
    cleanup_candidate_artifacts,
    normalize_homography,
    validate_artifact_manifest,
    validate_uploaded_artifact_file,
)
from services.template_canonicalization_contract import (
    CANONICALIZATION_STAGE,
    CANONICAL_TEMPLATE_CONTRACT_VERSION,
    validate_canonicalization_metadata,
)
from services.accelerator_jobs_base import (
    ALGORITHM_VERSION,
    LEASE_SECONDS,
    MAX_ATTEMPTS,
    NODE_STALE_SECONDS,
    JOB_DEADLINE_SECONDS,
    NODE_MAX_ACTIVE_LEASES,
    CPU_FALLBACK_STALE_SECONDS,
    TEMPLATE_OCR_EVIDENCE_STAGE,
    STUDENT_MULTI_EVIDENCE_STAGE,
    VISION_MODEL_STAGE,
    VISION_MODEL_REQUEST_CONTRACT_VERSION,
    VISION_MODEL_RESPONSE_CONTRACT_VERSION,
    utcnow,
    canonical_json,
    sha256_file,
    sha256_json,
)
from services.accelerator_jobs_identity import (
    TEMPLATE_OCR_IDENTITY_VERSION,
    TEMPLATE_OCR_CACHE_DIR_NAME,
    _IDENTIFIER_RE,
    _SHA256_RE,
    _TEMPLATE_OCR_IDENTITY_RE,
    _MAX_MODEL_PROMPT_BYTES,
    _MAX_MODEL_ASSETS,
    _MODEL_PROVIDER_PROFILES,
    normalize_template_ocr_question_schema,
    template_ocr_identity_payload,
    template_ocr_identity,
    template_ocr_cache_root,
    template_ocr_cache_path,
    template_ocr_layout_identity,
    _is_sha256,
    _normalize_vision_model_layout,
)
from services.accelerator_jobs_nodes import (
    _STAGE_ARTIFACT_CONTRACTS,
    _SUPPORTED_ARTIFACT_CONTRACTS,
    _ALLOWED_STAGES,
    _ALLOWED_LANES,
    _hash_secret,
    _node_capabilities,
    node_hardware_ready,
    _node_health,
    _node_public,
    register_node,
    _node_job_lanes,
    _node_can_access_job,
    node_supported_stages,
    node_supports_stage,
    node_supported_artifact_contracts,
    node_supports_artifact_contract,
    _node_slot_geometry_match,
    validate_node_slot_geometry,
    node_leaseable_stages,
    node_supported_provider_profiles,
    node_supported_provider_models,
    list_nodes,
    list_teacher_desktop_nodes,
    _requeue_node_leases,
    rotate_node_token,
    revoke_node,
    authenticate_node,
    heartbeat,
)
from services.accelerator_jobs_queue import (
    _OFFLOAD_ONLY_STAGES,
    _RETRYABLE_OFFLOAD_ERROR_CODES,
    _RETRYABLE_VISION_RESUME_ERROR_CODES,
    _CONTRACT_TERMINAL_STATUSES,
    enqueue_job,
    is_retryable_offload_terminal,
    resume_offload_job,
    _fallback_job,
    expire_leases,
)

logger = logging.getLogger(__name__)

_IMAGE_PROCESSING_STAGES = frozenset(_OFFLOAD_ONLY_STAGES - {VISION_MODEL_STAGE})
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


def _scope_filters(node: AcceleratorNode) -> tuple:
    if node.access_scope == "platform":
        return ()
    if node.access_scope == "tenant":
        return (AcceleratorJob.teacher_id == node.owner_user_id,)
    return (AcceleratorJob.id == "",)


def _node_lease_capacity(node: AcceleratorNode) -> int:
    """Read total lease capacity separately from currently free slots."""

    try:
        capabilities = json.loads(node.capabilities_json or "{}")
        declared = capabilities.get("lease_slots", node.free_slots)
        if isinstance(declared, bool):
            raise ValueError
        capacity = int(declared)
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        capacity = int(node.free_slots or 0)
    return max(0, min(capacity, NODE_MAX_ACTIVE_LEASES))


def _vision_provider_match(table, provider_models: dict[str, str]):
    """Build an exact ``profile + model`` predicate for a jobs table alias."""

    pairs = [
        (table.c.template_id == profile)
        & (func.json_extract(table.c.layout_snapshot_json, "$.model") == model)
        for profile, model in sorted(provider_models.items())
    ]
    return or_(*pairs) if pairs else false()


def _node_stage_slot_groups(
    node: AcceleratorNode,
    supported_stages: frozenset[str],
) -> tuple[tuple[frozenset[str], int], ...]:
    try:
        capabilities = json.loads(node.capabilities_json or "{}")
    except (TypeError, json.JSONDecodeError):
        capabilities = {}
    raw_groups = capabilities.get("stage_slots")
    groups: list[tuple[frozenset[str], int]] = []
    if isinstance(raw_groups, dict):
        for raw_group in raw_groups.values():
            if not isinstance(raw_group, dict):
                continue
            raw_stages = raw_group.get("stages")
            capacity = raw_group.get("capacity")
            if (
                not isinstance(raw_stages, list)
                or isinstance(capacity, bool)
                or not isinstance(capacity, int)
                or capacity < 1
            ):
                continue
            stages = frozenset(raw_stages) & supported_stages
            if stages:
                groups.append((stages, min(capacity, NODE_MAX_ACTIVE_LEASES)))
    if groups:
        return tuple(groups)
    return ((supported_stages, _node_lease_capacity(node)),)


def _platform_fair_teacher_id(
    node: AcceleratorNode,
    now: datetime,
    supported_stages: frozenset[str],
    provider_profiles: frozenset[str],
    provider_models: dict[str, str] | None = None,
):
    """Select the least-loaded queued teacher inside the lease CAS statement."""

    jobs = AcceleratorJob.__table__
    job_lanes = _node_job_lanes(node)
    queued = jobs.alias("accelerator_fair_queued")
    leased = jobs.alias("accelerator_fair_leased")
    vision_provider_match_queued = _vision_provider_match(
        queued,
        provider_models or {},
    )
    vision_provider_match_leased = _vision_provider_match(
        leased,
        provider_models or {},
    )
    queued_teachers = (
        select(
            queued.c.teacher_id.label("teacher_id"),
            func.min(queued.c.created_at).label("first_queued_at"),
        )
        .where(
            queued.c.status == "queued",
            queued.c.lane.in_(job_lanes),
            queued.c.algorithm_version == node.algorithm_version,
            queued.c.stage.in_(supported_stages),
            _node_slot_geometry_match(queued, node),
            or_(
                queued.c.stage != VISION_MODEL_STAGE,
                vision_provider_match_queued,
            ),
            queued.c.deadline_at > now,
        )
        .group_by(queued.c.teacher_id)
        .subquery("accelerator_fair_queued_teachers")
    )
    live_counts = (
        select(
            leased.c.teacher_id.label("teacher_id"),
            func.count(leased.c.id).label("live_count"),
        )
        .where(
            leased.c.status == "leased",
            leased.c.lane.in_(job_lanes),
            leased.c.algorithm_version == node.algorithm_version,
            leased.c.stage.in_(supported_stages),
            or_(
                leased.c.stage != VISION_MODEL_STAGE,
                vision_provider_match_leased,
            ),
            leased.c.lease_expires_at > now,
            leased.c.deadline_at > now,
        )
        .group_by(leased.c.teacher_id)
        .subquery("accelerator_fair_live_counts")
    )
    return (
        select(queued_teachers.c.teacher_id)
        .select_from(
            queued_teachers.outerjoin(
                live_counts,
                live_counts.c.teacher_id == queued_teachers.c.teacher_id,
            )
        )
        .order_by(
            func.coalesce(live_counts.c.live_count, 0),
            queued_teachers.c.first_queued_at,
            queued_teachers.c.teacher_id,
        )
        .limit(1)
        .scalar_subquery()
    )


def lease_next_job(
    db: Session,
    node: AcceleratorNode,
    requested_stages: list[str] | tuple[str, ...] | None = None,
) -> tuple[AcceleratorJob, str] | None:
    now = utcnow()
    expire_leases(db, now)
    supported_stages = node_leaseable_stages(node)
    if requested_stages is not None:
        if (
            not isinstance(requested_stages, (list, tuple))
            or not requested_stages
            or len(requested_stages) > 32
            or any(
                not isinstance(stage, str) or stage not in _ALLOWED_STAGES
                for stage in requested_stages
            )
        ):
            raise ValueError("requested accelerator stages are invalid")
        supported_stages &= frozenset(requested_stages)
    provider_profiles = node_supported_provider_profiles(node)
    provider_models = node_supported_provider_models(node)
    if VISION_MODEL_STAGE in supported_stages:
        # Vision leases require an exact local profile-to-model advertisement.
        # Nodes from before this capability existed must never receive paid
        # model work, even if they still advertise a profile name.
        if not provider_models:
            supported_stages -= {VISION_MODEL_STAGE}
        else:
            provider_profiles = frozenset(provider_models)
    job_lanes = _node_job_lanes(node)
    if (
        node.algorithm_version != ALGORITHM_VERSION
        or node.free_slots <= 0
        or (
            not node_hardware_ready(node)
            and bool(supported_stages & _IMAGE_PROCESSING_STAGES)
        )
    ):
        return None
    if not supported_stages:
        return None
    if not node.last_seen_at or node.last_seen_at < now - timedelta(seconds=NODE_STALE_SECONDS):
        return None
    live_leases = db.query(AcceleratorJob.id).filter(
        AcceleratorJob.status == "leased",
        AcceleratorJob.assigned_node_id == node.id,
        AcceleratorJob.lease_expires_at > now,
        AcceleratorJob.deadline_at > now,
    ).count()
    if live_leases >= _node_lease_capacity(node):
        return None
    stage_capacity: set[str] = set()
    for grouped_stages, capacity in _node_stage_slot_groups(node, supported_stages):
        live_for_group = db.query(AcceleratorJob.id).filter(
            AcceleratorJob.status == "leased",
            AcceleratorJob.assigned_node_id == node.id,
            AcceleratorJob.stage.in_(grouped_stages),
            AcceleratorJob.lease_expires_at > now,
            AcceleratorJob.deadline_at > now,
        ).count()
        if live_for_group < capacity:
            stage_capacity.update(grouped_stages)
    supported_stages &= frozenset(stage_capacity)
    if not supported_stages:
        return None
    hardware_ready_json = func.json_extract(
        AcceleratorNode.capabilities_json,
        "$.hardware_ready",
    )
    hardware_ready_camel_json = func.json_extract(
        AcceleratorNode.capabilities_json,
        "$.hardwareReady",
    )
    hardware_ready_sql = or_(
        hardware_ready_json == 1,
        hardware_ready_camel_json == 1,
        and_(hardware_ready_json.is_(None), hardware_ready_camel_json.is_(None)),
    )
    node_is_eligible = exists().where(
        AcceleratorNode.id == node.id,
        AcceleratorNode.enabled.is_(True),
        AcceleratorNode.revoked_at.is_(None),
        AcceleratorNode.token_hash == node.token_hash,
        AcceleratorNode.capabilities_json == node.capabilities_json,
        or_(
            AcceleratorNode.token_expires_at.is_(None),
            AcceleratorNode.token_expires_at > now,
        ),
        AcceleratorNode.algorithm_version == node.algorithm_version,
        AcceleratorNode.free_slots > 0,
        AcceleratorNode.last_seen_at.is_not(None),
        AcceleratorNode.last_seen_at >= now - timedelta(seconds=NODE_STALE_SECONDS),
        hardware_ready_sql,
    )
    for _attempt in range(8):
        candidate = AcceleratorJob.__table__.alias("accelerator_lease_candidate")
        candidate_filters = [
            candidate.c.status == "queued",
            candidate.c.lane.in_(job_lanes),
            candidate.c.algorithm_version == node.algorithm_version,
            candidate.c.stage.in_(supported_stages),
            _node_slot_geometry_match(candidate, node),
            candidate.c.deadline_at > now,
        ]
        if VISION_MODEL_STAGE in supported_stages:
            candidate_filters.append(or_(
                candidate.c.stage != VISION_MODEL_STAGE,
                _vision_provider_match(candidate, provider_models),
            ))
        fair_teacher_id = None
        if node.access_scope == "platform":
            fair_teacher_id = _platform_fair_teacher_id(
                node,
                now,
                supported_stages,
                provider_profiles,
                provider_models,
            )
            candidate_filters.append(candidate.c.teacher_id == fair_teacher_id)
        elif node.access_scope == "tenant":
            candidate_filters.append(candidate.c.teacher_id == node.owner_user_id)
        else:
            candidate_filters.append(candidate.c.id == "")
        candidate_id = (
            select(candidate.c.id)
            .where(*candidate_filters)
            .order_by(candidate.c.created_at.asc(), candidate.c.id.asc())
            .limit(1)
            .scalar_subquery()
        )
        lease_secret = secrets.token_urlsafe(32)
        lease_hash = _hash_secret(lease_secret)
        changed = db.execute(
            update(AcceleratorJob)
            .where(
                AcceleratorJob.id == candidate_id,
                AcceleratorJob.status == "queued",
                AcceleratorJob.lane.in_(job_lanes),
                AcceleratorJob.algorithm_version == node.algorithm_version,
                AcceleratorJob.stage.in_(supported_stages),
                _node_slot_geometry_match(AcceleratorJob.__table__, node),
                or_(
                    AcceleratorJob.stage != VISION_MODEL_STAGE,
                    _vision_provider_match(
                        AcceleratorJob.__table__,
                        provider_models,
                    ),
                ),
                *_scope_filters(node),
                AcceleratorJob.deadline_at > now,
                node_is_eligible,
            )
            .values(
                status="leased",
                assigned_node_id=node.id,
                lease_id_hash=lease_hash,
                lease_expires_at=now + timedelta(seconds=LEASE_SECONDS),
                attempt_count=AcceleratorJob.attempt_count + 1,
                error_code=None,
            )
        ).rowcount
        db.commit()
        if changed == 1:
            job = (
                db.query(AcceleratorJob)
                .execution_options(populate_existing=True)
                .filter(
                    AcceleratorJob.assigned_node_id == node.id,
                    AcceleratorJob.lease_id_hash == lease_hash,
                )
                .one()
            )
            # A previous expired/requeued attempt may have left partial files.
            # Remove them only after this claim succeeds and before exposing the
            # fresh lease, so cleanup cannot race a new worker upload.
            cleanup_candidate_artifacts(job.source_path, job.id)
            return job, lease_secret
    return None


def _lease_matches(job: AcceleratorJob, node: AcceleratorNode, lease_secret: str) -> bool:
    return bool(
        job.assigned_node_id == node.id
        and job.lease_id_hash
        and hmac.compare_digest(job.lease_id_hash, _hash_secret(lease_secret or ""))
    )


def _owned_live_lease(db: Session, node: AcceleratorNode, job_id: str, lease_secret: str) -> AcceleratorJob:
    job = (
        db.query(AcceleratorJob)
        .execution_options(populate_existing=True)
        .filter(AcceleratorJob.id == job_id)
        .first()
    )
    now = utcnow()
    status_ok = bool(job and job.status == "leased")
    access_ok = bool(job and _node_can_access_job(node, job))
    lease_ok = bool(job and _lease_matches(job, node, lease_secret))
    lease_time_ok = bool(job and job.lease_expires_at and job.lease_expires_at > now)
    deadline_ok = bool(job and job.deadline_at and job.deadline_at > now)
    if not all((status_ok, access_ok, lease_ok, lease_time_ok, deadline_ok)):
        logger.warning(
            "Accelerator live lease validation failed job_present=%s "
            "status_ok=%s access_ok=%s lease_ok=%s lease_time_ok=%s deadline_ok=%s",
            job is not None,
            status_ok,
            access_ok,
            lease_ok,
            lease_time_ok,
            deadline_ok,
        )
        raise PermissionError("invalid or expired lease")
    return job


def renew_lease(db: Session, node: AcceleratorNode, job_id: str, lease_secret: str) -> AcceleratorJob:
    now = utcnow()
    job = (
        db.query(AcceleratorJob)
        .execution_options(populate_existing=True)
        .filter(AcceleratorJob.id == job_id)
        .first()
    )
    if not job:
        raise PermissionError("invalid or expired lease")
    new_expiry = min(now + timedelta(seconds=LEASE_SECONDS), job.deadline_at)
    changed = db.execute(
        update(AcceleratorJob)
        .where(
            AcceleratorJob.id == job_id,
            AcceleratorJob.status == "leased",
            AcceleratorJob.assigned_node_id == node.id,
            *_scope_filters(node),
            AcceleratorJob.lease_id_hash == _hash_secret(lease_secret or ""),
            AcceleratorJob.lease_expires_at > now,
            AcceleratorJob.deadline_at > now,
        )
        .values(lease_expires_at=new_expiry)
    ).rowcount
    if changed != 1:
        db.rollback()
        # SQLite can report zero rows when another worker renewed the same
        # lease between the read and this conditional UPDATE.  Re-read after
        # rolling back the failed transaction; accept the lease only if all
        # ownership, hash, status, and deadline checks still pass.
        try:
            return _owned_live_lease(db, node, job_id, lease_secret)
        except PermissionError:
            raise PermissionError("invalid or expired lease")
    db.commit()
    return (
        db.query(AcceleratorJob)
        .execution_options(populate_existing=True)
        .filter(AcceleratorJob.id == job_id)
        .one()
    )


def verify_job_asset(job: AcceleratorJob, kind: str) -> Path:
    if job.stage == VISION_MODEL_STAGE:
        try:
            layout = json.loads(job.layout_snapshot_json or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("vision model request metadata is invalid") from exc
        assets = layout.get("assets")
        if not isinstance(assets, list):
            raise ValueError("vision model assets are invalid")
        item = next(
            (
                value
                for value in assets
                if isinstance(value, dict) and value.get("kind") == kind
            ),
            None,
        )
        if not isinstance(item, dict):
            raise ValueError("invalid asset kind")
        path = (Path(job.source_path).resolve().parent / f"{kind}.bin").resolve()
        if (
            path.parent != Path(job.source_path).resolve().parent
            or not path.is_file()
            or path.stat().st_size != int(item.get("byte_size") or 0)
            or sha256_file(path) != item.get("sha256")
        ):
            raise ValueError("job asset changed or is missing")
        return path
    if kind == "source":
        path, expected = Path(job.source_path), job.source_sha256
    elif kind == "template" and job.stage != CANONICALIZATION_STAGE:
        path, expected = Path(job.template_path), job.template_sha256
    else:
        raise ValueError("invalid asset kind")
    resolved = path.resolve()
    if not resolved.is_file() or sha256_file(resolved) != expected:
        raise ValueError("job asset changed or is missing")
    return resolved


def allowed_artifact_kinds_for_stage(stage: str) -> frozenset[str]:
    if stage == CANONICALIZATION_STAGE:
        return frozenset({"canonical_template"})
    if stage == "template_registration":
        return frozenset({"evidence"})
    if stage == STUDENT_MULTI_EVIDENCE_STAGE:
        return frozenset(f"evidence_{position:02d}" for position in range(1, 13))
    if stage == TEMPLATE_OCR_EVIDENCE_STAGE:
        return frozenset({TEMPLATE_OCR_EVIDENCE_KIND})
    if stage == VISION_MODEL_STAGE:
        return frozenset()
    return frozenset()


def authorize_candidate_artifact_upload(
    db: Session,
    node: AcceleratorNode,
    *,
    job_id: str,
    lease_secret: str,
    kind: str,
) -> tuple[AcceleratorJob, Path]:
    """Renew and authorize one artifact upload without persisting its bytes."""

    job = renew_lease(db, node, job_id, lease_secret)
    if kind not in allowed_artifact_kinds_for_stage(job.stage):
        raise ValueError("candidate artifact kind is invalid for job stage")
    path = candidate_artifact_path(job.source_path, job.id, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    return job, path


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


def submit_result(
    db: Session,
    node: AcceleratorNode,
    *,
    job_id: str,
    lease_secret: str,
    result: dict[str, Any],
) -> AcceleratorJob:
    job = (
        db.query(AcceleratorJob)
        .execution_options(populate_existing=True)
        .filter(AcceleratorJob.id == job_id)
        .first()
    )
    if not job:
        raise PermissionError("invalid or expired lease")
    layout = json.loads(job.layout_snapshot_json or "{}")
    validate_node_slot_geometry(node, job.stage, layout)
    payload = _validate_candidate_payload(
        result,
        stage=job.stage,
        source_sha256=job.source_sha256,
        layout_snapshot_sha256=job.layout_snapshot_sha256,
        layout_snapshot=layout,
    )
    candidate_json = canonical_json(payload)
    candidate_sha = hashlib.sha256(candidate_json.encode("utf-8")).hexdigest()
    if job and job.status in {
        "candidate_received", "server_validating", "validated", "consumed", "superseded",
    }:
        if not _node_can_access_job(node, job) or not _lease_matches(job, node, lease_secret):
            raise PermissionError("invalid or foreign completed lease")
        if job.candidate_sha256 != candidate_sha:
            raise ValueError("candidate conflicts with the previously received result")
        return job
    now = utcnow()
    values: dict[Any, Any] = {
        AcceleratorJob.candidate_json: candidate_json,
        AcceleratorJob.candidate_sha256: candidate_sha,
        AcceleratorJob.candidate_received_at: now,
        AcceleratorJob.lease_expires_at: None,
    }
    if job.stage == VISION_MODEL_STAGE:
        values.update({
            AcceleratorJob.status: "validated",
            AcceleratorJob.server_validation_status: "accepted",
            AcceleratorJob.validated_at: now,
        })
    else:
        values.update({
            AcceleratorJob.status: "candidate_received",
            AcceleratorJob.server_validation_status: "pending",
        })
    changed = db.execute(
        update(AcceleratorJob)
        .where(
            AcceleratorJob.id == job_id,
            AcceleratorJob.status == "leased",
            AcceleratorJob.assigned_node_id == node.id,
            *_scope_filters(node),
            AcceleratorJob.lease_id_hash == _hash_secret(lease_secret or ""),
            AcceleratorJob.lease_expires_at > now,
            AcceleratorJob.deadline_at > now,
        )
        .values(values)
    ).rowcount
    if changed != 1:
        db.rollback()
        raise PermissionError("invalid or expired lease")
    db.commit()
    return (
        db.query(AcceleratorJob)
        .execution_options(populate_existing=True)
        .filter(AcceleratorJob.id == job_id)
        .one()
    )


def begin_server_validation(db: Session, job_id: str) -> AcceleratorJob:
    changed = db.execute(
        update(AcceleratorJob)
        .where(
            AcceleratorJob.id == job_id,
            AcceleratorJob.status == "candidate_received",
        )
        .values(
            status="server_validating",
            server_validation_status="running",
            updated_at=utcnow(),
        )
    ).rowcount
    if changed != 1:
        db.rollback()
        raise ValueError("job is not ready for server validation")
    db.commit()
    return (
        db.query(AcceleratorJob)
        .execution_options(populate_existing=True)
        .filter(AcceleratorJob.id == job_id)
        .one()
    )


def finish_server_validation(
    db: Session,
    job_id: str,
    *,
    accepted: bool,
    validation: dict[str, Any],
    artifact_path: str | None = None,
    artifact_sha256: str | None = None,
) -> AcceleratorJob:
    values: dict[Any, Any] = {
        AcceleratorJob.server_validation_json: canonical_json(validation or {}),
        AcceleratorJob.updated_at: utcnow(),
    }
    if accepted:
        values.update({
            AcceleratorJob.status: "validated",
            AcceleratorJob.server_validation_status: "accepted",
            AcceleratorJob.validated_at: utcnow(),
            AcceleratorJob.error_code: None,
            AcceleratorJob.server_artifact_path: artifact_path,
            AcceleratorJob.server_artifact_sha256: artifact_sha256,
        })
    else:
        now = utcnow()
        rejected_stage = db.query(AcceleratorJob.stage).filter(
            AcceleratorJob.id == job_id,
            AcceleratorJob.status == "server_validating",
        ).scalar()
        rejected_status = (
            "fallback_failed"
            if rejected_stage in _OFFLOAD_ONLY_STAGES
            else "server_fallback"
        )
        values.update({
            AcceleratorJob.status: rejected_status,
            AcceleratorJob.error_code: "server_validation_failed",
            AcceleratorJob.fallback_at: now,
            AcceleratorJob.assigned_node_id: None,
            AcceleratorJob.lease_id_hash: None,
            AcceleratorJob.lease_expires_at: None,
            AcceleratorJob.server_validation_status: "rejected",
            AcceleratorJob.server_artifact_path: None,
            AcceleratorJob.server_artifact_sha256: None,
        })
    changed = db.execute(
        update(AcceleratorJob)
        .where(
            AcceleratorJob.id == job_id,
            AcceleratorJob.status == "server_validating",
        )
        .values(values)
    ).rowcount
    if changed != 1:
        db.rollback()
        job = (
            db.query(AcceleratorJob)
            .execution_options(populate_existing=True)
            .filter(AcceleratorJob.id == job_id)
            .first()
        )
        if not job:
            raise ValueError("job is not being server validated")
        return job
    db.commit()
    return (
        db.query(AcceleratorJob)
        .execution_options(populate_existing=True)
        .filter(AcceleratorJob.id == job_id)
        .one()
    )


def fail_job(
    db: Session,
    node: AcceleratorNode,
    *,
    job_id: str,
    lease_secret: str,
    error_code: str,
) -> AcceleratorJob:
    job = _owned_live_lease(db, node, job_id, lease_secret)
    cleanup_candidate_artifacts(job.source_path, job.id)
    job.error_code = str(error_code or "worker_failed")[:64]
    if job.stage == VISION_MODEL_STAGE:
        # A provider request may have left the node before its failure became
        # observable. Never lease the same billable attempt twice; the server
        # OCR scheduler owns the single bounded cross-pool retry instead.
        _fallback_job(job, job.error_code, utcnow())
    elif job.attempt_count >= MAX_ATTEMPTS or job.deadline_at <= utcnow():
        _fallback_job(job, job.error_code, utcnow())
    else:
        job.status = "queued"
        job.assigned_node_id = None
        job.lease_expires_at = None
        job.lease_id_hash = None
    db.commit()
    return job


def job_public(job: AcceleratorJob) -> dict[str, Any]:
    return {
        "job_id": job.id,
        "batch_id": job.batch_id,
        "paper_id": job.paper_id,
        "teacher_id": job.teacher_id,
        "source_sha256": job.source_sha256,
        "template_id": job.template_id,
        "template_sha256": job.template_sha256,
        "layout_snapshot_sha256": job.layout_snapshot_sha256,
        "stage": job.stage,
        "lane": job.lane,
        "algorithm_version": job.algorithm_version,
        "status": job.status,
        "attempt_count": job.attempt_count,
        "error_code": job.error_code,
        "lease_expires_at": job.lease_expires_at.isoformat() if job.lease_expires_at else None,
        "deadline_at": job.deadline_at.isoformat() if job.deadline_at else None,
        "server_validation_status": job.server_validation_status,
        "candidate": json.loads(job.candidate_json) if job.candidate_json else None,
        "server_validation": json.loads(job.server_validation_json) if job.server_validation_json else None,
        "server_artifact_ready": bool(job.server_artifact_path),
        "consumed_at": job.consumed_at.isoformat() if job.consumed_at else None,
    }
