"""Handing jobs to nodes: the next job a node may lease, a live lease, and the assets it may read or upload.

- ``lease_next_job`` reclaims expired leases first, then picks the next job for
  a node from the stages it may lease and its vision provider profiles and
  models (``_vision_provider_match``), within its slot capacity
  (``_node_lease_capacity``, ``_node_stage_slot_groups``) and its access scope
  (``_scope_filters``), choosing the least-loaded queued teacher on the platform
  lane (``_platform_fair_teacher_id``).
- ``renew_lease`` extends a lease the node still owns (``_owned_live_lease``,
  ``_lease_matches``).
- ``verify_job_asset`` resolves a job's source, template or vision asset;
  ``allowed_artifact_kinds_for_stage`` and ``authorize_candidate_artifact_upload``
  renew the lease and authorize one artifact upload without storing its bytes.

Moved unchanged from services/accelerator_jobs.py, which still exposes these names.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, exists, false, func, or_, select, update
from sqlalchemy.orm import Session

from models_accelerator import AcceleratorJob, AcceleratorNode
from services.accelerator_artifacts import (
    TEMPLATE_OCR_EVIDENCE_KIND,
    candidate_artifact_path,
    cleanup_candidate_artifacts,
)
from services.accelerator_jobs_base import (
    ALGORITHM_VERSION,
    LEASE_SECONDS,
    NODE_MAX_ACTIVE_LEASES,
    NODE_STALE_SECONDS,
    STUDENT_MULTI_EVIDENCE_STAGE,
    TEMPLATE_OCR_EVIDENCE_STAGE,
    VISION_MODEL_STAGE,
    sha256_file,
    utcnow,
)
from services.accelerator_jobs_nodes import (
    _ALLOWED_STAGES,
    _hash_secret,
    _node_can_access_job,
    _node_job_lanes,
    _node_slot_geometry_match,
    node_hardware_ready,
    node_leaseable_stages,
    node_supported_provider_models,
    node_supported_provider_profiles,
)
from services.accelerator_jobs_queue import _OFFLOAD_ONLY_STAGES, expire_leases
from services.template_canonicalization_contract import CANONICALIZATION_STAGE

# services.accelerator_jobs's logger, so these records keep their logger name.
logger = logging.getLogger("services.accelerator_jobs")


_IMAGE_PROCESSING_STAGES = frozenset(_OFFLOAD_ONLY_STAGES - {VISION_MODEL_STAGE})


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
