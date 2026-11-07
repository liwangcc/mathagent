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

logger = logging.getLogger(__name__)

_STAGE_ARTIFACT_CONTRACTS = {
    "template_registration": CLIENT_EVIDENCE_CONTRACT_VERSION,
    STUDENT_MULTI_EVIDENCE_STAGE: CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION,
    CANONICALIZATION_STAGE: CANONICAL_TEMPLATE_CONTRACT_VERSION,
    TEMPLATE_OCR_EVIDENCE_STAGE: TEMPLATE_OCR_EVIDENCE_CONTRACT_VERSION,
    VISION_MODEL_STAGE: VISION_MODEL_RESPONSE_CONTRACT_VERSION,
}
_SUPPORTED_ARTIFACT_CONTRACTS = set(_STAGE_ARTIFACT_CONTRACTS.values()) | {
    CLIENT_EVIDENCE_SLOT_GEOMETRY_CONTRACT_VERSION,
    # Historical student leases may still advertise the focus-sheet contract.
    CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION,
}
_ALLOWED_STAGES = set(_STAGE_ARTIFACT_CONTRACTS)
_OFFLOAD_ONLY_STAGES = {
    "template_registration",
    STUDENT_MULTI_EVIDENCE_STAGE,
    CANONICALIZATION_STAGE,
    TEMPLATE_OCR_EVIDENCE_STAGE,
    VISION_MODEL_STAGE,
}
_IMAGE_PROCESSING_STAGES = frozenset(_OFFLOAD_ONLY_STAGES - {VISION_MODEL_STAGE})
_RETRYABLE_OFFLOAD_ERROR_CODES = frozenset({
    "job_deadline_exceeded",
    "lease_attempts_exhausted",
    "server_validation_deadline_exceeded",
})
_RETRYABLE_VISION_RESUME_ERROR_CODES = frozenset({
    # These failures happen before a provider dispatch can become ambiguous.
    # Provider/lease failures remain terminal and are retried only by the OCR
    # scheduler through a fresh cross-pool request.
    "job_deadline_exceeded",
    "server_validation_deadline_exceeded",
})
_CONTRACT_TERMINAL_STATUSES = frozenset({
    "server_fallback",
    "fallback_running",
    "fallback_completed",
    "fallback_failed",
    "consumed",
    "superseded",
})
_ALLOWED_LANES = {"web_gpu", "teacher_desktop"}
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


def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _node_capabilities(node: AcceleratorNode) -> dict[str, Any]:
    """Return the last advertised capability document, failing closed."""

    try:
        value = json.loads(node.capabilities_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def node_hardware_ready(node: AcceleratorNode) -> bool:
    """Whether the last heartbeat allows this node to accept image work.

    ``hardware_ready`` is required for current workers.  Heartbeats from the
    original protocol did not carry the field, so their existing stage and
    slot declarations remain the compatibility signal.  An explicit false is
    always authoritative and prevents a lease even if stale slot counts were
    sent accidentally.
    """

    capabilities = _node_capabilities(node)
    snake_present = "hardware_ready" in capabilities
    camel_present = "hardwareReady" in capabilities
    if not snake_present and not camel_present:
        # Nodes from protocol v1 had no hardware field.  Their stage/slot
        # declarations remain the compatibility signal until they heartbeat.
        return True
    snake = capabilities.get("hardware_ready")
    camel = capabilities.get("hardwareReady")
    if snake_present and not isinstance(snake, bool):
        return False
    if camel_present and not isinstance(camel, bool):
        return False
    if snake_present and camel_present and snake != camel:
        return False
    return snake if snake_present else camel


def _node_health(node: AcceleratorNode, *, now: datetime | None = None) -> tuple[str, bool]:
    """Derive current health from authoritative registration and heartbeat state."""

    current = now or utcnow()
    if node.revoked_at is not None or not node.enabled:
        return "revoked", False
    if node.token_expires_at is not None and node.token_expires_at <= current:
        return "credential_expired", False
    if node.last_seen_at is None:
        return "never_seen", False
    if node.last_seen_at < current - timedelta(seconds=NODE_STALE_SECONDS):
        return "stale", False
    if node.algorithm_version != ALGORITHM_VERSION:
        return "version_mismatch", False
    if not node_hardware_ready(node):
        return "hardware_unavailable", False
    if not node_leaseable_stages(node):
        return "capability_mismatch", False
    if node.free_slots <= 0:
        return "busy", True
    return "ready", True


def _node_public(node: AcceleratorNode) -> dict[str, Any]:
    health_state, healthy = _node_health(node)
    return {
        "node_id": node.id,
        "owner_user_id": node.owner_user_id,
        "access_scope": node.access_scope,
        "lane": node.lane,
        "device_id": node.device_id,
        "name": node.name,
        "enabled": bool(node.enabled),
        "algorithm_version": node.algorithm_version,
        "free_slots": node.free_slots,
        "queue_depth": node.queue_depth,
        "estimated_finish_ms": node.estimated_finish_ms,
        "last_error_code": node.last_error_code,
        "last_seen_at": node.last_seen_at.isoformat() if node.last_seen_at else None,
        "revoked_at": node.revoked_at.isoformat() if node.revoked_at else None,
        "token_expires_at": node.token_expires_at.isoformat() if node.token_expires_at else None,
        "health_state": health_state,
        "healthy": healthy,
        "lease_eligible": health_state == "ready",
        "hardware_ready": node_hardware_ready(node),
        "hardwareReady": node_hardware_ready(node),
        "capabilities": _node_capabilities(node),
    }


def register_node(
    db: Session,
    *,
    owner_user_id: int,
    name: str,
    access_scope: str = "tenant",
    lane: str = "web_gpu",
    device_id: str | None = None,
    token_ttl_days: int | None = None,
) -> tuple[AcceleratorNode, str]:
    if access_scope not in {"tenant", "platform"}:
        raise ValueError("invalid accelerator node access scope")
    if lane not in _ALLOWED_LANES:
        raise ValueError("invalid accelerator node lane")
    if lane == "teacher_desktop" and access_scope != "tenant":
        raise ValueError("teacher desktop nodes must be tenant scoped")
    normalized_device_id = str(device_id or "").strip() or None
    if lane == "teacher_desktop" and not (
        normalized_device_id and _IDENTIFIER_RE.fullmatch(normalized_device_id)
    ):
        raise ValueError("teacher desktop device_id is invalid")
    if token_ttl_days is not None and not 1 <= int(token_ttl_days) <= 365:
        raise ValueError("accelerator token lifetime is invalid")
    if normalized_device_id and db.query(AcceleratorNode.id).filter(
        AcceleratorNode.owner_user_id == owner_user_id,
        AcceleratorNode.device_id == normalized_device_id,
        AcceleratorNode.lane == lane,
    ).first():
        raise ValueError("accelerator device is already registered")
    token = secrets.token_urlsafe(32)
    now = utcnow()
    node = AcceleratorNode(
        id=uuid.uuid4().hex,
        owner_user_id=owner_user_id,
        access_scope=access_scope,
        lane=lane,
        device_id=normalized_device_id,
        name=(name or "MathAgent Accelerator")[:100],
        token_hash=_hash_secret(token),
        enabled=1,
        capabilities_json="{}",
        algorithm_version=ALGORITHM_VERSION,
        free_slots=0,
        queue_depth=0,
        token_expires_at=(
            now + timedelta(days=int(token_ttl_days))
            if token_ttl_days is not None else None
        ),
        token_rotated_at=now,
    )
    db.add(node)
    db.commit()
    db.refresh(node)
    return node, token


def _node_job_lanes(node: AcceleratorNode) -> frozenset[str]:
    if node.access_scope == "platform":
        return frozenset(_ALLOWED_LANES)
    if node.lane == "teacher_desktop" and node.access_scope == "tenant":
        return frozenset({"teacher_desktop", "web_gpu"})
    return frozenset({node.lane}) if node.lane in _ALLOWED_LANES else frozenset()


def _node_can_access_job(node: AcceleratorNode, job: AcceleratorJob) -> bool:
    """Tenant nodes are owner-bound; explicit admin-created platform nodes are global."""
    return bool(
        job.lane in _node_job_lanes(node)
        and (
            node.access_scope == "platform"
            or (
                node.access_scope == "tenant"
                and int(node.owner_user_id) == int(job.teacher_id)
            )
        )
    )


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


def node_supported_stages(node: AcceleratorNode) -> frozenset[str]:
    """Return stages the node may lease from its last heartbeat.

    Stage declarations were added after the original template-registration
    protocol shipped.  An omitted declaration therefore retains compatibility
    only with that original stage; an explicit empty or different list is
    fail-closed.
    """

    try:
        capabilities = json.loads(node.capabilities_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return frozenset()
    raw_stages = capabilities.get("stages")
    if raw_stages is None:
        return (
            frozenset({"template_registration"})
            if node.algorithm_version == ALGORITHM_VERSION
            else frozenset()
        )
    if not isinstance(raw_stages, list):
        return frozenset()
    return frozenset(
        stage
        for stage in raw_stages
        if isinstance(stage, str) and stage in _ALLOWED_STAGES
    )


def node_supports_stage(node: AcceleratorNode, stage: str) -> bool:
    return stage in node_supported_stages(node)


def node_supported_artifact_contracts(node: AcceleratorNode) -> frozenset[str]:
    """Return explicitly advertised artifact contracts understood by the server."""

    try:
        capabilities = json.loads(node.capabilities_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return frozenset()
    raw_contracts = capabilities.get("artifact_contracts")
    if not isinstance(raw_contracts, list):
        return frozenset()
    return frozenset(
        contract
        for contract in raw_contracts
        if contract in _SUPPORTED_ARTIFACT_CONTRACTS
    )


def node_supports_artifact_contract(
    node: AcceleratorNode,
    contract: str,
) -> bool:
    return contract in node_supported_artifact_contracts(node)


def _node_slot_geometry_match(table, node: AcceleratorNode):
    """Apply to fairness, queue selection and the final lease CAS alike."""
    if node_supports_artifact_contract(node, CLIENT_EVIDENCE_SLOT_GEOMETRY_CONTRACT_VERSION):
        return table.c.id.is_not(None)
    slot_fields = []
    for field in ("question_schema", "template_answer_regions"):
        items = func.json_each(table.c.layout_snapshot_json, f"$.{field}").table_valued("value")
        slot_fields.append(exists(select(1).select_from(items).where(
            func.json_type(items.c.value, "$.slot_ids").is_not(None)
        )))
    return or_(table.c.stage != STUDENT_MULTI_EVIDENCE_STAGE, ~or_(*slot_fields))


def validate_node_slot_geometry(node: AcceleratorNode | None, stage: str, layout: dict) -> None:
    if stage == STUDENT_MULTI_EVIDENCE_STAGE and layout_requires_slot_geometry(layout):
        if node is None or not node_supports_artifact_contract(
            node, CLIENT_EVIDENCE_SLOT_GEOMETRY_CONTRACT_VERSION
        ):
            raise ValueError("node lacks slot geometry artifact capability")


def node_leaseable_stages(node: AcceleratorNode) -> frozenset[str]:
    contracts = node_supported_artifact_contracts(node)
    return frozenset(
        stage
        for stage in node_supported_stages(node)
        if (
            _STAGE_ARTIFACT_CONTRACTS.get(stage) in contracts
            or (
                stage == STUDENT_MULTI_EVIDENCE_STAGE
                and CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION in contracts
            )
        )
    )


def node_supported_provider_profiles(node: AcceleratorNode) -> frozenset[str]:
    try:
        capabilities = json.loads(node.capabilities_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return frozenset()
    raw_profiles = capabilities.get("provider_profiles")
    if not isinstance(raw_profiles, list):
        return frozenset()
    return frozenset(
        profile
        for profile in raw_profiles
        if profile in _MODEL_PROVIDER_PROFILES
    )


def node_supported_provider_models(node: AcceleratorNode) -> dict[str, str]:
    """Return the node's public profile-to-model advertisement.

    Provider profile names alone are not sufficient for a vision-model lease:
    a local credential may have been rotated to a different model while the
    profile name stayed the same.  A missing or malformed mapping therefore
    intentionally returns an empty result so vision work fails closed.
    """

    try:
        capabilities = json.loads(node.capabilities_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    raw_models = capabilities.get("provider_models")
    if not isinstance(raw_models, dict) or not 1 <= len(raw_models) <= 32:
        return {}
    normalized: dict[str, str] = {}
    for raw_profile, raw_model in raw_models.items():
        if (
            not isinstance(raw_profile, str)
            or raw_profile not in _MODEL_PROVIDER_PROFILES
            or not isinstance(raw_model, str)
            or not _IDENTIFIER_RE.fullmatch(raw_model)
        ):
            return {}
        normalized[raw_profile] = raw_model
    declared_profiles = capabilities.get("provider_profiles")
    if (
        not isinstance(declared_profiles, list)
        or set(declared_profiles) != set(normalized)
    ):
        return {}
    return {profile: normalized[profile] for profile in sorted(normalized)}


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


def list_nodes(db: Session) -> list[dict[str, Any]]:
    return [_node_public(node) for node in db.query(AcceleratorNode).order_by(AcceleratorNode.created_at).all()]


def list_teacher_desktop_nodes(db: Session, owner_user_id: int) -> list[dict[str, Any]]:
    rows = db.query(AcceleratorNode).filter(
        AcceleratorNode.owner_user_id == owner_user_id,
        AcceleratorNode.lane == "teacher_desktop",
    ).order_by(AcceleratorNode.created_at).all()
    return [_node_public(node) for node in rows]


def _requeue_node_leases(db: Session, node_id: str, error_code: str) -> None:
    db.query(AcceleratorJob).filter(
        AcceleratorJob.assigned_node_id == node_id,
        AcceleratorJob.status == "leased",
    ).update(
        {
            AcceleratorJob.status: "queued",
            AcceleratorJob.assigned_node_id: None,
            AcceleratorJob.lease_id_hash: None,
            AcceleratorJob.lease_expires_at: None,
            AcceleratorJob.error_code: error_code,
        },
        synchronize_session=False,
    )


def rotate_node_token(
    db: Session,
    node_id: str,
    *,
    owner_user_id: int,
    token_ttl_days: int = 90,
) -> tuple[AcceleratorNode, str]:
    if not 1 <= int(token_ttl_days) <= 365:
        raise ValueError("accelerator token lifetime is invalid")
    node = db.query(AcceleratorNode).filter(
        AcceleratorNode.id == node_id,
        AcceleratorNode.owner_user_id == owner_user_id,
        AcceleratorNode.lane == "teacher_desktop",
    ).first()
    if not node:
        raise LookupError("teacher desktop device not found")
    if not node.enabled or node.revoked_at is not None:
        raise ValueError("teacher desktop device is revoked")
    token = secrets.token_urlsafe(32)
    now = utcnow()
    node.token_hash = _hash_secret(token)
    node.token_rotated_at = now
    node.token_expires_at = now + timedelta(days=int(token_ttl_days))
    node.free_slots = 0
    _requeue_node_leases(db, node.id, "node_token_rotated_requeued")
    db.commit()
    db.refresh(node)
    return node, token


def revoke_node(db: Session, node_id: str) -> AcceleratorNode:
    node = db.query(AcceleratorNode).filter(AcceleratorNode.id == node_id).first()
    if not node:
        raise LookupError("accelerator node not found")
    node.enabled = 0
    node.free_slots = 0
    node.revoked_at = utcnow()
    node.token_hash = _hash_secret(secrets.token_urlsafe(48))
    _requeue_node_leases(db, node.id, "node_revoked_requeued")
    db.commit()
    db.refresh(node)
    return node


def authenticate_node(db: Session, node_id: str, token: str) -> AcceleratorNode | None:
    if not node_id or not token:
        return None
    node = db.query(AcceleratorNode).filter(AcceleratorNode.id == node_id).first()
    if not node or not node.enabled or node.revoked_at is not None:
        return None
    if node.token_expires_at is not None and node.token_expires_at <= utcnow():
        return None
    if not hmac.compare_digest(node.token_hash, _hash_secret(token)):
        return None
    return node


def heartbeat(
    db: Session,
    node: AcceleratorNode,
    *,
    capabilities: dict[str, Any],
    algorithm_version: str,
    free_slots: int,
    queue_depth: int = 0,
    estimated_finish_ms: int | None = None,
    last_error_code: str | None = None,
) -> dict[str, Any]:
    normalized_capabilities = dict(capabilities or {})
    # Current workers publish an explicit hardware gate.  Accept both spellings
    # during the protocol transition, but reject conflicting or malformed
    # values instead of allowing a stale positive slot count to lease work.
    if "hardware_ready" in normalized_capabilities or "hardwareReady" in normalized_capabilities:
        raw_ready = normalized_capabilities.get("hardware_ready")
        raw_camel = normalized_capabilities.get("hardwareReady")
        if (
            ("hardware_ready" in normalized_capabilities and not isinstance(raw_ready, bool))
            or ("hardwareReady" in normalized_capabilities and not isinstance(raw_camel, bool))
            or (
                "hardware_ready" in normalized_capabilities
                and "hardwareReady" in normalized_capabilities
                and raw_ready != raw_camel
            )
        ):
            raise ValueError("accelerator hardware readiness is invalid")
        normalized_capabilities["hardware_ready"] = (
            raw_ready if "hardware_ready" in normalized_capabilities else raw_camel
        )
        normalized_capabilities.pop("hardwareReady", None)
    for capability_field in ("stages", "artifact_contracts", "provider_profiles"):
        if capability_field not in normalized_capabilities:
            continue
        raw_values = normalized_capabilities[capability_field]
        if not isinstance(raw_values, list) or any(
            not isinstance(value, str)
            or not _IDENTIFIER_RE.fullmatch(value)
            for value in raw_values
        ):
            raise ValueError(
                f"accelerator capability {capability_field} are invalid"
            )
        if len(raw_values) > 32:
            raise ValueError(f"too many accelerator capability {capability_field}")
        if capability_field == "provider_profiles" and any(
            value not in _MODEL_PROVIDER_PROFILES for value in raw_values
        ):
            raise ValueError("accelerator provider profiles are invalid")
        normalized_capabilities[capability_field] = sorted(set(raw_values))
    if "provider_models" in normalized_capabilities:
        raw_models = normalized_capabilities["provider_models"]
        if (
            not isinstance(raw_models, dict)
            or len(raw_models) > 32
            or any(
                not isinstance(profile, str)
                or not _IDENTIFIER_RE.fullmatch(profile)
                or profile not in _MODEL_PROVIDER_PROFILES
                or not isinstance(model, str)
                or not _IDENTIFIER_RE.fullmatch(model)
                for profile, model in raw_models.items()
            )
        ):
            raise ValueError("accelerator provider models are invalid")
        declared_profiles = normalized_capabilities.get("provider_profiles")
        if (
            not isinstance(declared_profiles, list)
            or set(declared_profiles) != set(raw_models)
        ):
            raise ValueError(
                "accelerator provider models must match provider profiles"
            )
        normalized_capabilities["provider_models"] = {
            profile: raw_models[profile]
            for profile in sorted(raw_models)
        }
    if "stage_slots" in normalized_capabilities:
        raw_groups = normalized_capabilities["stage_slots"]
        declared_stages = set(normalized_capabilities.get("stages") or [])
        if not isinstance(raw_groups, dict) or not 1 <= len(raw_groups) <= 8:
            raise ValueError("accelerator stage slots are invalid")
        normalized_groups: dict[str, dict[str, Any]] = {}
        assigned_stages: set[str] = set()
        total_capacity = 0
        for group_name, raw_group in raw_groups.items():
            if (
                not isinstance(group_name, str)
                or not _IDENTIFIER_RE.fullmatch(group_name)
                or not isinstance(raw_group, dict)
                or set(raw_group) != {"stages", "capacity"}
            ):
                raise ValueError("accelerator stage slots are invalid")
            group_stages = raw_group.get("stages")
            capacity = raw_group.get("capacity")
            if (
                not isinstance(group_stages, list)
                or not group_stages
                or any(
                    not isinstance(stage, str)
                    or stage not in declared_stages
                    or stage in assigned_stages
                    for stage in group_stages
                )
                or isinstance(capacity, bool)
                or not isinstance(capacity, int)
                or not 1 <= capacity <= 64
            ):
                raise ValueError("accelerator stage slots are invalid")
            assigned_stages.update(group_stages)
            total_capacity += capacity
            normalized_groups[group_name] = {
                "stages": sorted(set(group_stages)),
                "capacity": capacity,
            }
        if assigned_stages != declared_stages or total_capacity > 64:
            raise ValueError("accelerator stage slots are invalid")
        normalized_capabilities["stage_slots"] = {
            name: normalized_groups[name]
            for name in sorted(normalized_groups)
        }
    node.capabilities_json = canonical_json(normalized_capabilities)
    node.algorithm_version = str(algorithm_version or "")[:64]
    node.free_slots = max(0, min(int(free_slots), 64))
    node.queue_depth = max(0, min(int(queue_depth), 1024))
    node.estimated_finish_ms = (
        max(0, min(int(estimated_finish_ms), 3_600_000))
        if estimated_finish_ms is not None else None
    )
    node.last_error_code = str(last_error_code)[:64] if last_error_code else None
    node.last_seen_at = utcnow()
    db.commit()
    return _node_public(node)


def enqueue_job(
    db: Session,
    *,
    teacher_id: int,
    batch_id: str = "",
    paper_id: str,
    source_path: str,
    template_id: str,
    template_path: str,
    layout_snapshot: dict[str, Any],
    stage: str = "template_registration",
    lane: str = "web_gpu",
    algorithm_version: str = ALGORITHM_VERSION,
    deadline_at: datetime | None = None,
) -> AcceleratorJob:
    if stage not in _ALLOWED_STAGES:
        raise ValueError("unsupported accelerator stage")
    if lane not in _ALLOWED_LANES:
        raise ValueError("unsupported accelerator lane")
    batch_id = str(batch_id or "").strip()
    paper_id = str(paper_id or "").strip()
    template_id = str(template_id or "").strip()
    if batch_id and not _IDENTIFIER_RE.fullmatch(batch_id):
        raise ValueError("batch_id is invalid")
    if not _IDENTIFIER_RE.fullmatch(paper_id) or not _IDENTIFIER_RE.fullmatch(template_id):
        raise ValueError("paper_id or template_id is invalid")
    source = Path(source_path).resolve()
    template = Path(template_path).resolve()
    if not source.is_file() or not template.is_file():
        raise ValueError("source or template file does not exist")
    source_sha = sha256_file(source)
    template_sha = sha256_file(template)
    if not isinstance(layout_snapshot, dict) or not layout_snapshot:
        raise ValueError("layout_snapshot is required")
    if stage == CANONICALIZATION_STAGE:
        operation_id = str(layout_snapshot.get("operation_id") or "").strip()
        if set(layout_snapshot) != {"operation_id"} or not _IDENTIFIER_RE.fullmatch(
            operation_id
        ):
            raise ValueError("canonicalization layout snapshot is invalid")
        if source != template or source_sha != template_sha:
            raise ValueError("canonicalization source and template must match")
        layout_snapshot = {"operation_id": operation_id}
    elif stage == TEMPLATE_OCR_EVIDENCE_STAGE:
        if source != template or source_sha != template_sha:
            raise ValueError("template OCR evidence source and template must match")
        canonical_file = validate_uploaded_artifact_file(
            source,
            "canonical_template",
        )
        if canonical_file["sha256"] != source_sha:
            raise ValueError("template OCR evidence canonical hash is inconsistent")
        allowed_keys = {
            "question_crop_plan",
            "operation_id",  # compatibility for jobs issued before stable identity
            "page_index",
            "identity_version",
            "template_identity",
            "question_schema",
        }
        if set(layout_snapshot) - allowed_keys or "question_crop_plan" not in layout_snapshot:
            raise ValueError("template OCR evidence layout snapshot is invalid")
        from services.template_anchor_crops import validate_anchor_crop_plan

        normalized_plan = validate_anchor_crop_plan(
            layout_snapshot.get("question_crop_plan"),
            artifact_sha256=source_sha,
        )
        normalized_layout = {
            "question_crop_plan": normalized_plan,
        }
        stable_identity_requested = any(
            key in layout_snapshot
            for key in ("identity_version", "template_identity", "question_schema")
        )
        if stable_identity_requested:
            if layout_snapshot.get("identity_version") != TEMPLATE_OCR_IDENTITY_VERSION:
                raise ValueError("template OCR evidence identity version is invalid")
            identity = str(layout_snapshot.get("template_identity") or "").strip().lower()
            if not _TEMPLATE_OCR_IDENTITY_RE.fullmatch(identity):
                raise ValueError("template OCR evidence identity is invalid")
            page_index = layout_snapshot.get("page_index")
            if (
                isinstance(page_index, bool)
                or not isinstance(page_index, int)
                or not 0 <= page_index <= 10_000
            ):
                raise ValueError("template OCR evidence page_index is invalid")
            question_schema = normalize_template_ocr_question_schema(
                layout_snapshot.get("question_schema")
            )
            expected_identity = template_ocr_identity(
                teacher_id=teacher_id,
                canonical_sha256=source_sha,
                question_crop_plan=normalized_plan,
                page_index=page_index,
                question_schema=question_schema,
                algorithm_version=algorithm_version,
            )
            if identity != expected_identity:
                raise ValueError("template OCR evidence identity does not match layout")
            normalized_layout.update({
                "identity_version": TEMPLATE_OCR_IDENTITY_VERSION,
                "template_identity": identity,
                "question_schema": question_schema,
                "page_index": page_index,
            })
        elif "operation_id" in layout_snapshot:
            operation_id = str(layout_snapshot.get("operation_id") or "").strip()
            if not _IDENTIFIER_RE.fullmatch(operation_id):
                raise ValueError("template OCR evidence operation_id is invalid")
            normalized_layout["operation_id"] = operation_id
        elif "page_index" in layout_snapshot:
            page_index = layout_snapshot.get("page_index")
            if (
                isinstance(page_index, bool)
                or not isinstance(page_index, int)
                or not 0 <= page_index <= 10_000
            ):
                raise ValueError("template OCR evidence page_index is invalid")
            normalized_layout["page_index"] = page_index
        layout_snapshot = normalized_layout
    elif stage == VISION_MODEL_STAGE:
        layout_snapshot = _normalize_vision_model_layout(
            layout_snapshot,
            source=source,
            template=template,
            source_sha256=source_sha,
            template_sha256=template_sha,
        )
    else:
        from services.template_evidence import (
            PRINTED_SOLVE_LABEL_POLICY,
            _bind_slot_geometry,
            normalize_answer_regions,
            normalize_question_anchors,
            normalize_question_schema,
            normalize_template_handwriting_regions,
        )

        page_type_id = str(layout_snapshot.get("page_type_id") or "").strip()
        if not _IDENTIFIER_RE.fullmatch(page_type_id):
            raise ValueError("layout snapshot page_type_id is invalid")
        raw_layout_snapshot = layout_snapshot
        legacy_answer_regions = raw_layout_snapshot.get("answer_regions")
        normalized_legacy_answer_regions = None
        if isinstance(legacy_answer_regions, list):
            normalized_legacy_answer_regions = normalize_answer_regions(
                legacy_answer_regions
            )
        blank_detection_enabled = raw_layout_snapshot.get(
            "blank_detection_enabled", False
        )
        raw_question_schema = raw_layout_snapshot.get("question_schema")
        if raw_question_schema is None:
            # Migrate leases created before the answer-box contract was
            # removed.  Only identity/type is retained; legacy coordinates
            # are deliberately discarded from the stored layout.
            legacy_regions = raw_layout_snapshot.get("answer_regions")
            if isinstance(legacy_regions, list):
                raw_question_schema = [
                    {
                        "question_number": item.get("question_number"),
                        "question_type": item.get("question_type", "solve"),
                        "choice_mode": item.get("choice_mode", "unknown"),
                    }
                    for item in legacy_regions
                    if isinstance(item, dict)
                ]
        if not isinstance(blank_detection_enabled, bool):
            raise ValueError("layout snapshot blank_detection_enabled is invalid")
        layout_snapshot = {
            "page_type_id": page_type_id,
            "question_schema": normalize_question_schema(
                raw_question_schema
            ),
            "question_numbers": [
                item["question_number"]
                for item in normalize_question_schema(raw_question_schema)
            ],
            "blank_detection_enabled": blank_detection_enabled,
        }
        # Frozen ownership is distinct from handwriting exemptions. Keep it
        # in the hashed job identity and reject conflicts before persistence.
        if "template_mode" in raw_layout_snapshot:
            mode = raw_layout_snapshot["template_mode"]
            if mode not in ("blank", "marked"):
                raise ValueError("layout snapshot template_mode is invalid")
            layout_snapshot["template_mode"] = mode
        if "question_label_policy" in raw_layout_snapshot:
            policy = raw_layout_snapshot["question_label_policy"]
            if policy != PRINTED_SOLVE_LABEL_POLICY:
                raise ValueError("layout snapshot question_label_policy is invalid")
            layout_snapshot["question_label_policy"] = policy
        if "template_answer_regions" in raw_layout_snapshot:
            answer_regions = normalize_template_handwriting_regions(
                raw_layout_snapshot["template_answer_regions"]
            )
            if {item["question_number"] for item in answer_regions} != set(
                layout_snapshot["question_numbers"]
            ):
                raise ValueError("template answer regions do not match question schema")
            layout_snapshot["template_answer_regions"] = _bind_slot_geometry(
                layout_snapshot["question_schema"], answer_regions
            )
        raw_question_anchors = raw_layout_snapshot.get("question_anchors")
        if raw_question_anchors is not None:
            normalized_anchors = normalize_question_anchors(raw_question_anchors)
            if {
                item["question_number"] for item in normalized_anchors
            } != set(layout_snapshot["question_numbers"]):
                raise ValueError("question anchors do not match question schema")
            layout_snapshot["question_anchors"] = normalized_anchors
        question_crop_plan = raw_layout_snapshot.get("question_crop_plan")
        if stage == STUDENT_MULTI_EVIDENCE_STAGE and question_crop_plan is None:
            raise ValueError("student multi-evidence requires question_crop_plan")
        if question_crop_plan is not None:
            from services.template_anchor_crops import validate_anchor_crop_plan

            layout_snapshot["question_crop_plan"] = validate_anchor_crop_plan(
                question_crop_plan,
                artifact_sha256=template_sha,
            )
        handwriting_regions = raw_layout_snapshot.get(
            "template_handwriting_regions"
        )
        if handwriting_regions is not None:
            normalized_handwriting = normalize_template_handwriting_regions(
                handwriting_regions
            )
            # An empty list is a valid legacy state: the baseline carries no
            # exemption boxes. A non-empty contract must still name every
            # question exactly once.
            if normalized_handwriting and {
                item["question_number"] for item in normalized_handwriting
            } != set(layout_snapshot["question_numbers"]):
                raise ValueError(
                    "template handwriting regions do not match question schema"
                )
            layout_snapshot["template_handwriting_regions"] = (
                normalized_handwriting
            )
        if normalized_legacy_answer_regions is not None:
            # Compatibility only for already-issued focus-sheet jobs. New
            # full-column jobs never carry this private legacy key.
            layout_snapshot["_legacy_answer_regions"] = (
                normalized_legacy_answer_regions
            )
    layout_json = canonical_json(layout_snapshot)
    if len(layout_json.encode("utf-8")) > 128 * 1024:
        raise ValueError("layout_snapshot is too large")
    layout_sha = sha256_json(layout_snapshot)
    identity = (
        AcceleratorJob.teacher_id == teacher_id,
        AcceleratorJob.paper_id == paper_id,
        AcceleratorJob.source_sha256 == source_sha,
        AcceleratorJob.template_sha256 == template_sha,
        AcceleratorJob.layout_snapshot_sha256 == layout_sha,
        AcceleratorJob.stage == stage,
        AcceleratorJob.algorithm_version == algorithm_version,
    )
    existing = (
        db.query(AcceleratorJob)
        .filter(*identity)
        .order_by(
            case(
                (
                    AcceleratorJob.status.in_(_CONTRACT_TERMINAL_STATUSES),
                    1,
                ),
                else_=0,
            ),
            AcceleratorJob.created_at.asc(),
            AcceleratorJob.id.asc(),
        )
        .first()
    )
    if existing:
        if is_retryable_offload_terminal(existing):
            return resume_offload_job(db, existing.id)
        return existing
    deadline = deadline_at or (utcnow() + timedelta(seconds=JOB_DEADLINE_SECONDS))
    if deadline <= utcnow():
        raise ValueError("job deadline must be in the future")
    job = AcceleratorJob(
        id=uuid.uuid4().hex,
        batch_id=batch_id or None,
        paper_id=paper_id,
        teacher_id=teacher_id,
        source_path=str(source),
        source_sha256=source_sha,
        template_id=template_id,
        template_path=str(template),
        template_sha256=template_sha,
        layout_snapshot_json=layout_json,
        layout_snapshot_sha256=layout_sha,
        stage=stage,
        lane=lane,
        algorithm_version=algorithm_version,
        status="queued",
        deadline_at=deadline,
        server_validation_status="not_started",
    )
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = (
            db.query(AcceleratorJob)
            .filter(*identity)
            .order_by(
                case(
                    (
                        AcceleratorJob.status.in_(_CONTRACT_TERMINAL_STATUSES),
                        1,
                    ),
                    else_=0,
                ),
                AcceleratorJob.created_at.asc(),
                AcceleratorJob.id.asc(),
            )
            .first()
        )
        if existing is None:
            raise
        if is_retryable_offload_terminal(existing):
            return resume_offload_job(db, existing.id)
        return existing
    db.refresh(job)
    return job


def is_retryable_offload_terminal(job: AcceleratorJob) -> bool:
    """Return whether an offload-only job may be explicitly requeued."""
    return (
        job.stage in _OFFLOAD_ONLY_STAGES
        and job.status in {"fallback_failed", "server_fallback"}
        and job.error_code in _RETRYABLE_OFFLOAD_ERROR_CODES
        and (
            job.stage != VISION_MODEL_STAGE
            or job.error_code in _RETRYABLE_VISION_RESUME_ERROR_CODES
        )
    )


def resume_offload_job(
    db: Session,
    job_id: str,
    *,
    extend_seconds: int | None = None,
) -> AcceleratorJob:
    """Requeue one failed offload job without changing its durable identity.

    Opportunity nodes may be offline longer than the ordinary lease deadline.
    An explicit teacher retry therefore revives the same job ID instead of
    creating a duplicate contract row or falling back to server image work.
    """
    job = db.query(AcceleratorJob).filter(
        AcceleratorJob.id == str(job_id),
    ).first()
    if job is None or job.stage not in _OFFLOAD_ONLY_STAGES:
        raise ValueError("offload job is unavailable")
    if not is_retryable_offload_terminal(job):
        return job

    duration = JOB_DEADLINE_SECONDS if extend_seconds is None else int(extend_seconds)
    if not 60 <= duration <= 3600:
        raise ValueError("offload resume deadline is invalid")
    now = utcnow()
    try:
        changed = db.execute(
            update(AcceleratorJob)
            .where(
                AcceleratorJob.id == job.id,
                AcceleratorJob.stage.in_(_OFFLOAD_ONLY_STAGES),
                AcceleratorJob.status.in_(("fallback_failed", "server_fallback")),
                AcceleratorJob.error_code.in_(_RETRYABLE_OFFLOAD_ERROR_CODES),
            )
            .values(
                status="queued",
                attempt_count=0,
                assigned_node_id=None,
                lease_id_hash=None,
                lease_expires_at=None,
                deadline_at=now + timedelta(seconds=duration),
                candidate_json=None,
                candidate_sha256=None,
                server_validation_status="not_started",
                server_validation_json=None,
                error_code=None,
                candidate_received_at=None,
                validated_at=None,
                fallback_at=None,
                server_artifact_path=None,
                server_artifact_sha256=None,
                consumed_at=None,
                updated_at=now,
            )
        ).rowcount
        if changed == 1:
            # The queued state is not visible until commit, so a new lease
            # cannot race cleanup of artifacts left by the previous attempt.
            cleanup_candidate_artifacts(job.source_path, job.id)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return (
        db.query(AcceleratorJob)
        .execution_options(populate_existing=True)
        .filter(AcceleratorJob.id == job.id)
        .one()
    )


def _fallback_job(job: AcceleratorJob, code: str, now: datetime) -> None:
    job.status = (
        "fallback_failed"
        if job.stage in _OFFLOAD_ONLY_STAGES
        else "server_fallback"
    )
    job.error_code = code
    job.fallback_at = now
    job.assigned_node_id = None
    job.lease_id_hash = None
    job.lease_expires_at = None


def expire_leases(db: Session, now: datetime | None = None) -> int:
    """Atomically reclaim expired leases and abandoned server-owned work.

    This function is called both opportunistically before a claim and by the
    server reaper. Every transition includes its current state in the SQL
    predicate so two application workers cannot both reclaim the same lease.
    """
    now = now or utcnow()
    try:
        terminal_candidates = db.query(
            AcceleratorJob.id,
            AcceleratorJob.source_path,
        ).filter(
            or_(
                (
                    AcceleratorJob.status.in_(("queued", "leased"))
                    & (AcceleratorJob.deadline_at <= now)
                ),
                (
                    (AcceleratorJob.status == "leased")
                    & (AcceleratorJob.deadline_at > now)
                    & (AcceleratorJob.lease_expires_at <= now)
                    & (AcceleratorJob.attempt_count >= MAX_ATTEMPTS)
                ),
                (
                    (AcceleratorJob.status == "leased")
                    & (AcceleratorJob.stage == VISION_MODEL_STAGE)
                    & (AcceleratorJob.lease_expires_at <= now)
                ),
                (
                    AcceleratorJob.status.in_((
                        "candidate_received",
                        "server_validating",
                    ))
                    & (AcceleratorJob.deadline_at <= now)
                ),
                (
                    (AcceleratorJob.status == "fallback_running")
                    & or_(
                        AcceleratorJob.fallback_at.is_(None),
                        AcceleratorJob.fallback_at
                        <= now - timedelta(seconds=CPU_FALLBACK_STALE_SECONDS),
                    )
                ),
            )
        ).all()
        changed = 0
        deadline_update = db.execute(
            update(AcceleratorJob)
            .where(
                AcceleratorJob.status.in_(("queued", "leased")),
                AcceleratorJob.deadline_at <= now,
            )
            .values(
                status=case(
                    (
                        AcceleratorJob.stage.in_(_OFFLOAD_ONLY_STAGES),
                        "fallback_failed",
                    ),
                    else_="server_fallback",
                ),
                error_code="job_deadline_exceeded",
                fallback_at=now,
                assigned_node_id=None,
                lease_id_hash=None,
                lease_expires_at=None,
                updated_at=now,
            )
        )
        changed += int(deadline_update.rowcount or 0)

        validation_deadline_update = db.execute(
            update(AcceleratorJob)
            .where(
                AcceleratorJob.status.in_((
                    "candidate_received",
                    "server_validating",
                )),
                AcceleratorJob.deadline_at <= now,
            )
            .values(
                status=case(
                    (
                        AcceleratorJob.stage.in_(_OFFLOAD_ONLY_STAGES),
                        "fallback_failed",
                    ),
                    else_="server_fallback",
                ),
                error_code="server_validation_deadline_exceeded",
                fallback_at=now,
                assigned_node_id=None,
                lease_id_hash=None,
                lease_expires_at=None,
                server_validation_status="timed_out",
                server_artifact_path=None,
                server_artifact_sha256=None,
                updated_at=now,
            )
        )
        changed += int(validation_deadline_update.rowcount or 0)

        exhausted_update = db.execute(
            update(AcceleratorJob)
            .where(
                AcceleratorJob.status == "leased",
                AcceleratorJob.deadline_at > now,
                AcceleratorJob.lease_expires_at <= now,
                AcceleratorJob.attempt_count >= MAX_ATTEMPTS,
            )
            .values(
                status=case(
                    (
                        AcceleratorJob.stage.in_(_OFFLOAD_ONLY_STAGES),
                        "fallback_failed",
                    ),
                    else_="server_fallback",
                ),
                error_code="lease_attempts_exhausted",
                fallback_at=now,
                assigned_node_id=None,
                lease_id_hash=None,
                lease_expires_at=None,
                updated_at=now,
            )
        )
        changed += int(exhausted_update.rowcount or 0)

        ambiguous_model_update = db.execute(
            update(AcceleratorJob)
            .where(
                AcceleratorJob.status == "leased",
                AcceleratorJob.stage == VISION_MODEL_STAGE,
                AcceleratorJob.deadline_at > now,
                AcceleratorJob.lease_expires_at <= now,
                AcceleratorJob.attempt_count < MAX_ATTEMPTS,
            )
            .values(
                status="fallback_failed",
                error_code="provider_dispatch_ambiguous",
                fallback_at=now,
                assigned_node_id=None,
                lease_id_hash=None,
                lease_expires_at=None,
                updated_at=now,
            )
        )
        changed += int(ambiguous_model_update.rowcount or 0)

        requeue_update = db.execute(
            update(AcceleratorJob)
            .where(
                AcceleratorJob.status == "leased",
                AcceleratorJob.deadline_at > now,
                AcceleratorJob.lease_expires_at <= now,
                AcceleratorJob.attempt_count < MAX_ATTEMPTS,
                AcceleratorJob.stage != VISION_MODEL_STAGE,
            )
            .values(
                status="queued",
                error_code="lease_expired_requeued",
                assigned_node_id=None,
                lease_id_hash=None,
                lease_expires_at=None,
                updated_at=now,
            )
        )
        changed += int(requeue_update.rowcount or 0)

        fallback_stale_before = now - timedelta(
            seconds=CPU_FALLBACK_STALE_SECONDS
        )
        abandoned_fallback_update = db.execute(
            update(AcceleratorJob)
            .where(
                AcceleratorJob.status == "fallback_running",
                or_(
                    AcceleratorJob.fallback_at.is_(None),
                    AcceleratorJob.fallback_at <= fallback_stale_before,
                ),
            )
            .values(
                status="fallback_failed",
                error_code="cpu_fallback_owner_lost",
                server_validation_status="cpu_abandoned",
                server_validation_json=canonical_json({
                    "executor": "business_cpu_template_evidence",
                    "reason": "cpu_fallback_owner_lost",
                }),
                assigned_node_id=None,
                lease_id_hash=None,
                lease_expires_at=None,
                updated_at=now,
            )
        )
        changed += int(abandoned_fallback_update.rowcount or 0)
        db.commit()
        for job_id, source_path in terminal_candidates:
            terminal = db.query(AcceleratorJob.status).filter(
                AcceleratorJob.id == job_id,
            ).scalar()
            if terminal in {"server_fallback", "fallback_failed"}:
                cleanup_candidate_artifacts(source_path, job_id)
        return changed
    except Exception:
        db.rollback()
        raise


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
