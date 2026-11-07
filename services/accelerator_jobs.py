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
from services.accelerator_jobs_leases import (
    _IMAGE_PROCESSING_STAGES,
    _scope_filters,
    _node_lease_capacity,
    _vision_provider_match,
    _node_stage_slot_groups,
    _platform_fair_teacher_id,
    lease_next_job,
    _lease_matches,
    _owned_live_lease,
    renew_lease,
    verify_job_asset,
    allowed_artifact_kinds_for_stage,
    authorize_candidate_artifact_upload,
)
from services.accelerator_jobs_results import (
    _ALLOWED_RESULT_KEYS,
    _REQUIRED_RESULT_KEYS,
    _CLIENT_EVIDENCE_RESULT_KEYS,
    _CANONICAL_RESULT_KEYS,
    _TEMPLATE_OCR_EVIDENCE_RESULT_KEYS,
    _TEMPLATE_OCR_EVIDENCE_INPUT_IDENTITY_KEYS,
    _EVIDENCE_DIAGNOSTIC_KEYS,
    _OPTIONAL_EVIDENCE_DIAGNOSTIC_KEYS,
    _COVERAGE_ITEM_KEYS,
    _MAX_RESULT_QUESTION_COUNT,
    _MAX_RESULT_QUESTION_LENGTH,
    _MAX_EVIDENCE_DIAGNOSTICS_BYTES,
    _MAX_MODEL_OUTPUT_BYTES,
    _normalize_result_sequence,
    _normalize_evidence_diagnostics,
    _validate_canonical_candidate_payload,
    _validate_template_ocr_evidence_candidate_payload,
    _validate_vision_model_result_payload,
    _validate_candidate_payload,
)

logger = logging.getLogger(__name__)


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
