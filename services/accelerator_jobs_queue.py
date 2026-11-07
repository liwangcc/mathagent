"""Jobs entering, re-entering and timing out of the accelerator queue.

- ``enqueue_job`` validates a job's stage, lane, identifiers, files and layout
  snapshot (a vision model request's through ``_normalize_vision_model_layout``)
  and queues it with its deadline.
- ``is_retryable_offload_terminal`` and ``resume_offload_job`` requeue a failed
  offload-only job without changing its durable identity
  (``_OFFLOAD_ONLY_STAGES``, ``_RETRYABLE_OFFLOAD_ERROR_CODES``,
  ``_RETRYABLE_VISION_RESUME_ERROR_CODES``, ``_CONTRACT_TERMINAL_STATUSES``).
- ``expire_leases`` atomically reclaims expired leases and abandoned
  server-owned work, before a claim and from the reaper; a job out of attempts
  goes to the server fallback (``_fallback_job``).

Moved unchanged from services/accelerator_jobs.py, which still exposes these names.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import case, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models_accelerator import AcceleratorJob
from services.accelerator_artifacts import (
    cleanup_candidate_artifacts,
    validate_uploaded_artifact_file,
)
from services.accelerator_jobs_base import (
    ALGORITHM_VERSION,
    CPU_FALLBACK_STALE_SECONDS,
    JOB_DEADLINE_SECONDS,
    MAX_ATTEMPTS,
    STUDENT_MULTI_EVIDENCE_STAGE,
    TEMPLATE_OCR_EVIDENCE_STAGE,
    VISION_MODEL_STAGE,
    canonical_json,
    sha256_file,
    sha256_json,
    utcnow,
)
from services.accelerator_jobs_identity import (
    TEMPLATE_OCR_IDENTITY_VERSION,
    _IDENTIFIER_RE,
    _TEMPLATE_OCR_IDENTITY_RE,
    _normalize_vision_model_layout,
    normalize_template_ocr_question_schema,
    template_ocr_identity,
)
from services.accelerator_jobs_nodes import _ALLOWED_LANES, _ALLOWED_STAGES
from services.template_canonicalization_contract import CANONICALIZATION_STAGE


_OFFLOAD_ONLY_STAGES = {
    "template_registration",
    STUDENT_MULTI_EVIDENCE_STAGE,
    CANONICALIZATION_STAGE,
    TEMPLATE_OCR_EVIDENCE_STAGE,
    VISION_MODEL_STAGE,
}


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
