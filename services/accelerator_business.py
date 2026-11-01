"""Business bridge from frozen-template OCR to opportunistic preprocessing.

Workers propose geometry and materialize the evidence consumed by the existing
answer-only OCR path. A live remote job remains authoritative while pending;
when the business handoff is durably unavailable, one worker may atomically
claim the verified server CPU matcher and close that handoff.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import and_, or_, update

from database import SessionLocal
from models_accelerator import AcceleratorJob, AcceleratorNode
from services.accelerator_artifacts import (
    BOUND_CLIENT_FULL_COLUMN_VALIDATOR,
    CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION,
    CLIENT_EVIDENCE_SLOT_GEOMETRY_CONTRACT_VERSION,
    layout_requires_slot_geometry,
    CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION,
    SERVER_REPLAYED_FULL_COLUMN_VALIDATOR,
)
from services.accelerator_jobs import (
    ALGORITHM_VERSION,
    CLIENT_EVIDENCE_CONTRACT_VERSION,
    NODE_STALE_SECONDS,
    STUDENT_MULTI_EVIDENCE_STAGE,
    TEMPLATE_OCR_IDENTITY_VERSION,
    canonical_json,
    enqueue_job,
    expire_leases,
    is_retryable_offload_terminal,
    node_supports_artifact_contract,
    node_supports_stage,
    resume_offload_job,
    template_ocr_layout_identity,
    utcnow,
)
from services.template_evidence import EvidenceResult, TemplateEvidenceMatcher
from services.template_canonicalization_contract import (
    CANONICALIZATION_STAGE,
    CANONICAL_TEMPLATE_CONTRACT_VERSION,
)

logger = logging.getLogger(__name__)

_CANONICAL_STORAGE_NAMESPACE_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\Z"
)

# Typical desktop-node image jobs finish in 29-40 seconds. Keep the request
# window below common 60-second gateway limits while allowing the normal job
# to complete without the former 20-second false timeout.
_MAX_WAIT_SECONDS = 60.0
WAIT_SECONDS = float(os.environ.get("ACCELERATOR_BUSINESS_WAIT_SECONDS", "50"))
# One-second polling is responsive at this job duration and avoids ten SQLite
# reads per second for every in-flight paper.
POLL_SECONDS = float(os.environ.get("ACCELERATOR_BUSINESS_POLL_SECONDS", "1"))
if not 0 <= WAIT_SECONDS <= _MAX_WAIT_SECONDS:
    raise RuntimeError("ACCELERATOR_BUSINESS_WAIT_SECONDS must be between 0 and 60")
if not 0.05 <= POLL_SECONDS <= 1:
    raise RuntimeError("ACCELERATOR_BUSINESS_POLL_SECONDS must be between 0.05 and 1")

_REMOTE_TERMINAL = {
    "server_fallback",
    "fallback_running",
    "fallback_completed",
    "fallback_failed",
    "consumed",
    "superseded",
}
_REMOTE_IN_PROGRESS = {
    "leased",
    "candidate_received",
    "server_validating",
}


@dataclass(frozen=True)
class AcceleratorEvidence:
    evidence: EvidenceResult | None
    job_ids: tuple[str, ...] = ()
    used_job_id: str = ""
    state: str = "cpu_fallback"
    reason_code: str = ""


@dataclass(frozen=True)
class AcceleratedCanonicalTemplate:
    state: str
    canonical_path: str | None = None
    canonicalization: dict | None = None
    job_id: str = ""
    reason_code: str = ""


@dataclass(frozen=True)
class _EnqueuedTemplatePages:
    job_ids: tuple[str, ...]
    node_available: bool
    lane: str

    def __len__(self) -> int:
        return len(self.job_ids)

    def __iter__(self):
        return iter(self.job_ids)

    def __getitem__(self, index):
        return self.job_ids[index]


def _compatible_nodes(db, teacher_id: int, lane: str):
    now = utcnow()
    scope = or_(
        AcceleratorNode.access_scope == "platform",
        and_(
            AcceleratorNode.access_scope == "tenant",
            AcceleratorNode.owner_user_id == teacher_id,
        ),
    )
    return db.query(AcceleratorNode).filter(
        AcceleratorNode.enabled == 1,
        AcceleratorNode.revoked_at.is_(None),
        or_(
            AcceleratorNode.token_expires_at.is_(None),
            AcceleratorNode.token_expires_at > now,
        ),
        AcceleratorNode.algorithm_version == ALGORITHM_VERSION,
        AcceleratorNode.lane == lane,
        scope,
    ).all()


def _registered_compatible_node(
    db,
    teacher_id: int,
    lane: str,
    *,
    stage: str = "template_registration",
    contract: str = CLIENT_EVIDENCE_CONTRACT_VERSION,
    additional_contracts: tuple[str, ...] = (),
) -> bool:
    return any(
        node_supports_stage(node, stage)
        and node_supports_artifact_contract(node, contract)
        and all(node_supports_artifact_contract(node, item) for item in additional_contracts)
        for node in _compatible_nodes(db, teacher_id, lane)
    )


def _live_compatible_node(
    db,
    teacher_id: int,
    lane: str,
    *,
    stage: str = "template_registration",
    contract: str = CLIENT_EVIDENCE_CONTRACT_VERSION,
    additional_contracts: tuple[str, ...] = (),
    require_free_slot: bool = True,
) -> bool:
    fresh_after = utcnow() - timedelta(seconds=NODE_STALE_SECONDS)
    return any(
        (not require_free_slot or node.free_slots > 0)
        and node.last_seen_at is not None
        and node.last_seen_at >= fresh_after
        and node_supports_stage(node, stage)
        and node_supports_artifact_contract(node, contract)
        and all(node_supports_artifact_contract(node, item) for item in additional_contracts)
        for node in _compatible_nodes(db, teacher_id, lane)
    )


def _select_lane(
    db,
    teacher_id: int,
    *,
    stage: str = "template_registration",
    contract: str = CLIENT_EVIDENCE_CONTRACT_VERSION,
    additional_contracts: tuple[str, ...] = (),
    require_free_slot: bool = True,
) -> str | None:
    options = {
        "stage": stage,
        "contract": contract,
        "additional_contracts": additional_contracts,
        "require_free_slot": require_free_slot,
    }
    if _live_compatible_node(db, teacher_id, "teacher_desktop", **options):
        return "teacher_desktop"
    if _live_compatible_node(db, teacher_id, "web_gpu", **options):
        return "web_gpu"
    return None


def _select_queue_lane(
    db,
    teacher_id: int,
    *,
    stage: str = "template_registration",
    contract: str = CLIENT_EVIDENCE_CONTRACT_VERSION,
    additional_contracts: tuple[str, ...] = (),
    require_free_slot: bool = True,
) -> tuple[str, bool]:
    live_lane = _select_lane(
        db,
        teacher_id,
        stage=stage,
        contract=contract,
        additional_contracts=additional_contracts,
        require_free_slot=require_free_slot,
    )
    if live_lane is not None:
        return live_lane, True
    if _registered_compatible_node(
        db,
        teacher_id,
        "teacher_desktop",
        stage=stage,
        contract=contract,
        additional_contracts=additional_contracts,
    ):
        return "teacher_desktop", False
    if _registered_compatible_node(
        db,
        teacher_id,
        "web_gpu",
        stage=stage,
        contract=contract,
        additional_contracts=additional_contracts,
    ):
        return "web_gpu", False
    # Platform opportunity nodes use the web lane.  Keeping the work there
    # allows a node that comes online after this request to claim it.
    return "web_gpu", False


def _enqueue_template_pages(
    *,
    teacher_id: int,
    batch_id: str,
    paper_id: str,
    source_path: str,
    template_id: str,
    artifacts: dict[str, dict],
    template_dir: str,
) -> _EnqueuedTemplatePages:
    db = SessionLocal()
    try:
        # Legacy templates are allowed to finish their already-issued focus
        # sheet jobs. Newly built artifacts never contain answer_regions and
        # therefore always use the full-column contract.
        legacy_contract = any(
            isinstance(artifact, dict) and "answer_regions" in artifact
            for artifact in artifacts.values()
        )
        lane, node_available = _select_queue_lane(
            db,
            teacher_id,
            stage=STUDENT_MULTI_EVIDENCE_STAGE,
            contract=(
                CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION
                if legacy_contract
                else CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION
            ),
            additional_contracts=(
                (CLIENT_EVIDENCE_SLOT_GEOMETRY_CONTRACT_VERSION,)
                if any(layout_requires_slot_geometry(item) for item in artifacts.values())
                else ()
            ),
            require_free_slot=False,
        )
        job_ids: list[str] = []
        root = Path(template_dir).resolve()
        for page_type_id, artifact in artifacts.items():
            template_path = (root / artifact["template_image"]).resolve()
            question_schema = artifact.get("question_schema") or []
            question_numbers = artifact.get("question_numbers") or []
            if not question_schema:
                legacy_regions = artifact.get("answer_regions") or []
                question_schema = [
                    {
                        "question_number": item.get("question_number"),
                        "question_type": item.get("question_type", "solve"),
                        "choice_mode": item.get("choice_mode", "unknown"),
                    }
                    for item in legacy_regions
                    if isinstance(item, dict)
                ]
                question_numbers = [
                    item["question_number"] for item in question_schema
                ]
            layout_snapshot = {
                "page_type_id": page_type_id,
                "question_schema": question_schema,
                "question_numbers": question_numbers,
                "blank_detection_enabled": artifact["blank_detection_enabled"],
            }
            for key in ("template_mode", "template_answer_regions", "question_label_policy"):
                if key in artifact:
                    layout_snapshot[key] = artifact[key]
            if legacy_contract:
                # Preserve the private compatibility signal so an already
                # issued answer-box job is rendered with the focus-sheet
                # contract. New artifacts never contain this field.
                layout_snapshot["answer_regions"] = artifact.get(
                    "answer_regions"
                ) or []
            if artifact.get("question_anchors") is not None:
                layout_snapshot["question_anchors"] = artifact.get(
                    "question_anchors"
                ) or []
            if artifact.get("question_crop_plan") is not None:
                layout_snapshot["question_crop_plan"] = artifact["question_crop_plan"]
            if artifact.get("template_handwriting_regions") is not None:
                layout_snapshot["template_handwriting_regions"] = artifact[
                    "template_handwriting_regions"
                ]
            job = enqueue_job(
                db,
                teacher_id=teacher_id,
                batch_id=batch_id,
                paper_id=paper_id,
                source_path=source_path,
                template_id=f"{template_id}:{page_type_id}",
                template_path=str(template_path),
                layout_snapshot=layout_snapshot,
                stage=STUDENT_MULTI_EVIDENCE_STAGE,
                lane=lane,
            )
            job_ids.append(job.id)
        return _EnqueuedTemplatePages(tuple(job_ids), node_available, lane)
    finally:
        db.close()


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(root))) == str(root)
    except ValueError:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_evidence(job: AcceleratorJob) -> EvidenceResult | None:
    if (
        job.status != "validated"
        or job.server_validation_status != "accepted"
        or not job.server_artifact_path
        or not job.server_artifact_sha256
    ):
        return None
    path = Path(job.server_artifact_path).resolve()
    source_root = Path(job.source_path).resolve().parent
    if (
        not _path_is_within(path, source_root)
        or not path.is_file()
        or _sha256(path) != job.server_artifact_sha256
    ):
        logger.warning("Validated accelerator artifact is missing or changed: %s", job.id)
        return None
    try:
        validation = json.loads(job.server_validation_json or "{}")
    except json.JSONDecodeError:
        return None
    validator = str(validation.get("validator") or "")
    # A historical full-column row was accepted from client pixels.  It may
    # contain a metadata-valid near-blank sidecar, so it is never eligible for
    # OCR after the server-owned replay contract is introduced.
    if validator == "trusted_client_evidence_full_column_v1":
        logger.warning(
            "Ignoring historical client-owned full-column evidence: %s",
            job.id,
        )
        return None
    if validator == BOUND_CLIENT_FULL_COLUMN_VALIDATOR and (
        validation.get("bound_node_identity_validated") is not True
        or validation.get("local_quality_report_validated") is not True
        or validation.get("client_answer_source_complete") is not True
        or validation.get("server_source_transform_replayed") is not False
        or validation.get("server_evidence_rendered") is not False
    ):
        return None
    evidence_paths: list[str]
    if validator in {
        "trusted_client_evidence_multi_v1",
        "trusted_client_evidence_multi_v2",
        "trusted_client_evidence_multi_v3",
        "trusted_client_evidence_full_column_v1",
        BOUND_CLIENT_FULL_COLUMN_VALIDATOR,
        SERVER_REPLAYED_FULL_COLUMN_VALIDATOR,
    }:
        try:
            index = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        expected_contract = {
            "trusted_client_evidence_multi_v1": "client-evidence-multi-v1",
            "trusted_client_evidence_multi_v2": "client-evidence-multi-v2",
            "trusted_client_evidence_multi_v3": (
                CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION
            ),
            "trusted_client_evidence_full_column_v1": (
                CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION
            ),
            SERVER_REPLAYED_FULL_COLUMN_VALIDATOR: (
                CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION
            ),
            BOUND_CLIENT_FULL_COLUMN_VALIDATOR: (
                CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION
            ),
        }[validator]
        if index.get("contract_version") != expected_contract:
            return None
        raw_images = index.get("evidence_images")
        image_limit = (
            3
            if validator in {
                "trusted_client_evidence_multi_v2",
                "trusted_client_evidence_full_column_v1",
                BOUND_CLIENT_FULL_COLUMN_VALIDATOR,
                SERVER_REPLAYED_FULL_COLUMN_VALIDATOR,
            }
            else 12
        )
        if not isinstance(raw_images, list) or not 1 <= len(raw_images) <= image_limit:
            return None
        evidence_paths = []
        for position, item in enumerate(raw_images, 1):
            if not isinstance(item, dict) or item.get("kind") != f"evidence_{position:02d}":
                return None
            image_path = (path.parent / f"evidence_{position:02d}.png").resolve()
            if (
                image_path.parent != path.parent
                or not image_path.is_file()
                or image_path.stat().st_size != int(item.get("byte_size") or 0)
                or _sha256(image_path) != item.get("sha256")
            ):
                return None
            evidence_paths.append(str(image_path))
    else:
        # Preserve visibility of historical validated artifacts so the atomic
        # fallback race still has a winner. Unknown validators remain marked
        # as server replay below and are rejected by the grading entry point.
        evidence_paths = [str(path)]
    page_type_id = str(validation.get("page_type_id") or "")
    question_numbers = [str(value) for value in validation.get("question_numbers") or []]
    if not page_type_id or not question_numbers:
        return None
    diagnostics = dict(validation.get("evidence_diagnostics") or {})
    client_authoritative = validator in {
        "bound_client_artifact_v1",
        "trusted_client_evidence_v1",
        "trusted_client_evidence_multi_v1",
        "trusted_client_evidence_multi_v2",
        "trusted_client_evidence_multi_v3",
        "trusted_client_evidence_full_column_v1",
        BOUND_CLIENT_FULL_COLUMN_VALIDATOR,
    }
    evidence_authority = {
        "bound_client_artifact_v1": "bound_client_artifact",
        "trusted_client_evidence_v1": "trusted_client_evidence",
        "trusted_client_evidence_multi_v1": "trusted_client_evidence_multi",
        "trusted_client_evidence_multi_v2": "trusted_client_evidence_multi",
        "trusted_client_evidence_multi_v3": "trusted_client_evidence_focus_sheets",
        "trusted_client_evidence_full_column_v1": "trusted_client_evidence_full_columns",
        SERVER_REPLAYED_FULL_COLUMN_VALIDATOR: "server_rendered_full_columns",
        BOUND_CLIENT_FULL_COLUMN_VALIDATOR: "bound_client_full_columns",
    }.get(validator, "server_cpu_replay")
    diagnostics["accelerator"] = {
        "job_id": job.id,
        "server_validated": True,
        "candidate_reused_for_acceptance": validator != SERVER_REPLAYED_FULL_COLUMN_VALIDATOR,
        "evidence_authority": evidence_authority,
        "server_source_transform_replayed": (
            True
            if validator == SERVER_REPLAYED_FULL_COLUMN_VALIDATOR
            else not client_authoritative
        ),
        "server_evidence_rendered": (
            True if validator == SERVER_REPLAYED_FULL_COLUMN_VALIDATOR else False
        ),
    }
    if validator == BOUND_CLIENT_FULL_COLUMN_VALIDATOR:
        diagnostics["accelerator"].update({
            "bound_node_identity_validated": True,
            "local_quality_report_validated": True,
        })
    return EvidenceResult(
        page_type_id=page_type_id,
        evidence_path=evidence_paths[0],
        evidence_paths=tuple(evidence_paths),
        question_numbers=question_numbers,
        blank_question_numbers=[
            str(value) for value in validation.get("blank_question_numbers") or []
        ],
        diagnostics=diagnostics,
    )


def _inspect_evidence_once(
    job_ids: list[str],
) -> tuple[EvidenceResult | None, str, bool, bool]:
    db = SessionLocal()
    try:
        rows = (
            db.query(AcceleratorJob)
            .execution_options(populate_existing=True)
            .filter(AcceleratorJob.id.in_(job_ids))
            .all()
        )
        if not rows or len(rows) != len(set(job_ids)):
            return None, "", True, False
        identities = set()
        try:
            for row in rows:
                page_type_id = str(json.loads(row.layout_snapshot_json)["page_type_id"])
                # enqueue_template_pages suffixes each candidate with its page
                # type; compare the frozen template family, not that suffix.
                template_family = row.template_id.removesuffix(f":{page_type_id}")
                identities.add((
                    row.teacher_id, row.batch_id, row.paper_id, row.source_sha256,
                    template_family, row.stage,
                ))
        except (ValueError, TypeError, KeyError, AttributeError):
            return None, "", True, False
        if len(identities) != 1:
            logger.error("Accelerator candidate group has mixed source identities")
            return None, "", True, False
        # Page-type candidates may finish on different nodes in either order.
        # Never let the first accepted receipt override a still-running match.
        terminal = all(
            row.status in _REMOTE_TERMINAL or row.status == "validated"
            for row in rows
        )
        if not terminal:
            in_progress = any(
                row.status in _REMOTE_IN_PROGRESS or row.status == "validated"
                for row in rows
            )
            return None, "", False, in_progress
        usable = []
        unusable_validated = []
        for row in rows:
            evidence = _validated_evidence(row)
            if evidence is not None:
                usable.append((row, evidence))
            elif row.status == "validated":
                unusable_validated.append(row.id)
        if len(usable) == 1:
            row, evidence = usable[0]
            return evidence, row.id, False, False
        if len(usable) > 1:
            ranked = []
            try:
                for row, evidence in usable:
                    candidate = json.loads(row.candidate_json or "{}")
                    score = (
                        int(candidate["inliers"]),
                        float(candidate["inlier_ratio"]),
                        float(candidate["coverage"]),
                        -float(candidate["reprojection_rmse"]),
                        int(candidate["good_matches"]),
                    )
                    if not all(math.isfinite(value) for value in score):
                        raise ValueError("non-finite candidate score")
                    ranked.append((score, candidate, row, evidence))
                ranked.sort(key=lambda item: item[0], reverse=True)
                # Use the existing matcher margin, without recomputing images.
                if TemplateEvidenceMatcher._has_clear_winner(ranked[0][1], ranked[1][1]):
                    return ranked[0][3], ranked[0][2].id, False, False
            except (ValueError, TypeError, KeyError, OverflowError):
                logger.error("Accelerator page-type scores are invalid")
            logger.warning("Accelerator page-type candidates are ambiguous")
            return None, "", True, False
        if unusable_validated:
            logger.error(
                "Validated accelerator evidence is unusable: %s",
                ",".join(unusable_validated),
            )
        return None, "", True, False
    finally:
        db.close()


def _poll_evidence_once(
    job_ids: list[str],
) -> tuple[EvidenceResult | None, str, bool]:
    evidence, used_job_id, terminal, _in_progress = _inspect_evidence_once(job_ids)
    return evidence, used_job_id, terminal


async def _wait_for_evidence(
    job_ids: list[str], deadline: float
) -> tuple[EvidenceResult | None, str, bool]:
    while time.monotonic() < deadline:
        evidence, used_job_id, terminal = await asyncio.to_thread(
            _poll_evidence_once,
            job_ids,
        )
        if evidence is not None:
            return evidence, used_job_id, False
        if terminal:
            return None, "", True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(POLL_SECONDS, remaining))
    return None, "", False


async def try_accelerated_evidence(
    *,
    teacher_id: str,
    batch_id: str,
    paper_id: str,
    source_path: str,
    template_id: str,
    artifacts: dict[str, dict],
    template_dir: str,
    wait_seconds: float | None = None,
) -> AcceleratorEvidence:
    """Return a validated artifact or an explicit non-CPU accelerator state."""

    if (
        not teacher_id
        or not batch_id
        or not paper_id
        or not template_id
        or not artifacts
    ):
        return AcceleratorEvidence(
            None,
            state="unavailable",
            reason_code="accelerator_context_invalid",
        )
    try:
        owner_id = int(teacher_id)
        request_wait = float(WAIT_SECONDS if wait_seconds is None else wait_seconds)
    except (TypeError, ValueError):
        return AcceleratorEvidence(
            None,
            state="unavailable",
            reason_code="accelerator_context_invalid",
        )
    if not 0 <= request_wait <= _MAX_WAIT_SECONDS:
        return AcceleratorEvidence(
            None,
            state="unavailable",
            reason_code="accelerator_context_invalid",
        )
    try:
        enqueued = await asyncio.to_thread(
            _enqueue_template_pages,
            teacher_id=owner_id,
            batch_id=batch_id,
            paper_id=paper_id,
            source_path=source_path,
            template_id=template_id,
            artifacts=artifacts,
            template_dir=template_dir,
        )
        if not enqueued.job_ids:
            return AcceleratorEvidence(
                None,
                state="unavailable",
                reason_code="accelerator_enqueue_empty",
            )
        evidence, used_job_id, terminal, in_progress = await asyncio.to_thread(
            _inspect_evidence_once,
            list(enqueued.job_ids),
        )
        if evidence is not None:
            return AcceleratorEvidence(
                evidence,
                enqueued.job_ids,
                used_job_id,
                state="ready",
            )
        if terminal:
            return AcceleratorEvidence(
                None,
                enqueued.job_ids,
                state="failed",
                reason_code="accelerator_job_failed",
            )
        if not enqueued.node_available and not in_progress:
            return AcceleratorEvidence(
                None,
                enqueued.job_ids,
                state="unavailable",
                reason_code="accelerator_node_unavailable",
            )
        evidence, used_job_id, terminal = await _wait_for_evidence(
            list(enqueued.job_ids),
            time.monotonic() + request_wait,
        )
        if evidence is not None:
            return AcceleratorEvidence(
                evidence,
                enqueued.job_ids,
                used_job_id,
                state="ready",
            )
        if terminal:
            return AcceleratorEvidence(
                None,
                enqueued.job_ids,
                state="failed",
                reason_code="accelerator_job_failed",
            )
        # A heartbeat only proves that a node was recently registered; it does
        # not prove that the queued job was actually claimed.  Once the bounded
        # wait has elapsed, re-read the durable rows.  A real remote producer
        # remains authoritative, while an entirely queued handoff may be
        # atomically claimed by PaperCache's server CPU fallback.
        evidence, used_job_id, terminal, in_progress = await asyncio.to_thread(
            _inspect_evidence_once,
            list(enqueued.job_ids),
        )
        if evidence is not None:
            return AcceleratorEvidence(
                evidence,
                enqueued.job_ids,
                used_job_id,
                state="ready",
            )
        if terminal:
            return AcceleratorEvidence(
                None,
                enqueued.job_ids,
                state="failed",
                reason_code="accelerator_job_failed",
            )
        if not in_progress:
            return AcceleratorEvidence(
                None,
                enqueued.job_ids,
                state="unavailable",
                reason_code="accelerator_wait_timeout_no_active_worker",
            )
        return AcceleratorEvidence(
            None,
            enqueued.job_ids,
            state="pending",
            reason_code="accelerator_wait_timeout",
        )
    except Exception as exc:
        logger.warning(
            "Accelerator business path is unavailable (%s): %s",
            type(exc).__name__,
            exc,
        )
        return AcceleratorEvidence(
            None,
            state="unavailable",
            reason_code="accelerator_enqueue_failed",
        )


def _enqueue_template_canonicalization(
    *,
    teacher_id: int,
    operation_id: str,
    source_path: str,
) -> tuple[str, bool]:
    db = SessionLocal()
    try:
        # Reuse an unfinished job for the same immutable source.  A browser
        # retry or an SSE reconnect must not enqueue a second lease for one
        # page; compare the streamed source digest before reusing it so a true
        # replacement upload still gets a fresh operation.
        source_digest = _sha256(Path(source_path))
        existing = (
            db.query(AcceleratorJob)
            .filter(
                AcceleratorJob.teacher_id == teacher_id,
                AcceleratorJob.paper_id == operation_id,
                AcceleratorJob.template_id == operation_id,
                AcceleratorJob.stage == CANONICALIZATION_STAGE,
                AcceleratorJob.source_sha256 == source_digest,
                AcceleratorJob.status.notin_(tuple(_REMOTE_TERMINAL)),
            )
            .order_by(AcceleratorJob.created_at.desc())
            .first()
        )
        if existing is not None:
            if is_retryable_offload_terminal(existing):
                existing = resume_offload_job(db, existing.id)
            node_available = _live_compatible_node(
                db,
                teacher_id,
                existing.lane,
                stage=CANONICALIZATION_STAGE,
                contract=CANONICAL_TEMPLATE_CONTRACT_VERSION,
                require_free_slot=False,
            )
            return existing.id, node_available
        lane, node_available = _select_queue_lane(
            db,
            teacher_id,
            stage=CANONICALIZATION_STAGE,
            contract=CANONICAL_TEMPLATE_CONTRACT_VERSION,
            require_free_slot=False,
        )
        job = enqueue_job(
            db,
            teacher_id=teacher_id,
            paper_id=operation_id,
            source_path=source_path,
            template_id=operation_id,
            template_path=source_path,
            layout_snapshot={"operation_id": operation_id},
            stage=CANONICALIZATION_STAGE,
            lane=lane,
        )
        if is_retryable_offload_terminal(job):
            job = resume_offload_job(db, job.id)
        return job.id, node_available
    finally:
        db.close()


def _validated_canonical_template(
    job: AcceleratorJob,
) -> AcceleratedCanonicalTemplate | None:
    if (
        job.stage != CANONICALIZATION_STAGE
        or job.status != "validated"
        or job.server_validation_status != "accepted"
        or not job.server_artifact_path
        or not job.server_artifact_sha256
        or not job.candidate_json
    ):
        return None
    path = Path(job.server_artifact_path).resolve()
    source_root = Path(job.source_path).resolve().parent
    if (
        not _path_is_within(path, source_root)
        or not path.is_file()
        or _sha256(path) != job.server_artifact_sha256
    ):
        return None
    try:
        candidate = json.loads(job.candidate_json)
        manifest = candidate["artifact_manifest"]
        canonicalization = candidate["canonicalization"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(manifest, dict)
        or manifest.get("contract_version")
        != CANONICAL_TEMPLATE_CONTRACT_VERSION
        or not isinstance(canonicalization, dict)
        or canonicalization.get("canonical_sha256")
        != job.server_artifact_sha256
    ):
        return None
    return AcceleratedCanonicalTemplate(
        state="ready",
        canonical_path=str(path),
        canonicalization=dict(canonicalization),
        job_id=job.id,
    )


def _poll_canonicalization_once(
    job_id: str,
) -> tuple[AcceleratedCanonicalTemplate | None, bool]:
    db = SessionLocal()
    try:
        job = (
            db.query(AcceleratorJob)
            .execution_options(populate_existing=True)
            .filter(AcceleratorJob.id == job_id)
            .first()
        )
        if job is None:
            return None, True
        ready = _validated_canonical_template(job)
        if ready is not None:
            return ready, False
        terminal = job.status in _REMOTE_TERMINAL or job.status == "validated"
        return None, terminal
    finally:
        db.close()


async def _wait_for_canonicalization(
    job_id: str,
    deadline: float,
) -> tuple[AcceleratedCanonicalTemplate | None, bool]:
    while time.monotonic() < deadline:
        ready, terminal = await asyncio.to_thread(
            _poll_canonicalization_once,
            job_id,
        )
        if ready is not None or terminal:
            return ready, terminal
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(POLL_SECONDS, remaining))
    return None, False


async def try_accelerated_template_canonicalization(
    *,
    teacher_id: int | str,
    operation_id: str,
    source_path: str,
    wait_seconds: float | None = None,
) -> AcceleratedCanonicalTemplate:
    """Return a client-canonicalized template or an explicit remote state."""

    operation_id = str(operation_id or "").strip()
    if not operation_id or not source_path:
        return AcceleratedCanonicalTemplate(
            state="unavailable",
            reason_code="accelerator_context_invalid",
        )
    try:
        owner_id = int(teacher_id)
        request_wait = float(WAIT_SECONDS if wait_seconds is None else wait_seconds)
    except (TypeError, ValueError):
        return AcceleratedCanonicalTemplate(
            state="unavailable",
            reason_code="accelerator_context_invalid",
        )
    if not 0 <= request_wait <= _MAX_WAIT_SECONDS:
        return AcceleratedCanonicalTemplate(
            state="unavailable",
            reason_code="accelerator_context_invalid",
        )
    try:
        job_id, node_available = await asyncio.to_thread(
            _enqueue_template_canonicalization,
            teacher_id=owner_id,
            operation_id=operation_id,
            source_path=source_path,
        )
        ready, terminal = await asyncio.to_thread(
            _poll_canonicalization_once,
            job_id,
        )
        if ready is not None:
            return ready
        if terminal:
            return AcceleratedCanonicalTemplate(
                state="fault",
                job_id=job_id,
                reason_code="accelerator_job_failed",
            )
        if not node_available:
            return AcceleratedCanonicalTemplate(
                state="unavailable",
                job_id=job_id,
                reason_code="accelerator_node_unavailable",
            )
        ready, terminal = await _wait_for_canonicalization(
            job_id,
            time.monotonic() + request_wait,
        )
        if ready is not None:
            return ready
        if terminal:
            return AcceleratedCanonicalTemplate(
                state="fault",
                job_id=job_id,
                reason_code="accelerator_job_failed",
            )
        return AcceleratedCanonicalTemplate(
            state="pending",
            job_id=job_id,
            reason_code="accelerator_wait_timeout",
        )
    except Exception as exc:
        logger.warning(
            "Template canonicalization accelerator failed (%s): %s",
            type(exc).__name__,
            exc,
        )
        return AcceleratedCanonicalTemplate(
            state="fault",
            reason_code="accelerator_enqueue_failed",
        )


def _canonicalization_job_owner(job_id: str, owner_id: int) -> AcceleratorJob:
    """Load and, when eligible, requeue one owner-bound canonicalization job."""
    db = SessionLocal()
    try:
        job = (
            db.query(AcceleratorJob)
            .filter(
                AcceleratorJob.id == str(job_id),
                AcceleratorJob.teacher_id == int(owner_id),
                AcceleratorJob.stage == CANONICALIZATION_STAGE,
            )
            .first()
        )
        if job is None:
            raise PermissionError("canonicalization job is unavailable")
        if is_retryable_offload_terminal(job):
            job = resume_offload_job(db, job.id)
        return job
    finally:
        db.close()


async def resume_accelerated_template_canonicalization(
    *,
    teacher_id: int | str,
    job_id: str,
    wait_seconds: float | None = None,
) -> AcceleratedCanonicalTemplate:
    """Poll an existing canonicalization job without enqueueing duplicate work.

    Upload/SSE callers may disconnect or hit a short request budget while a
    desktop/GPU node is still processing.  Resuming must therefore observe the
    original job and artifact rather than creating another image-processing job.
    """
    try:
        owner_id = int(teacher_id)
        normalized_job_id = str(job_id or "").strip()
        timeout = float(WAIT_SECONDS if wait_seconds is None else wait_seconds)
    except (TypeError, ValueError):
        return AcceleratedCanonicalTemplate(
            state="unavailable",
            reason_code="accelerator_context_invalid",
        )
    if not normalized_job_id or not 0 <= timeout <= _MAX_WAIT_SECONDS:
        return AcceleratedCanonicalTemplate(
            state="unavailable",
            reason_code="accelerator_context_invalid",
        )
    try:
        await asyncio.to_thread(_canonicalization_job_owner, normalized_job_id, owner_id)
        ready, terminal = await asyncio.to_thread(
            _poll_canonicalization_once,
            normalized_job_id,
        )
        if ready is not None:
            return ready
        if terminal:
            return AcceleratedCanonicalTemplate(
                state="fault",
                job_id=normalized_job_id,
                reason_code="accelerator_job_failed",
            )
        ready, terminal = await _wait_for_canonicalization(
            normalized_job_id,
            time.monotonic() + timeout,
        )
        if ready is not None:
            return ready
        if terminal:
            return AcceleratedCanonicalTemplate(
                state="fault",
                job_id=normalized_job_id,
                reason_code="accelerator_job_failed",
            )
        return AcceleratedCanonicalTemplate(
            state="pending",
            job_id=normalized_job_id,
            reason_code="accelerator_wait_timeout",
        )
    except PermissionError:
        return AcceleratedCanonicalTemplate(
            state="unavailable",
            job_id=normalized_job_id,
            reason_code="accelerator_job_unavailable",
        )
    except Exception as exc:
        logger.warning(
            "Template canonicalization resume failed (%s): %s",
            type(exc).__name__,
            exc,
        )
        return AcceleratedCanonicalTemplate(
            state="fault",
            job_id=normalized_job_id,
            reason_code="accelerator_poll_failed",
        )


def _copy_file_streaming(source: Path, target: Path) -> None:
    with source.open("rb") as input_stream, target.open("xb") as output_stream:
        shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
        output_stream.flush()
        os.fsync(output_stream.fileno())


def adopt_accelerated_template_canonicalization(
    handle: AcceleratedCanonicalTemplate,
    *,
    teacher_id: int | str,
    template_dir: str | os.PathLike[str],
    page_index: int,
    storage_namespace: str = "canonical",
) -> AcceleratedCanonicalTemplate:
    """Atomically move a validated canonical page into template-owned storage."""

    if (
        handle.state != "ready"
        or not handle.job_id
        or not handle.canonical_path
        or not isinstance(handle.canonicalization, dict)
    ):
        raise ValueError("canonicalization handle is not ready")
    if isinstance(page_index, bool) or not isinstance(page_index, int) or page_index < 0:
        raise ValueError("page_index must be a non-negative integer")
    if (
        not isinstance(storage_namespace, str)
        or not _CANONICAL_STORAGE_NAMESPACE_RE.fullmatch(storage_namespace)
    ):
        raise ValueError("storage_namespace is invalid")
    try:
        owner_id = int(teacher_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("teacher_id is invalid") from exc

    db = SessionLocal()
    staging: Path | None = None
    destination: Path | None = None
    published = False
    consumed = False
    try:
        job = db.query(AcceleratorJob).filter(
            AcceleratorJob.id == handle.job_id,
            AcceleratorJob.teacher_id == owner_id,
            AcceleratorJob.stage == CANONICALIZATION_STAGE,
        ).first()
        if job is None:
            raise PermissionError("canonicalization job is unavailable")
        ready = _validated_canonical_template(job)
        if (
            ready is None
            or Path(ready.canonical_path).resolve()
            != Path(handle.canonical_path).resolve()
        ):
            raise ValueError("canonicalization artifact is no longer valid")

        root = Path(template_dir)
        root.mkdir(parents=True, exist_ok=True)
        root = root.resolve()
        destination = (
            root / storage_namespace / f"page_{page_index:03d}"
        ).resolve()
        if not _path_is_within(destination, root) or destination == root:
            raise ValueError("canonicalization destination escaped template storage")
        source_format = ready.canonicalization.get("source_format")
        if source_format not in {"jpeg", "png"}:
            raise ValueError("canonicalization source format is invalid")
        source_name = "source.jpg" if source_format == "jpeg" else "source.png"
        source_path = Path(job.source_path).resolve()
        canonical_path = Path(ready.canonical_path).resolve()

        staging = root / f".canonical-adopt-{uuid.uuid4().hex}"
        staging.mkdir()
        staged_source = staging / source_name
        staged_canonical = staging / "template.png"
        _copy_file_streaming(source_path, staged_source)
        _copy_file_streaming(canonical_path, staged_canonical)
        if (
            _sha256(staged_source) != job.source_sha256
            or _sha256(staged_canonical) != job.server_artifact_sha256
        ):
            raise ValueError("canonicalization artifact changed during adoption")

        if destination.exists():
            existing_source = destination / source_name
            existing_canonical = destination / "template.png"
            if (
                not existing_source.is_file()
                or not existing_canonical.is_file()
                or _sha256(existing_source) != job.source_sha256
                or _sha256(existing_canonical) != job.server_artifact_sha256
            ):
                raise ValueError("canonicalization destination already exists")
            shutil.rmtree(staging, ignore_errors=True)
            staging = None
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, destination)
            staging = None
            published = True

        changed = db.execute(
            update(AcceleratorJob)
            .where(
                AcceleratorJob.id == job.id,
                AcceleratorJob.teacher_id == owner_id,
                AcceleratorJob.stage == CANONICALIZATION_STAGE,
                AcceleratorJob.status == "validated",
                AcceleratorJob.server_validation_status == "accepted",
                AcceleratorJob.server_artifact_path == str(canonical_path),
                AcceleratorJob.server_artifact_sha256
                == ready.canonicalization["canonical_sha256"],
            )
            .values(
                status="consumed",
                consumed_at=utcnow(),
                server_artifact_path=None,
                server_artifact_sha256=None,
                updated_at=utcnow(),
            )
        ).rowcount
        if changed != 1:
            db.rollback()
            if published and destination is not None:
                shutil.rmtree(destination, ignore_errors=True)
            raise ValueError("canonicalization adoption lost its ownership race")
        db.commit()
        consumed = True
        try:
            canonical_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "Failed to remove consumed canonical artifact %s: %s",
                job.id,
                exc,
            )

        canonicalization = dict(ready.canonicalization)
        canonicalization["source_image"] = (
            destination / source_name
        ).relative_to(root).as_posix()
        return AcceleratedCanonicalTemplate(
            state="ready",
            canonical_path=str(destination / "template.png"),
            canonicalization=canonicalization,
            job_id=job.id,
        )
    except Exception:
        db.rollback()
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        if published and not consumed and destination is not None:
            shutil.rmtree(destination, ignore_errors=True)
        raise
    finally:
        db.close()


def _template_ocr_layout_for_job(job: AcceleratorJob) -> dict | None:
    try:
        layout = json.loads(job.layout_snapshot_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return layout if isinstance(layout, dict) else None


def _is_reusable_template_ocr_job(job: AcceleratorJob) -> bool:
    layout = _template_ocr_layout_for_job(job)
    if layout is None:
        return False
    identity = template_ocr_layout_identity(
        teacher_id=job.teacher_id,
        source_sha256=job.source_sha256,
        layout_snapshot=layout,
        algorithm_version=job.algorithm_version,
    )
    return bool(identity and identity == layout.get("template_identity"))


def _is_published_reusable_template_ocr_job(job: AcceleratorJob) -> bool:
    if not _is_reusable_template_ocr_job(job):
        return False
    try:
        validation = json.loads(job.server_validation_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    layout = _template_ocr_layout_for_job(job) or {}
    return (
        validation.get("reusable") is True
        and validation.get("identity_version") == TEMPLATE_OCR_IDENTITY_VERSION
        and validation.get("template_identity") == layout.get("template_identity")
    )


def _remove_server_artifact(job: AcceleratorJob) -> None:
    if not job.server_artifact_path:
        return
    if _is_published_reusable_template_ocr_job(job):
        # Stable template OCR evidence is intentionally retained for reuse;
        # clearing the DB pointer here would make a valid cache unreachable.
        return
    path = Path(job.server_artifact_path).resolve()
    root = Path(job.source_path).resolve().parent
    if (
        path.name == "manifest.json"
        and re.fullmatch(r"evidence-set-[0-9a-f]{32}", path.parent.name)
        and _path_is_within(path.parent, root)
    ):
        try:
            shutil.rmtree(path.parent)
        except OSError as exc:
            logger.warning("Failed to remove accelerator evidence %s: %s", job.id, exc)
    elif _path_is_within(path, root):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Failed to remove accelerator evidence %s: %s", job.id, exc)
    job.server_artifact_path = None
    job.server_artifact_sha256 = None


def cpu_fallback_has_active_owner(
    handle: AcceleratorEvidence,
    *,
    teacher_id: str,
    batch_id: str,
    paper_id: str,
    source_path: str,
    template_id: str,
) -> bool:
    """Read-only proof of a live owner after a refused CPU claim.

    None from claim_cpu_fallback is deliberately ambiguous. Only a complete,
    same-request group with known states and an unexpired owner can make the
    caller wait; missing/corrupt rows must not become an endless pending loop.
    """
    from sqlalchemy.exc import SQLAlchemyError
    from services.accelerator_jobs import CPU_FALLBACK_STALE_SECONDS

    if not handle.job_ids or len(set(handle.job_ids)) != len(handle.job_ids):
        return False
    try:
        with SessionLocal() as db:
            rows = db.query(AcceleratorJob).filter(
                AcceleratorJob.id.in_(handle.job_ids),
            ).all()
            if len(rows) != len(handle.job_ids):
                return False
            now = utcnow()
            identities = set()
            active = False
            for row in rows:
                layout = _template_ocr_layout_for_job(row)
                page_type = layout.get("page_type_id") if layout else None
                if (
                    not isinstance(page_type, str) or not page_type
                    or row.teacher_id != int(teacher_id)
                    or row.batch_id != batch_id or row.paper_id != paper_id
                    or row.template_id != f"{template_id}:{page_type}"
                    or Path(row.source_path).resolve() != Path(source_path).resolve()
                    or row.stage not in {"template_registration", STUDENT_MULTI_EVIDENCE_STAGE}
                    or row.status not in _REMOTE_TERMINAL | _REMOTE_IN_PROGRESS | {"queued", "validated"}
                ):
                    return False
                identities.add((row.source_sha256, row.stage))
                if row.status == "validated" and _validated_evidence(row) is None:
                    return False
                if row.status in _REMOTE_IN_PROGRESS:
                    if (
                        not row.assigned_node_id or not row.lease_id_hash
                        or row.deadline_at is None or row.deadline_at <= now
                    ):
                        return False
                    if row.status == "leased":
                        if row.lease_expires_at is None or row.lease_expires_at <= now:
                            return False
                    elif (
                        row.candidate_received_at is None
                        or row.server_validation_status not in {"pending", "running"}
                    ):
                        return False
                    active = True
                elif row.status == "fallback_running":
                    if (
                        row.server_validation_status != "cpu_running"
                        or row.fallback_at is None
                        or row.fallback_at <= now - timedelta(seconds=CPU_FALLBACK_STALE_SECONDS)
                    ):
                        return False
                    active = True
            return active and len(identities) == 1
    except (SQLAlchemyError, OSError, TypeError, ValueError, AttributeError):
        # An unavailable store or malformed persisted receipt is not ownership
        # evidence. Preserve the caller's existing fail-closed result.
        logger.warning("Cannot establish an active owner after CPU claim refusal")
        return False


def claim_cpu_fallback(
    handle: AcceleratorEvidence,
) -> AcceleratorEvidence | None:
    """Claim an explicitly permitted CPU fallback without stealing remote work.

    Pending, unavailable and failed accelerator handles may only observe a
    validated race winner; they never transition jobs. Legacy ``cpu_fallback``
    handles retain the atomic claim used by callers that deliberately bypassed
    acceleration.
    """

    if handle.used_job_id:
        return handle
    if handle.state != "cpu_fallback":
        if not handle.job_ids:
            return None
        db = SessionLocal()
        try:
            rows = (
                db.query(AcceleratorJob)
                .execution_options(populate_existing=True)
                .filter(AcceleratorJob.id.in_(handle.job_ids))
                .all()
            )
            for row in rows:
                evidence = _validated_evidence(row)
                if evidence is not None:
                    return AcceleratorEvidence(
                        evidence,
                        handle.job_ids,
                        row.id,
                        state="ready",
                    )
            return None
        finally:
            db.close()
    if not handle.job_ids:
        return handle
    db = SessionLocal()
    try:
        # Recover an owner process that died after claiming the CPU fallback.
        # The same atomic reaper also closes validation work past its hard
        # deadline, so a request does not depend on the background loop running.
        expire_leases(db)
        rows = (
            db.query(AcceleratorJob)
            .execution_options(populate_existing=True)
            .filter(AcceleratorJob.id.in_(handle.job_ids))
            .all()
        )
        if any(row.stage == CANONICALIZATION_STAGE for row in rows):
            return None
        for row in rows:
            evidence = _validated_evidence(row)
            if evidence is not None:
                return AcceleratorEvidence(
                    evidence,
                    handle.job_ids,
                    row.id,
                    state="ready",
                )
        if len(rows) != len(handle.job_ids):
            return None
        live_statuses = {
            "queued",
            "leased",
            "candidate_received",
            "server_validating",
            "server_fallback",
        }
        if any(row.status == "validated" for row in rows):
            # A validated row without a usable server artifact is corrupt or was
            # already consumed. Never overwrite that authoritative boundary.
            return None
        if any(row.status == "fallback_running" for row in rows):
            # Another request already owns the CPU fallback.  This request must
            # not start a second matcher for the same business work.
            return None
        closed_statuses = {
            "fallback_completed",
            "fallback_failed",
            "consumed",
            "superseded",
        }
        if rows and all(row.status in closed_statuses for row in rows):
            # These rows belong to a prior, fully closed invocation.  There is
            # no remaining remote producer, so a user retry may use normal CPU.
            return AcceleratorEvidence(None)
        if any(row.status not in live_statuses for row in rows):
            # A mixed or unknown state cannot be claimed atomically.
            return None
        now = utcnow()
        changed = db.execute(
            update(AcceleratorJob)
            .where(
                AcceleratorJob.id.in_(handle.job_ids),
                # The earlier observation may already be stale. Only unowned
                # work is claimable at the CAS boundary; leased/received/
                # validating work must first be reclaimed by expire_leases.
                # A partial group update is rolled back below.
                AcceleratorJob.status.in_(("queued", "server_fallback")),
            )
            .values(
                status="fallback_running",
                server_validation_status="cpu_running",
                fallback_at=now,
                assigned_node_id=None,
                lease_id_hash=None,
                lease_expires_at=None,
                updated_at=now,
            )
        ).rowcount
        if int(changed or 0) != len(rows):
            db.rollback()
            # Validation may have committed between our SELECT and CAS. Re-read
            # once so that newly authoritative evidence wins the race; all
            # other outcomes fail closed instead of starting duplicate CPU work.
            raced_rows = (
                db.query(AcceleratorJob)
                .execution_options(populate_existing=True)
                .filter(AcceleratorJob.id.in_(handle.job_ids))
                .all()
            )
            for row in raced_rows:
                evidence = _validated_evidence(row)
                if evidence is not None:
                    return AcceleratorEvidence(
                        evidence,
                        handle.job_ids,
                        row.id,
                        state="ready",
                    )
            return None
        db.expire_all()
        claimed_rows = (
            db.query(AcceleratorJob)
            .execution_options(populate_existing=True)
            .filter(AcceleratorJob.id.in_(handle.job_ids))
            .all()
        )
        for row in claimed_rows:
            _remove_server_artifact(row)
        db.commit()
        return handle
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def finish_business_preprocessing(
    handle: AcceleratorEvidence,
    *,
    fallback_succeeded: bool,
    error_code: str = "",
) -> int:
    """Close the selected artifact or the request-owned CPU fallback exactly once."""

    if not handle.job_ids:
        return 0
    db = SessionLocal()
    try:
        now = utcnow()
        changed = 0
        rows = db.query(AcceleratorJob).filter(
            AcceleratorJob.id.in_(handle.job_ids)
        ).all()
        if any(row.stage == CANONICALIZATION_STAGE for row in rows):
            return 0
        for row in rows:
            if handle.used_job_id:
                if row.id == handle.used_job_id and row.status == "validated":
                    _remove_server_artifact(row)
                    row.status = "consumed"
                    row.consumed_at = now
                    changed += 1
                elif row.id != handle.used_job_id and row.status not in {
                    "consumed", "superseded",
                }:
                    _remove_server_artifact(row)
                    row.status = "superseded"
                    row.error_code = "validated_sibling_selected"
                    # A node's valid work may lose the page-type comparison.
                    # Keep its receipt identity so an in-flight timeout retry
                    # can learn that it was accepted, even after consumption.
                    if row.server_validation_status != "accepted":
                        row.assigned_node_id = None
                        row.lease_id_hash = None
                    row.lease_expires_at = None
                    changed += 1
                continue
            if row.status != "fallback_running":
                continue
            closed = db.execute(
                update(AcceleratorJob)
                .where(
                    AcceleratorJob.id == row.id,
                    AcceleratorJob.status == "fallback_running",
                )
                .values(
                    status=(
                        "fallback_completed"
                        if fallback_succeeded else "fallback_failed"
                    ),
                    server_validation_status=(
                        "cpu_completed" if fallback_succeeded else "cpu_failed"
                    ),
                    server_validation_json=canonical_json({
                        "executor": "business_cpu_template_evidence",
                        "reason": error_code or (
                            "accelerator_unavailable_or_rejected"
                            if fallback_succeeded
                            else "business_cpu_fallback_failed"
                        ),
                    }),
                    error_code=(
                        None
                        if fallback_succeeded
                        else (
                            error_code or "business_cpu_fallback_failed"
                        )[:64]
                    ),
                    consumed_at=now if fallback_succeeded else None,
                    updated_at=now,
                )
            ).rowcount
            changed += int(closed or 0)
        db.commit()
        return changed
    finally:
        db.close()
