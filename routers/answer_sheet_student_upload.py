"""Bringing a batch's student answer sheets in: one upload, from start to processed pages.

- POST /answer-sheet/sessions/{session_id}/students/upload/start
  (``start_student_upload``, ``StudentUploadStartRequest``) opens an upload or
  restarts it; DELETE .../students/upload/{upload_id} discards it.
- POST .../students/upload (``upload_students``) stores the pages of the open
  upload (``_require_student_upload``, ``_student_upload_pages``).
- POST .../students/upload/{upload_id}/process/stream
  (``process_student_upload_stream``) turns the stored pages into the batch's
  student pages over SSE: browser-normalized pixels are accepted as oriented
  (``_accelerator_managed_orientation``) and each template-code batch page is
  registered on the server (``_register_batch_page``, ``_registration_source``,
  ``_batch_page_registration_current``, ``_remove_superseded_registration``);
  a completed upload answers again without redoing the work
  (``_student_upload_checkpoint_complete``).

Moved unchanged from routers/answer_sheet.py, which still exposes these names
and includes this module's router before its own routes.
Annotations are evaluated when this module is imported (no ``from __future__
import annotations``): FastAPI resolves a decorated route's string annotations
in the decorator's module, so routes here carry real types.
"""

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from config import MAX_UPLOAD_FILES, MAX_UPLOAD_FILE_SIZE, MAX_UPLOAD_TOTAL_SIZE
from models_user import User
from routers.answer_sheet_assets import (
    _ALLOWED_EXTENSIONS,
    _remove_layout_assets,
    _remove_upload_path,
)
from routers.answer_sheet_availability import _require_answer_sheet_chain
from routers.answer_sheet_common import (
    _answer_sheet_sse,
    _answer_sheet_stream,
    _load_owned,
    _locked_owned,
    _requires_answer_sheet_writes,
    _run_answer_sheet_image_work,
    _serialized_session_mutation,
    _with_answer_sheet_heartbeats,
)
from routers.answer_sheet_pairing import _ORDERED_DUPLEX_PAIRING
from routers.answer_sheet_student_state import (
    _GRADING_DRAFT_CHECKPOINT_KEY,
    _STUDENT_UPLOAD_ACTIVE_STATES,
    _student_recognition_source_path,
    _student_upload_is_active,
    _valid_student_upload_id,
)
from routers.auth import _require_user
from services.answer_sheet_session import AnswerSheetSession
from services.collection_identity import normalize_source_path
from services.paper_orientation import sha256_file
from services.preprocess import ocr_ready_storage_suffix
from services.security import stream_to_disk_capped, validate_upload_path
from services.system_readiness import get_upload_storage_readiness_async
from services.teaching_context import stream_teaching
from services.template_evidence import TemplateEvidenceMatcher

# routers.answer_sheet's logger, so these records keep their logger name.
logger = logging.getLogger("routers.answer_sheet")
router = APIRouter()


def _accelerator_managed_orientation(_path: str) -> dict:
    """Accept browser-normalized pixels; registration verifies page geometry."""

    return {
        "orientation_status": "auto_passed",
        "orientation_reason": "client_normalized_node_registered",
        "orientation_confidence": 1.0,
    }


class StudentUploadStartRequest(BaseModel):
    restart: bool = False
    pairing_mode: Literal["ordered_duplex"] = _ORDERED_DUPLEX_PAIRING


def _student_upload_pages(session: AnswerSheetSession) -> list[dict]:
    upload = session.student_upload
    pages = upload.get("pages") if isinstance(upload, dict) else None
    return pages if isinstance(pages, list) else []


def _require_student_upload(
    session: AnswerSheetSession,
    upload_id: str,
) -> dict:
    upload = session.student_upload
    if (
        not isinstance(upload, dict)
        or upload.get("upload_id") != upload_id
        or not _valid_student_upload_id(upload_id)
    ):
        raise HTTPException(404, detail="学生答题卡上传任务不存在")
    return upload


def _student_upload_checkpoint_complete(page: dict) -> bool:
    checkpoint = page.get("upload_checkpoint")
    return (
        isinstance(checkpoint, dict)
        and checkpoint.get("state") == "orientation_complete"
        and checkpoint.get("sha256") == page.get("sha256")
    )


@router.post("/sessions/{session_id}/students/upload/start")
@_requires_answer_sheet_writes
@_serialized_session_mutation
def start_student_upload(
    session_id: str,
    req: StudentUploadStartRequest = Body(default_factory=StudentUploadStartRequest),
    user: User = Depends(_require_user),
):
    session = _load_owned(session_id, user.id)
    if session.status not in {"frozen", "review"}:
        raise HTTPException(409, detail="请先冻结答题卡模板")
    if _student_upload_is_active(session) and not req.restart:
        session.student_upload.setdefault("pairing_mode", _ORDERED_DUPLEX_PAIRING)
        session.student_upload.setdefault("order_contract", "front_back_adjacent")
        session.save()
        return session.to_dict(public=True)

    previous_staged = _student_upload_pages(session)
    upload_id = uuid.uuid4().hex
    session.student_upload = {
        "upload_id": upload_id,
        "status": "uploading",
        "pages": [],
        "total_bytes": 0,
        "pairing_mode": req.pairing_mode,
        "order_contract": "front_back_adjacent",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "error": None,
    }
    session.error = None
    session.save()
    _remove_layout_assets(session.session_dir, previous_staged, {})
    return session.to_dict(public=True)


@router.delete("/sessions/{session_id}/students/upload/{upload_id}")
@_requires_answer_sheet_writes
@_serialized_session_mutation
def discard_student_upload(
    session_id: str,
    upload_id: str,
    user: User = Depends(_require_user),
):
    session = _load_owned(session_id, user.id)
    upload = _require_student_upload(session, upload_id)
    staged = _student_upload_pages(session)
    if upload.get("status") == "completed":
        staged = []
    session.student_upload = {}
    session.error = None
    session.save()
    _remove_layout_assets(session.session_dir, staged, {})
    return session.to_dict(public=True)


@router.post("/sessions/{session_id}/students/upload")
@_requires_answer_sheet_writes
@_serialized_session_mutation
async def upload_students(
    session_id: str,
    upload_id: str = Form(...),
    files: list[UploadFile] = File(...),
    source_path: str = Form(""),
    user: User = Depends(_require_user),
):
    session = _load_owned(session_id, user.id)
    if session.status not in {"frozen", "review"}:
        raise HTTPException(409, detail="请先冻结答题卡模板")
    upload = _require_student_upload(session, upload_id)
    try:
        # Display and identity metadata only, never a storage path.
        source_relative_path = normalize_source_path(source_path) if source_path else ""
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    if upload.get("status") not in _STUDENT_UPLOAD_ACTIVE_STATES:
        raise HTTPException(409, detail="学生答题卡上传任务已经结束")
    if len(files) != 1:
        raise HTTPException(413, detail="学生答题卡必须逐页上传")

    file = files[0]
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _ALLOWED_EXTENSIONS:
        raise HTTPException(400, detail="仅支持 JPG、JPEG、PNG 图片")

    written_path: str | None = None
    persisted = False
    try:
        pages = _student_upload_pages(session)
        if len(pages) >= MAX_UPLOAD_FILES:
            raise HTTPException(413, detail=f"学生答题卡最多上传 {MAX_UPLOAD_FILES} 页")
        total_bytes = sum(
            max(0, int(page.get("byte_size") or 0))
            for page in pages
            if isinstance(page, dict)
        )
        remaining_bytes = MAX_UPLOAD_TOTAL_SIZE - total_bytes
        if remaining_bytes <= 0:
            raise HTTPException(413, detail="上传文件总大小超过限制")
        byte_limit = min(MAX_UPLOAD_FILE_SIZE, remaining_bytes)
        target = os.path.join(session.session_dir, f"student_upload_{upload_id}")
        storage = await get_upload_storage_readiness_async(
            target,
            byte_limit,
            required_copies=1,
        )
        if not storage.get("ready"):
            raise HTTPException(
                503,
                detail={
                    "error": "SERVER_STORAGE_UNAVAILABLE",
                    "message": "服务器存储空间不足或不可写，当前无法接收新的答题卡批阅任务。",
                },
            )
        os.makedirs(target, exist_ok=True)
        storage_suffix = ocr_ready_storage_suffix(file.filename or "") or suffix
        name = (
            f"student_{len(pages) + 1}_{uuid.uuid4().hex}"
            f"{storage_suffix}"
        )
        written_path = os.path.join(target, name)
        size = await stream_to_disk_capped(file, written_path, byte_limit)
        if size < 0:
            if remaining_bytes < MAX_UPLOAD_FILE_SIZE:
                raise HTTPException(413, detail="上传文件总大小超过限制")
            raise HTTPException(413, detail="单个上传文件大小超过限制")
        if size == 0:
            raise HTTPException(400, detail="上传图片为空")
        validation = await _run_answer_sheet_image_work(
            validate_upload_path,
            file.filename or f"student{suffix}",
            written_path,
        )
        if not validation.get("safe"):
            raise HTTPException(
                400,
                detail=validation.get("reason") or "图片校验失败",
            )
        digest = await _run_answer_sheet_image_work(sha256_file, written_path)
        if any(page.get("sha256") == digest for page in pages):
            _remove_upload_path(written_path)
            written_path = None
            upload["status"] = "uploading"
            upload["error"] = None
            session.save()
            persisted = True
            return session.to_dict(public=True)
        physical_order = len(pages) + 1
        page = {
            "paper_id": f"student_{len(pages) + 1}",
            "index": len(pages),
            "physical_order": physical_order,
            "physical_pair_index": (physical_order + 1) // 2,
            "physical_side": "front" if physical_order % 2 else "back",
            "path": written_path,
            "sha256": digest,
            "byte_size": size,
            "filename": file.filename or name,
            "source_relative_path": source_relative_path,
            "orientation_status": "pending",
            "identity": {
                "status": "pending",
                "student_id": None,
                "candidates": [],
            },
            "answers": [],
            "review_reasons": [],
        }
        pages.append(page)
        upload["pages"] = pages
        upload["total_bytes"] = total_bytes + size
        upload["status"] = "uploading"
        upload["error"] = None
        try:
            session.save()
        except BaseException:
            pages.pop()
            raise
        persisted = True
        return session.to_dict(public=True)
    finally:
        if written_path is not None and not persisted:
            _remove_upload_path(written_path)
    return session.to_dict(public=True)


@router.post("/sessions/{session_id}/students/upload/{upload_id}/process/stream")
@_requires_answer_sheet_writes
async def process_student_upload_stream(
    session_id: str,
    upload_id: str,
    user: User = Depends(_require_user),
):
    session = _load_owned(session_id, user.id)
    upload = _require_student_upload(session, upload_id)
    if upload.get("status") == "completed" and session.student_pages:
        async def completed_events():
            yield _answer_sheet_sse("done", {
                "message": "学生答题卡上传处理已完成",
                "current": len(session.student_pages),
                "total": len(session.student_pages),
                "idempotent": True,
                "session": session.to_dict(public=True),
            })

        return _answer_sheet_stream(completed_events())
    try:
        await _require_answer_sheet_chain(user)
    except HTTPException as exc:
        unavailable_message = str(exc.detail)

        async def unavailable_events():
            yield _answer_sheet_sse("error", {
                "code": "GRADING_CHAIN_FAILED",
                "message": unavailable_message,
            })

        return _answer_sheet_stream(unavailable_events())
    if session.status not in {"frozen", "review"}:
        raise HTTPException(409, detail="请先冻结答题卡模板")
    if not _student_upload_pages(session):
        raise HTTPException(409, detail="请先上传学生答题卡")

    async def events():
        try:
            with _locked_owned(session_id, user.id) as locked:
                locked_upload = _require_student_upload(locked, upload_id)
                pages = _student_upload_pages(locked)
                if locked_upload.get("status") == "completed" and locked.student_pages:
                    yield _answer_sheet_sse("done", {
                        "message": "学生答题卡上传处理已完成",
                        "current": len(locked.student_pages),
                        "total": len(locked.student_pages),
                        "idempotent": True,
                        "session": locked.to_dict(public=True),
                    })
                    return
                if locked.status not in {"frozen", "review"} or not pages:
                    raise HTTPException(409, detail="学生答题卡上传状态已经变化，请刷新后重试")
                total = len(pages)
                completed = sum(
                    1 for page in pages if _student_upload_checkpoint_complete(page)
                )
                locked_upload["status"] = "processing"
                locked_upload["error"] = None
                locked.error = None
                await asyncio.to_thread(locked.save)
                yield _answer_sheet_sse("progress", {
                    "message": "正在恢复学生答题卡方向处理" if completed else "正在检查学生答题卡方向",
                    "current": completed,
                    "total": total,
                })
                finalized = False
                # Template-code batches register every page on the server
                # right here; recognition later reads only that image.
                register_pages = locked.is_batch
                registration_matcher: dict = {}
                try:
                    for page in pages:
                        oriented = _student_upload_checkpoint_complete(page)
                        if oriented and (
                            not register_pages or _batch_page_registration_current(locked, page)
                        ):
                            continue
                        if not oriented:
                            orientation = None
                            async for event, payload in _with_answer_sheet_heartbeats(
                                _run_answer_sheet_image_work(
                                    _accelerator_managed_orientation,
                                    page["path"],
                                ),
                                message=f"正在检查 {page.get('filename') or page.get('paper_id')} 的方向",
                                current=completed,
                                total=total,
                            ):
                                if event == "progress":
                                    yield _answer_sheet_sse("progress", payload)
                                else:
                                    orientation = payload
                            if not isinstance(orientation, dict):
                                raise RuntimeError("student orientation task ended without a result")
                            page.update(orientation)
                            page["orientation_source_sha256"] = page.get("sha256")
                            page["orientation_working_sha256"] = page.get("sha256")
                            page["upload_checkpoint"] = {
                                "state": "orientation_complete",
                                "sha256": page.get("sha256"),
                            }
                            completed += 1
                        superseded = None
                        if register_pages:
                            registration = None
                            async for event, payload in _with_answer_sheet_heartbeats(
                                _register_batch_page(locked, page, registration_matcher),
                                message=f"正在配准 {page.get('filename') or page.get('paper_id')}",
                                current=completed,
                                total=total,
                            ):
                                if event == "progress":
                                    yield _answer_sheet_sse("progress", payload)
                                else:
                                    registration = payload
                            if not isinstance(registration, dict):
                                raise RuntimeError("student page registration ended without a result")
                            superseded = page.get("registered_image")
                            page["registered_image"] = registration
                        await asyncio.to_thread(locked.save)
                        _remove_superseded_registration(locked, superseded, page.get("registered_image"))
                        yield _answer_sheet_sse("progress", {
                            "message": "当前页方向进度已保存",
                            "current": completed,
                            "total": total,
                            "checkpoint": True,
                            "paper_id": page.get("paper_id"),
                        })

                    previous_pages = list(locked.student_pages)
                    for page in pages:
                        page.pop("upload_checkpoint", None)
                    total_bytes = sum(
                        max(0, int(page.get("byte_size") or 0))
                        for page in pages
                        if isinstance(page, dict)
                    )
                    locked.student_pages = pages
                    locked.invalidate_grading_drafts()
                    locked.asset_lifecycle.pop(_GRADING_DRAFT_CHECKPOINT_KEY, None)
                    locked.status = "recognizing"
                    locked.error = None
                    locked.student_upload = {
                        "upload_id": upload_id,
                        "status": "completed",
                        "pairing_mode": str(
                            locked_upload.get("pairing_mode") or _ORDERED_DUPLEX_PAIRING
                        ),
                        "order_contract": "front_back_adjacent",
                        "uploaded_count": total,
                        "processed_count": total,
                        "total_bytes": total_bytes,
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "error": None,
                    }
                    await asyncio.to_thread(locked.save)
                    finalized = True
                    _remove_layout_assets(
                        locked.session_dir,
                        previous_pages,
                        {},
                        preserve_pages=pages,
                    )
                    yield _answer_sheet_sse("done", {
                        "message": "学生答题卡上传处理完成",
                        "current": total,
                        "total": total,
                        "session": locked.to_dict(public=True),
                    })
                except (asyncio.CancelledError, GeneratorExit):
                    if finalized:
                        raise
                    locked_upload["status"] = "processing"
                    locked_upload["error"] = "方向处理连接中断，已保存完成页；再次执行即可恢复。"
                    locked.error = locked_upload["error"]
                    await asyncio.to_thread(locked.save)
                    raise
                except Exception:
                    if finalized:
                        raise
                    logger.exception("answer-sheet student upload processing failed")
                    locked_upload["status"] = "processing"
                    locked_upload["error"] = "学生答题卡方向处理链路中断，已保存完成页；请稍后重试恢复。"
                    locked.error = locked_upload["error"]
                    await asyncio.to_thread(locked.save)
                    yield _answer_sheet_sse("error", {
                        "code": "GRADING_CHAIN_FAILED",
                        "message": locked.error,
                    })
        except HTTPException as exc:
            yield _answer_sheet_sse("error", {
                "code": "ANSWER_SHEET_OPERATION_BUSY" if exc.status_code == 409 else "GRADING_CHAIN_FAILED",
                "message": str(exc.detail),
            })

    return _answer_sheet_stream(stream_teaching(session.teaching_context, events()))


def _registration_source(page: dict) -> tuple[str, str]:
    """The oriented source a batch page is registered from, and its hash."""
    return (
        _student_recognition_source_path(page),
        str(page.get("orientation_working_sha256") or page.get("sha256") or ""),
    )


def _batch_page_registration_current(session: AnswerSheetSession, page: dict) -> bool:
    from services.answer_sheet_page_registration import registration_is_current

    _path, source_sha256 = _registration_source(page)
    return registration_is_current(
        page.get("registered_image"),
        template_fingerprint=str(session.shared_template_hash or ""),
        source_sha256=source_sha256,
    )


async def _register_batch_page(
    session: AnswerSheetSession,
    page: dict,
    matcher_holder: dict,
) -> dict:
    """Register one template-code batch page on the server (answer-sheet B5).

    Always returns a record: a page that cannot be registered gets a
    ``failed`` record, which recognition later refuses (fail closed).
    """
    from services.answer_sheet_page_registration import (
        failed_registration,
        register_student_page,
    )

    source_path, source_sha256 = _registration_source(page)
    fingerprint = str(session.shared_template_hash or "")
    if not session.shared_template_code or not fingerprint:
        return failed_registration(
            "template_not_published",
            "批次尚未绑定已发布的答题卡模板码，无法配准学生页面",
            template_fingerprint=fingerprint,
            source_sha256=source_sha256,
        )
    matcher = matcher_holder.get("matcher")
    if matcher is None:
        try:
            matcher = await _run_answer_sheet_image_work(
                TemplateEvidenceMatcher, session.layout_artifacts, session.session_dir,
            )
        except (OSError, RuntimeError, ValueError):
            logger.exception("answer-sheet template artifacts failed verification for registration")
            return failed_registration(
                "template_artifacts_invalid",
                "答题卡模板配准资源校验失败，无法配准学生页面",
                template_fingerprint=fingerprint,
                source_sha256=source_sha256,
            )
        matcher_holder["matcher"] = matcher
    return await _run_answer_sheet_image_work(
        register_student_page,
        matcher,
        source_path,
        session.session_dir,
        answer_regions=session.answer_regions,
        identity_regions=session.identity_regions,
        template_fingerprint=fingerprint,
        source_sha256=source_sha256,
    )


def _remove_superseded_registration(
    session: AnswerSheetSession,
    previous: object,
    current: object,
) -> None:
    """Delete a replaced registered image once the new record is saved."""
    from services.answer_sheet_page_registration import registered_image_path

    if not isinstance(previous, dict) or not previous.get("file"):
        return
    if isinstance(current, dict) and current.get("file") == previous.get("file"):
        return
    try:
        registered_image_path(session.session_dir, previous).unlink(missing_ok=True)
    except (OSError, ValueError):
        logger.warning("failed to remove superseded registered answer sheet")
