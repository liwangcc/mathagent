"""HTTP protocol for explicitly scoped opportunistic preprocessing nodes."""
from __future__ import annotations
import asyncio
from datetime import datetime
import hashlib
import logging
import os
from pathlib import Path
import re
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from config import UPLOAD_DIR
from database import get_db
from models_accelerator import AcceleratorJob
from models_user import User
from routers.auth import _require_admin, _require_user
from services.accelerator_jobs import (
    ALGORITHM_VERSION,
    TEMPLATE_OCR_EVIDENCE_STAGE,
    authenticate_node,
    authorize_candidate_artifact_upload,
    enqueue_job,
    fail_job,
    heartbeat,
    job_public,
    lease_next_job,
    list_nodes,
    list_teacher_desktop_nodes,
    node_leaseable_stages,
    node_supported_artifact_contracts,
    node_supported_stages,
    register_node,
    revoke_node,
    rotate_node_token,
    renew_lease,
    submit_result,
    verify_job_asset,
)
from services.accelerator_artifacts import MAX_UPLOAD_BYTES, validate_uploaded_artifact_file
from services.accelerator_artifacts import TEMPLATE_OCR_EVIDENCE_KIND
from services.accelerator_validation import try_validate_candidate_job

router = APIRouter(prefix="/accelerator")
logger = logging.getLogger(__name__)

_IMAGE_PROCESSING_STAGES = (
    "template_registration",
    "student_multi_evidence",
    "template_canonicalization",
    TEMPLATE_OCR_EVIDENCE_STAGE,
)
_RETIRED_MODEL_STAGE = "vision_model_request"


def _log_lease_rejection(
    db: Session,
    node,
    *,
    job_id: str,
    lease_id: str,
    operation: str,
    phase: str,
) -> None:
    """Log lease state without exposing node or lease credentials."""

    rejected = (
        db.query(AcceleratorJob)
        .execution_options(populate_existing=True)
        .filter(AcceleratorJob.id == job_id)
        .first()
    )
    now = datetime.utcnow()
    expected_hash = hashlib.sha256((lease_id or "").encode("utf-8")).hexdigest()
    lease_remaining_ms = (
        int((rejected.lease_expires_at - now).total_seconds() * 1000)
        if rejected is not None and rejected.lease_expires_at is not None
        else None
    )
    deadline_remaining_ms = (
        int((rejected.deadline_at - now).total_seconds() * 1000)
        if rejected is not None and rejected.deadline_at is not None
        else None
    )
    logger.warning(
        "Accelerator lease rejected operation=%s phase=%s job_present=%s "
        "status=%s assigned_node_matches=%s owner_matches=%s "
        "lease_supplied=%s lease_hash_matches=%s lease_remaining_ms=%s "
        "deadline_remaining_ms=%s",
        operation,
        phase,
        rejected is not None,
        rejected.status if rejected is not None else None,
        bool(rejected and rejected.assigned_node_id == node.id),
        bool(
            rejected
            and (
                node.access_scope == "platform"
                or node.owner_user_id == rejected.teacher_id
            )
        ),
        bool(lease_id),
        bool(
            rejected
            and rejected.lease_id_hash
            and rejected.lease_id_hash == expected_hash
        ),
        lease_remaining_ms,
        deadline_remaining_ms,
    )


class RegisterNodeRequest(BaseModel):
    name: str = Field("MathAgent GPU Node", min_length=1, max_length=100)
    access_scope: str = Field("tenant", pattern="^(tenant|platform)$")


class RegisterDesktopDeviceRequest(BaseModel):
    device_id: str = Field(..., pattern=r"^[A-Za-z0-9._:-]{1,64}$")
    name: str = Field("MathAgent Windows Desktop", min_length=1, max_length=100)
    token_ttl_days: int = Field(90, ge=1, le=365)


class RotateDesktopTokenRequest(BaseModel):
    token_ttl_days: int = Field(90, ge=1, le=365)


class HeartbeatRequest(BaseModel):
    capabilities: dict = Field(default_factory=dict)
    algorithm_version: str = Field(..., min_length=1, max_length=64)
    free_slots: int = Field(..., ge=0, le=64)
    queue_depth: int = Field(0, ge=0, le=1024)
    estimated_finish_ms: int | None = Field(None, ge=0, le=3_600_000)
    last_error_code: str | None = Field(None, max_length=64)


class EnqueueRequest(BaseModel):
    paper_id: str = Field(..., min_length=1, max_length=64)
    source_relative_path: str = Field(..., min_length=1, max_length=500)
    template_id: str = Field(..., min_length=1, max_length=64)
    template_relative_path: str = Field(..., min_length=1, max_length=500)
    layout_snapshot: dict
    stage: str = Field(
        "template_registration",
        pattern="^(template_registration|student_multi_evidence|template_canonicalization|template_ocr_evidence)$",
    )


class ResultRequest(BaseModel):
    lease_id: str = Field(..., min_length=20, max_length=200)
    result: dict


class LeaseRequest(BaseModel):
    lease_id: str = Field(..., min_length=20, max_length=200)


class LeaseJobRequest(BaseModel):
    stages: list[str] | None = Field(None, min_length=1, max_length=32)


class FailureRequest(LeaseRequest):
    error_code: str = Field("worker_failed", min_length=1, max_length=64)


class ValidationRequest(BaseModel):
    force: bool = False


def _node_auth(
    db: Session = Depends(get_db),
    node_id: str | None = Header(None, alias="X-Accelerator-Node"),
    node_token: str | None = Header(None, alias="X-Accelerator-Token"),
):
    node = authenticate_node(db, node_id or "", node_token or "")
    if not node:
        raise HTTPException(401, detail="加速节点认证失败")
    return node


def _owned_upload_path(relative: str) -> Path:
    root = Path(UPLOAD_DIR).resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise HTTPException(400, detail="任务文件路径越界") from exc
    if not candidate.is_file():
        raise HTTPException(404, detail="任务文件不存在")
    return candidate


@router.post("/nodes/register")
def api_register_node(
    req: RegisterNodeRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(_require_admin),
):
    node, token = register_node(
        db,
        owner_user_id=admin.id,
        name=req.name,
        access_scope=req.access_scope,
    )
    return {
        "node_id": node.id,
        "node_token": token,
        "algorithm_version": ALGORITHM_VERSION,
        "access_scope": node.access_scope,
        "warning": "节点令牌仅显示一次，请存入受保护的本机凭据库。",
    }


@router.get("/nodes")
def api_list_nodes(
    db: Session = Depends(get_db),
    _admin: User = Depends(_require_admin),
):
    return {"nodes": list_nodes(db)}


def _owned_teacher_desktop_node(db: Session, user: User, node_id: str):
    from models_accelerator import AcceleratorNode

    node = db.query(AcceleratorNode).filter(
        AcceleratorNode.id == node_id,
        AcceleratorNode.owner_user_id == user.id,
        AcceleratorNode.lane == "teacher_desktop",
    ).first()
    if not node:
        raise HTTPException(404, detail="桌面设备不存在")
    return node


@router.post("/desktop/devices/register")
def api_register_desktop_device(
    req: RegisterDesktopDeviceRequest,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    try:
        node, token = register_node(
            db,
            owner_user_id=user.id,
            name=req.name,
            access_scope="tenant",
            lane="teacher_desktop",
            device_id=req.device_id,
            token_ttl_days=req.token_ttl_days,
        )
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return {
        "device_id": node.device_id,
        "node_id": node.id,
        "node_token": token,
        "lane": node.lane,
        "algorithm_version": ALGORITHM_VERSION,
        "token_expires_at": node.token_expires_at.isoformat(),
        "warning": "桌面令牌仅显示一次，请立即存入 Windows 受保护凭据库。",
    }


@router.get("/desktop/devices")
def api_list_desktop_devices(
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    return {"devices": list_teacher_desktop_nodes(db, user.id)}


@router.get("/desktop/devices/{node_id}")
def api_get_desktop_device(
    node_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    node = _owned_teacher_desktop_node(db, user, node_id)
    return {
        "node_id": node.id,
        "device_id": node.device_id,
        "enabled": bool(node.enabled),
        "revoked": node.revoked_at is not None,
        "token_expires_at": (
            node.token_expires_at.isoformat() if node.token_expires_at else None
        ),
    }


@router.post("/desktop/devices/{node_id}/rotate")
def api_rotate_desktop_device_token(
    node_id: str,
    req: RotateDesktopTokenRequest,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    _owned_teacher_desktop_node(db, user, node_id)
    try:
        node, token = rotate_node_token(
            db,
            node_id,
            owner_user_id=user.id,
            token_ttl_days=req.token_ttl_days,
        )
    except LookupError as exc:
        raise HTTPException(404, detail="桌面设备不存在") from exc
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return {
        "device_id": node.device_id,
        "node_id": node.id,
        "node_token": token,
        "lane": node.lane,
        "token_expires_at": node.token_expires_at.isoformat(),
        "warning": "新令牌仅显示一次，旧令牌已立即失效。",
    }


@router.post("/desktop/devices/{node_id}/revoke")
def api_revoke_desktop_device(
    node_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    _owned_teacher_desktop_node(db, user, node_id)
    revoke_node(db, node_id)
    return {"node_id": node_id, "revoked": True}


@router.post("/nodes/{node_id}/revoke")
def api_revoke_node(
    node_id: str,
    db: Session = Depends(get_db),
    _admin: User = Depends(_require_admin),
):
    try:
        revoke_node(db, node_id)
    except LookupError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    return {"node_id": node_id, "revoked": True}


@router.post("/nodes/heartbeat")
def api_heartbeat(
    req: HeartbeatRequest,
    db: Session = Depends(get_db),
    node=Depends(_node_auth),
):
    try:
        return heartbeat(
            db,
            node,
            capabilities=req.capabilities,
            algorithm_version=req.algorithm_version,
            free_slots=req.free_slots,
            queue_depth=req.queue_depth,
            estimated_finish_ms=req.estimated_finish_ms,
            last_error_code=req.last_error_code,
        )
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


@router.post("/jobs/enqueue")
def api_enqueue(
    req: EnqueueRequest,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    if (user.role or "").lower() != "admin":
        raise HTTPException(403, detail="当前仅管理员可创建远程加速任务")
    source = _owned_upload_path(req.source_relative_path)
    template = _owned_upload_path(req.template_relative_path)
    try:
        job = enqueue_job(
            db,
            teacher_id=user.id,
            paper_id=req.paper_id,
            source_path=str(source),
            template_id=req.template_id,
            template_path=str(template),
            layout_snapshot=req.layout_snapshot,
            stage=req.stage,
        )
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return job_public(job)


@router.post("/jobs/lease")
def api_lease(
    req: LeaseJobRequest | None = None,
    db: Session = Depends(get_db),
    node=Depends(_node_auth),
):
    requested_stages = req.stages if req is not None else None
    if requested_stages is not None and any(
        stage not in {*_IMAGE_PROCESSING_STAGES, _RETIRED_MODEL_STAGE}
        for stage in requested_stages
    ):
        raise HTTPException(422, detail="requested accelerator stages are invalid")
    if requested_stages is None:
        requested_stages = list(_IMAGE_PROCESSING_STAGES)
    else:
        requested_stages = [
            stage
            for stage in requested_stages
            if stage in _IMAGE_PROCESSING_STAGES
        ]
    # Older workers may still poll the retired model stage. Returning an empty
    # lease lets them continue accepting CPU/GPU image work without issuing an
    # OCR model request from the node.
    if not requested_stages:
        return {
            "job": None,
            "reason": "no_job_available",
            "supported_stages": sorted(node_supported_stages(node)),
            "supported_artifact_contracts": sorted(
                node_supported_artifact_contracts(node)
            ),
        }
    try:
        leased = lease_next_job(
            db,
            node,
            requested_stages=requested_stages,
        )
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    if not leased:
        supported_stages = sorted(node_supported_stages(node))
        supported_contracts = sorted(node_supported_artifact_contracts(node))
        leaseable_stages = sorted(node_leaseable_stages(node))
        return {
            "job": None,
            "reason": (
                "no_supported_stage"
                if not supported_stages
                else (
                    "no_supported_artifact_contract"
                    if not leaseable_stages
                    else "no_job_available"
                )
            ),
            "supported_stages": supported_stages,
            "supported_artifact_contracts": supported_contracts,
        }
    job, lease_secret = leased
    job_payload = {
        **job_public(job),
        "lease_id": lease_secret,
    }
    job_payload.update({
        "source_url": f"/api/accelerator/jobs/{job.id}/assets/source",
        "layout_snapshot": __import__("json").loads(job.layout_snapshot_json),
    })
    if job.stage == TEMPLATE_OCR_EVIDENCE_STAGE:
        job_payload["input_identity"] = {
            "canonical_sha256": job.source_sha256,
            "layout_snapshot_sha256": job.layout_snapshot_sha256,
        }
        job_payload["canonical_url"] = (
            f"/api/accelerator/jobs/{job.id}/assets/source"
        )
    if job.stage in {
        "template_registration",
        "student_multi_evidence",
        TEMPLATE_OCR_EVIDENCE_STAGE,
    }:
        job_payload["template_url"] = (
            f"/api/accelerator/jobs/{job.id}/assets/template"
        )
    return {
        "job": job_payload
    }


@router.get("/jobs/{job_id}/assets/{kind}")
def api_asset(
    job_id: str,
    kind: str,
    lease_id: str = Header(..., alias="X-Accelerator-Lease"),
    db: Session = Depends(get_db),
    node=Depends(_node_auth),
):
    try:
        job = renew_lease(db, node, job_id, lease_id)
        path = verify_job_asset(job, kind)
    except PermissionError as exc:
        # Never log the lease itself.  These booleans distinguish transport
        # header loss from a stale/foreign lease without exposing credentials.
        rejected = db.query(AcceleratorJob).filter(AcceleratorJob.id == job_id).first()
        now = datetime.utcnow()
        logger.warning(
            "Accelerator asset lease rejected job_present=%s status=%s "
            "assigned_node_matches=%s owner_matches=%s lease_hash_matches=%s "
            "lease_live=%s deadline_live=%s",
            rejected is not None,
            rejected.status if rejected is not None else None,
            bool(rejected and rejected.assigned_node_id == node.id),
            bool(
                rejected
                and (
                    node.access_scope == "platform"
                    or int(node.owner_user_id) == int(rejected.teacher_id)
                )
            ),
            bool(
                rejected
                and rejected.lease_id_hash
                and rejected.lease_id_hash
                == hashlib.sha256((lease_id or "").encode("utf-8")).hexdigest()
            ),
            bool(rejected and rejected.lease_expires_at and rejected.lease_expires_at > now),
            bool(rejected and rejected.deadline_at and rejected.deadline_at > now),
        )
        raise HTTPException(409, detail="任务租约无效或已过期") from exc
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return FileResponse(path, filename=f"{kind}{path.suffix.lower()}")


@router.post("/jobs/{job_id}/renew")
def api_renew(
    job_id: str,
    req: LeaseRequest,
    db: Session = Depends(get_db),
    node=Depends(_node_auth),
):
    try:
        job = renew_lease(db, node, job_id, req.lease_id)
    except PermissionError as exc:
        raise HTTPException(409, detail="任务租约无效或已过期") from exc
    return job_public(job)


@router.put("/jobs/{job_id}/artifacts/{kind}")
async def api_upload_artifact(
    job_id: str,
    kind: str,
    request: Request,
    lease_id: str = Header(..., alias="X-Accelerator-Lease"),
    content_length: int | None = Header(None, alias="Content-Length"),
    content_type: str | None = Header(None, alias="Content-Type"),
    db: Session = Depends(get_db),
    node=Depends(_node_auth),
):
    """Atomically stage one lease-bound PNG candidate artifact."""

    if kind not in {
        "evidence",
        "canonical_template",
        TEMPLATE_OCR_EVIDENCE_KIND,
    } and not re.fullmatch(r"evidence_(0[1-9]|1[0-2])", kind):
        raise HTTPException(404, detail="候选产物类型不存在")
    if (content_type or "").split(";", 1)[0].strip().lower() != "image/png":
        raise HTTPException(415, detail="候选产物必须是 PNG")
    if content_length is not None and (
        content_length <= 0 or content_length > MAX_UPLOAD_BYTES
    ):
        raise HTTPException(413, detail="候选产物大小超出限制")
    try:
        _job, target = authorize_candidate_artifact_upload(
            db,
            node,
            job_id=job_id,
            lease_secret=lease_id,
            kind=kind,
        )
    except PermissionError as exc:
        _log_lease_rejection(
            db,
            node,
            job_id=job_id,
            lease_id=lease_id,
            operation="artifact_upload",
            phase="pre_body",
        )
        raise HTTPException(409, detail="任务租约无效或已过期") from exc
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc

    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.upload")
    size = 0
    output = None
    try:
        output = await asyncio.to_thread(temporary.open, "wb")
        try:
            async for chunk in request.stream():
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, detail="候选产物大小超出限制")
                await asyncio.to_thread(output.write, chunk)
        finally:
            await asyncio.to_thread(output.close)
            output = None
        if size <= 0:
            raise HTTPException(422, detail="候选产物不能为空")
        metadata = await asyncio.to_thread(
            validate_uploaded_artifact_file,
            temporary,
            kind,
        )
        # Re-check the lease after receiving and decoding the body.  A request
        # that started before expiry must not publish bytes after expiry.
        try:
            _job, current_target = authorize_candidate_artifact_upload(
                db,
                node,
                job_id=job_id,
                lease_secret=lease_id,
                kind=kind,
            )
        except PermissionError as exc:
            _log_lease_rejection(
                db,
                node,
                job_id=job_id,
                lease_id=lease_id,
                operation="artifact_upload",
                phase="post_validation",
            )
            raise HTTPException(409, detail="任务租约无效或已过期") from exc
        if current_target != target:
            raise ValueError("candidate artifact target changed")
        os.replace(temporary, target)
    except HTTPException:
        raise
    except (OSError, ValueError) as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    finally:
        temporary.unlink(missing_ok=True)
    return {"job_id": job_id, "kind": kind, **metadata}


@router.post("/jobs/{job_id}/result")
def api_result(
    job_id: str,
    req: ResultRequest,
    db: Session = Depends(get_db),
    node=Depends(_node_auth),
):
    try:
        job = submit_result(
            db,
            node,
            job_id=job_id,
            lease_secret=req.lease_id,
            result=req.result,
        )
    except PermissionError as exc:
        raise HTTPException(409, detail="任务租约无效或已过期") from exc
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    if job.status == "candidate_received" and job.stage in _IMAGE_PROCESSING_STAGES:
        job = try_validate_candidate_job(db, job.id)
    public = job_public(job)
    if job.stage == _RETIRED_MODEL_STAGE or job.stage in _IMAGE_PROCESSING_STAGES:
        # The worker retains its exact idempotent candidate until acceptance.
        # Do not echo that payload back through every receipt retry.
        public["candidate"] = None
    return public


@router.post("/jobs/{job_id}/fail")
def api_fail(
    job_id: str,
    req: FailureRequest,
    db: Session = Depends(get_db),
    node=Depends(_node_auth),
):
    try:
        job = fail_job(
            db,
            node,
            job_id=job_id,
            lease_secret=req.lease_id,
            error_code=req.error_code,
        )
    except PermissionError as exc:
        raise HTTPException(409, detail="任务租约无效或已过期") from exc
    return job_public(job)


@router.get("/jobs/{job_id}")
def api_job(
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    job = db.query(AcceleratorJob).filter(
        AcceleratorJob.id == job_id,
        AcceleratorJob.teacher_id == user.id,
    ).first()
    if not job and (user.role or "").lower() == "admin":
        job = db.query(AcceleratorJob).filter(AcceleratorJob.id == job_id).first()
    if not job:
        raise HTTPException(404, detail="加速任务不存在")
    return job_public(job)


@router.post("/jobs/{job_id}/validate")
def api_validate_job(
    job_id: str,
    req: ValidationRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(_require_admin),
):
    try:
        job = try_validate_candidate_job(db, job_id)
    except (LookupError, ValueError) as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return job_public(job)
