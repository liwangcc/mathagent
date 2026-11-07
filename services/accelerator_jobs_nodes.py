"""Accelerator nodes: who they are, what they can do, and whether they are healthy.

- Registration and credentials: ``register_node`` (tenant or platform scope, one
  lane in ``_ALLOWED_LANES``), ``authenticate_node``, ``rotate_node_token`` and
  ``revoke_node``; rotating or revoking requeues the node's leases
  (``_requeue_node_leases``).
- ``heartbeat`` records a node's capabilities, free slots and queue depth and
  rejects a malformed or conflicting hardware gate; ``node_hardware_ready`` and
  ``_node_health`` derive whether it may take image work now.
- Capabilities: the stages, artifact contracts (``_STAGE_ARTIFACT_CONTRACTS``),
  vision provider profiles and models a node supports, the stages it may lease,
  whether its slot geometry matches (``validate_node_slot_geometry``), and
  whether it may see a job's lane (``_node_can_access_job``).
- ``list_nodes``, ``list_teacher_desktop_nodes`` and ``_node_public`` are the
  public views.

Moved unchanged from services/accelerator_jobs.py, which still exposes these names.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from models_accelerator import AcceleratorJob, AcceleratorNode
from services.accelerator_artifacts import (
    CLIENT_EVIDENCE_CONTRACT_VERSION,
    CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION,
    CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION,
    CLIENT_EVIDENCE_SLOT_GEOMETRY_CONTRACT_VERSION,
    TEMPLATE_OCR_EVIDENCE_CONTRACT_VERSION,
    layout_requires_slot_geometry,
)
from services.accelerator_jobs_base import (
    ALGORITHM_VERSION,
    NODE_STALE_SECONDS,
    STUDENT_MULTI_EVIDENCE_STAGE,
    TEMPLATE_OCR_EVIDENCE_STAGE,
    VISION_MODEL_RESPONSE_CONTRACT_VERSION,
    VISION_MODEL_STAGE,
    canonical_json,
    utcnow,
)
from services.accelerator_jobs_identity import _IDENTIFIER_RE, _MODEL_PROVIDER_PROFILES
from services.template_canonicalization_contract import (
    CANONICALIZATION_STAGE,
    CANONICAL_TEMPLATE_CONTRACT_VERSION,
)


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


_ALLOWED_LANES = {"web_gpu", "teacher_desktop"}


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
