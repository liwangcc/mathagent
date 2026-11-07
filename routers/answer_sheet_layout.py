"""The blank answer sheet of a session: its pages, located regions, shared template code and bubble grids.

- POST /answer-sheet/sessions/{session_id}/layout/upload (``upload_layout``)
  stores the blank answer-sheet pages, with the client's canonical candidates
  when it sends them; POST .../layout/process (``process_layout_acceleration``)
  resumes their canonicalization jobs without a re-upload; GET
  .../layout/pages/{page_id}/image (``layout_image``) serves one page.
- POST .../layout/locate (``locate_layout_regions``) asks the model for
  editable, never-authoritative region proposals.
- POST .../layout/templates/use applies an immutable shared template code
  without any model call; POST .../layout/templates/publish publishes the
  frozen, teacher-confirmed layout as one (``TemplateCodeRequest``).
- POST .../layout/bubble-grids/confirm and .../discard record or remove the
  teacher's choice-bubble grid rows (the ``BubbleGrid*`` request bodies).

Saving and freezing the layout are the last two routes of the router, so they
live in answer_sheet_layout_freeze, included last.

Moved unchanged from routers/answer_sheet.py, which still exposes these names
and includes this module's router before its own routes.
Annotations are evaluated when this module is imported (no ``from __future__
import annotations``): FastAPI resolves a decorated route's string annotations
in the decorator's module, so routes here carry real types.
"""

import asyncio
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from config import MAX_TEMPLATE_PAGES, MAX_UPLOAD_TOTAL_SIZE
from models_user import User
from routers.answer_sheet_assets import (
    _remove_layout_assets,
    _require_raw_assets_available,
    _save_images,
)
from routers.answer_sheet_common import (
    _load_owned,
    _requires_answer_sheet_writes,
    _run_answer_sheet_image_work,
    _serialized_session_mutation,
)
from routers.answer_sheet_sessions import _batch_label_binding
from routers.auth import _require_user
from services.accelerator_business import (
    adopt_accelerated_template_canonicalization,
    resume_accelerated_template_canonicalization,
    try_accelerated_template_canonicalization,
)
from services.answer_sheet_layout_contract import choice_row_keys
from services.answer_sheet_template_locator import locate_answer_sheet_template
from services.answer_sheet_template_registry import (
    AnswerSheetTemplateIncompatible,
    AnswerSheetTemplateNotFound,
    get_answer_sheet_template_registry,
    normalize_template_code,
)
from services.template_evidence import (
    accept_canonical_template_candidate,
    parse_canonical_metadata,
)

# routers.answer_sheet's logger, so these records keep their logger name.
logger = logging.getLogger("routers.answer_sheet")
router = APIRouter()


class TemplateCodeRequest(BaseModel):
    template_code: str = Field(..., min_length=8, max_length=40)


class BubbleGridCell(BaseModel):
    model_config = ConfigDict(extra="forbid")
    option: str = Field(..., min_length=1, max_length=1)
    bbox: list[int] = Field(..., min_length=4, max_length=4)


class BubbleGridKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question_no: str = Field(..., min_length=1, max_length=20)
    subquestion_no: str = Field("", max_length=4)


class BubbleGridConfirmation(BubbleGridKey):
    # Who and when are stamped by the server; a request carrying
    # confirmation state or provenance is rejected, not trusted.
    grid_order: Literal["left_to_right", "top_to_bottom"] = "left_to_right"
    cells: list[BubbleGridCell] = Field(..., min_length=2, max_length=8)


class BubbleGridConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grids: list[BubbleGridConfirmation] = Field(..., min_length=1, max_length=200)


class BubbleGridDiscardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keys: list[BubbleGridKey] = Field(..., min_length=1, max_length=200)


@router.post("/sessions/{session_id}/layout/upload")
@_requires_answer_sheet_writes
@_serialized_session_mutation
async def upload_layout(
    session_id: str,
    files: list[UploadFile] = File(...),
    canonical_files: list[UploadFile] | None = File(None),
    canonical_metadata: str | None = Form(None),
    user: User = Depends(_require_user),
):
    session = _load_owned(session_id, user.id)
    if session.status not in {"answers_confirmed", "layout_draft"}:
        raise HTTPException(409, detail="请先确认题目与答案")
    previous_pages = list(session.answer_sheet_pages)
    previous_pending_pages = list(session.pending_layout_pages)
    previous_artifacts = dict(session.layout_artifacts)
    has_active_layout = bool(previous_pages) and all(
        page.get("working_path") and isinstance(page.get("canonicalization"), dict)
        for page in previous_pages
    )
    has_canonical_files = canonical_files is not None
    has_canonical_metadata = canonical_metadata is not None
    if has_canonical_files != has_canonical_metadata:
        raise HTTPException(
            422,
            detail="canonical_files 与 canonical_metadata 必须同时提交",
        )
    if len(files) > MAX_TEMPLATE_PAGES:
        raise HTTPException(
            413,
            detail=f"空白答题卡最多上传 {MAX_TEMPLATE_PAGES} 页原图",
        )
    submitted_metadata = None
    if has_canonical_files:
        if len(canonical_files) != len(files):
            raise HTTPException(422, detail="标准模板候选数量必须与原图一致")
        if any(
            Path(upload.filename or "").suffix.lower() != ".png"
            for upload in canonical_files
        ):
            raise HTTPException(422, detail="标准模板候选必须为 PNG")
        try:
            submitted_metadata = parse_canonical_metadata(
                canonical_metadata,
                len(files),
            )
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
    layout_dir = os.path.join(session.session_dir, "layout")
    storage_namespace = f"layout_canonical_{uuid.uuid4().hex}"
    candidate_dir = os.path.join(
        session.session_dir,
        f"layout_candidates_{uuid.uuid4().hex}",
    )
    pages: list[dict] = []
    candidate_pages: list[dict] = []
    accelerator_states: list[dict] = []
    try:
        pages = await _save_images(files, layout_dir, "layout")
        if submitted_metadata is not None:
            candidate_pages = await _save_images(
                canonical_files,
                candidate_dir,
                "canonical",
                total_byte_budget=(
                    MAX_UPLOAD_TOTAL_SIZE
                    - sum(os.path.getsize(page["path"]) for page in pages)
                ),
            )
        for page_index, page in enumerate(pages):
            if submitted_metadata is not None:
                canonical = await _run_answer_sheet_image_work(
                    accept_canonical_template_candidate,
                    page["path"],
                    candidate_pages[page_index]["path"],
                    submitted_metadata[page_index],
                    session.session_dir,
                    page_index,
                    storage_namespace=storage_namespace,
                )
            else:
                accelerated = await try_accelerated_template_canonicalization(
                    teacher_id=user.id,
                    operation_id=(
                        f"answer-sheet:{session.session_id}:layout:"
                        f"{page_index}:{page['sha256'][:16]}"
                    ),
                    source_path=page["path"],
                    wait_seconds=0,
                )
                if accelerated.state != "ready":
                    checkpoint = {
                        "state": str(accelerated.state or "fault"),
                        "reason_code": str(accelerated.reason_code or ""),
                        "job_id": str(accelerated.job_id or ""),
                        "source_sha256": page["sha256"],
                        "storage_namespace": storage_namespace,
                    }
                    page["canonicalization_accelerator"] = checkpoint
                    accelerator_states.append(checkpoint)
                    continue
                adopted = await asyncio.to_thread(
                    adopt_accelerated_template_canonicalization,
                    accelerated,
                    teacher_id=user.id,
                    template_dir=session.session_dir,
                    page_index=page_index,
                    storage_namespace=storage_namespace,
                )
                canonical_path = str(adopted.canonical_path or "")
                canonicalization = dict(adopted.canonicalization or {})
            if submitted_metadata is not None:
                canonical_path = canonical.image_path
                canonicalization = canonical.canonicalization
            page["working_path"] = canonical_path
            page["working_sha256"] = canonicalization[
                "canonical_sha256"
            ]
            page["canonicalization"] = canonicalization
    except ValueError as exc:
        for page in pages:
            try:
                os.remove(page["path"])
            except OSError:
                pass
        shutil.rmtree(
            os.path.join(session.session_dir, storage_namespace),
            ignore_errors=True,
        )
        raise HTTPException(
            422,
            detail=(
                "客户端标准模板候选校验失败"
                if submitted_metadata is not None
                else "答题卡页面未能完整拉正，请使用边界完整、方向正确且清晰的图片重新上传"
            ),
        ) from exc
    except BaseException:
        for page in pages:
            try:
                os.remove(page["path"])
            except OSError:
                pass
        shutil.rmtree(
            os.path.join(session.session_dir, storage_namespace),
            ignore_errors=True,
        )
        raise
    finally:
        shutil.rmtree(candidate_dir, ignore_errors=True)
    try:
        if accelerator_states:
            rank = {"pending": 0, "unavailable": 1, "fault": 2, "failed": 2}
            worst = max(
                accelerator_states,
                key=lambda item: rank.get(str(item.get("state") or "fault"), 2),
            )
            raw_state = str(worst.get("state") or "fault")
            public_state = {
                "pending": "waiting_accelerator",
                "unavailable": "unavailable",
                "failed": "fault",
                "fault": "fault",
            }.get(raw_state, "fault")
            messages = {
                "waiting_accelerator": "空白答题卡已保存，正在等待客户端或机会式加速节点处理页面。",
                "unavailable": "空白答题卡已保存，但当前没有可用加速节点；服务器不会回退处理图像。",
                "fault": "空白答题卡已保存，但加速图像处理发生故障；服务器不会回退处理图像。",
            }
            session.asset_lifecycle["layout_acceleration"] = {
                "status": public_state,
                "reason_code": str(worst.get("reason_code") or ""),
                "page_count": len(pages),
                "ready_count": len(pages) - len(accelerator_states),
            }
            session.pending_layout_pages = pages
            if not has_active_layout:
                session.answer_sheet_pages = []
                session.answer_regions = []
                session.identity_regions = []
                session.layout_hash = ""
                session.layout_artifacts = {}
                session.status = "answers_confirmed"
            else:
                session.status = "layout_draft"
            session.error = messages[public_state]
        else:
            session.answer_sheet_pages = pages
            session.pending_layout_pages = []
            session.answer_regions = []
            session.identity_regions = []
            session.layout_hash = ""
            session.layout_artifacts = {}
            session.layout_localization = {}
            session.shared_template_code = ""
            session.shared_template_hash = ""
            session.asset_lifecycle.pop("layout_acceleration", None)
            session.status = "layout_draft"
            session.error = None
        session.save()
    except Exception:
        _remove_layout_assets(session.session_dir, pages, {})
        raise
    _remove_layout_assets(
        session.session_dir,
        previous_pending_pages,
        {},
        preserve_pages=pages,
    )
    if not accelerator_states:
        _remove_layout_assets(
            session.session_dir,
            previous_pages,
            previous_artifacts,
            preserve_pages=pages,
        )
    elif not has_active_layout:
        _remove_layout_assets(
            session.session_dir,
            previous_pages,
            previous_artifacts,
            preserve_pages=pages,
        )
    payload = session.to_dict(public=True)
    accelerator = session.asset_lifecycle.get("layout_acceleration")
    if isinstance(accelerator, dict):
        payload["accelerator"] = {
            "status": accelerator.get("status"),
            "reason_code": accelerator.get("reason_code"),
            "message": session.error,
        }
        return JSONResponse(status_code=202, content=payload)
    return payload


@router.post("/sessions/{session_id}/layout/process")
@_requires_answer_sheet_writes
@_serialized_session_mutation
async def process_layout_acceleration(
    session_id: str,
    user: User = Depends(_require_user),
):
    """Resume durable canonicalization jobs without re-uploading the pages."""
    session = _load_owned(session_id, user.id)
    if session.status not in {"answers_confirmed", "layout_draft"}:
        raise HTTPException(409, detail="当前答题卡版面状态不能继续处理")
    active_pages = list(session.answer_sheet_pages)
    pending_pages = list(session.pending_layout_pages)
    legacy_pending = (
        not pending_pages
        and any(
            isinstance(page.get("canonicalization_accelerator"), dict)
            for page in active_pages
        )
    )
    pages = pending_pages or (active_pages if legacy_pending else [])
    if not pages:
        raise HTTPException(409, detail="请先上传空白答题卡")

    remaining: list[dict] = []
    for page_index, page in enumerate(pages):
        if page.get("working_path") and isinstance(page.get("canonicalization"), dict):
            continue
        checkpoint = page.get("canonicalization_accelerator")
        if not isinstance(checkpoint, dict):
            remaining.append({
                "state": "fault",
                "reason_code": "canonicalization_checkpoint_missing",
            })
            continue
        if checkpoint.get("source_sha256") != page.get("sha256"):
            checkpoint.update(
                state="fault",
                reason_code="canonicalization_source_changed",
            )
            remaining.append(checkpoint)
            continue
        job_id = str(checkpoint.get("job_id") or "")
        if not job_id:
            checkpoint.update(
                state="unavailable",
                reason_code="canonicalization_job_missing",
            )
            remaining.append(checkpoint)
            continue
        accelerated = await resume_accelerated_template_canonicalization(
            teacher_id=user.id,
            job_id=job_id,
        )
        if accelerated.state != "ready":
            checkpoint.update(
                state=str(accelerated.state or "fault"),
                reason_code=str(accelerated.reason_code or ""),
            )
            remaining.append(checkpoint)
            continue
        try:
            adopted = await asyncio.to_thread(
                adopt_accelerated_template_canonicalization,
                accelerated,
                teacher_id=user.id,
                template_dir=session.session_dir,
                page_index=page_index,
                storage_namespace=str(
                    checkpoint.get("storage_namespace") or "layout_canonical"
                ),
            )
        except (OSError, PermissionError, ValueError) as exc:
            logger.warning(
                "answer-sheet canonicalization adoption failed page=%s: %s",
                page_index,
                exc,
            )
            checkpoint.update(
                state="fault",
                reason_code="canonicalization_adoption_failed",
            )
            remaining.append(checkpoint)
            continue
        page["working_path"] = str(adopted.canonical_path or "")
        page["canonicalization"] = dict(adopted.canonicalization or {})
        page["working_sha256"] = page["canonicalization"].get(
            "canonical_sha256", ""
        )
        page.pop("canonicalization_accelerator", None)

    if remaining:
        rank = {"pending": 0, "unavailable": 1, "fault": 2, "failed": 2}
        worst = max(
            remaining,
            key=lambda item: rank.get(str(item.get("state") or "fault"), 2),
        )
        public_state = {
            "pending": "waiting_accelerator",
            "unavailable": "unavailable",
            "failed": "fault",
            "fault": "fault",
        }.get(str(worst.get("state") or "fault"), "fault")
        message = {
            "waiting_accelerator": "空白答题卡已保存，正在等待客户端或机会式加速节点处理页面。",
            "unavailable": "当前没有可用加速节点；已保存页面，服务器不会回退处理图像。",
            "fault": "加速图像处理发生故障；已保存页面，服务器不会回退处理图像。",
        }[public_state]
        session.asset_lifecycle["layout_acceleration"] = {
            "status": public_state,
            "reason_code": str(worst.get("reason_code") or ""),
            "page_count": len(pages),
            "ready_count": len(pages) - len(remaining),
        }
        if pending_pages:
            session.pending_layout_pages = pages
            session.status = "layout_draft" if active_pages else "answers_confirmed"
        else:
            session.answer_sheet_pages = pages
            session.status = "answers_confirmed"
        session.error = message
    else:
        previous_pages = active_pages if pending_pages else []
        previous_artifacts = dict(session.layout_artifacts) if pending_pages else {}
        session.answer_sheet_pages = pages
        session.pending_layout_pages = []
        session.answer_regions = []
        session.identity_regions = []
        session.layout_hash = ""
        session.layout_artifacts = {}
        session.layout_localization = {}
        session.shared_template_code = ""
        session.shared_template_hash = ""
        session.asset_lifecycle.pop("layout_acceleration", None)
        session.status = "layout_draft"
        session.error = None
    await asyncio.to_thread(session.save)
    if not remaining and previous_pages:
        _remove_layout_assets(
            session.session_dir,
            previous_pages,
            previous_artifacts,
            preserve_pages=pages,
        )
    payload = session.to_dict(public=True)
    accelerator = session.asset_lifecycle.get("layout_acceleration")
    if isinstance(accelerator, dict):
        payload["accelerator"] = {
            "status": accelerator.get("status"),
            "reason_code": accelerator.get("reason_code"),
            "message": session.error,
        }
        return JSONResponse(status_code=202, content=payload)
    return payload


@router.get("/sessions/{session_id}/layout/pages/{page_id}/image")
def layout_image(session_id: str, page_id: str, user: User = Depends(_require_user)):
    session = _load_owned(session_id, user.id)
    _require_raw_assets_available(session)
    page = next((item for item in session.answer_sheet_pages if item.get("page_id") == page_id), None)
    active_path = str((page or {}).get("working_path") or (page or {}).get("path") or "")
    if not page or not os.path.isfile(active_path):
        raise HTTPException(404, detail="答题卡页面不存在")
    return FileResponse(active_path)


@router.post("/sessions/{session_id}/layout/locate")
@_requires_answer_sheet_writes
@_serialized_session_mutation
async def locate_layout_regions(
    session_id: str,
    user: User = Depends(_require_user),
):
    """Ask qwen3.8max/high for editable, never-authoritative region proposals."""

    session = _load_owned(session_id, user.id)
    if session.status != "layout_draft" or not session.answer_sheet_pages:
        raise HTTPException(409, detail="请先上传并完成空白答题卡页面矫正")
    try:
        localization = await locate_answer_sheet_template(
            session.answer_sheet_pages,
            session.answer_spec_snapshot,
            teacher_id=user.id,
        )
        specs = {
            str(item.get("question_no") or "").strip(): item
            for item in session.answer_spec_snapshot
        }
        answer_regions: list[dict] = []
        identity_regions: list[dict] = []
        grid_proposals: list[dict] = []
        for page in localization["pages"]:
            page_id = str(page["page_id"])
            for raw in page["answer_regions"]:
                question_no = str(raw["question_no"])
                spec = specs[question_no]
                question_type = str(spec.get("question_type") or "solve")
                if question_type == "choice" and choice_row_keys(spec) == [""]:
                    # The locator frames all option bubbles of a bubble-style
                    # choice question; that box is only a placement proposal.
                    # Small-question rows are left for the teacher to place.
                    grid_proposals.append({
                        "question_no": question_no,
                        "grid_bbox": raw["bbox"],
                        "confidence": raw.get("confidence"),
                    })
                answer_regions.append({
                    "region_id": f"answer_{len(answer_regions) + 1}",
                    "page_id": page_id,
                    "question_no": question_no,
                    "bbox": raw["bbox"],
                    "region_type": (
                        "choice_mark" if question_type == "choice" else question_type
                    ),
                    "reading_order": len(answer_regions) + 1,
                    "required": True,
                    "teacher_confirmed": False,
                    "choice_options": list(spec.get("choice_options") or ["A", "B", "C", "D"]),
                    "allow_multiple": (
                        question_type == "choice"
                        and str(spec.get("choice_mode") or "unknown") == "multiple"
                    ),
                })
            for raw in page["identity_regions"]:
                identity_regions.append({
                    "region_id": f"identity_{len(identity_regions) + 1}",
                    "page_id": page_id,
                    "bbox": raw["bbox"],
                    "identity_type": raw["identity_type"],
                    "teacher_confirmed": False,
                    "config": dict(raw.get("config") or {}),
                })
        summary = session.save_layout(answer_regions, identity_regions)
        session.layout_localization = localization
        if session.is_batch and grid_proposals:
            session.propose_bubble_grids(grid_proposals, proposer={
                "contract_version": localization.get("contract_version"),
                "provider_route": localization.get("provider_route"),
            })
            summary = session.layout_summary()
        session.save()
    except ValueError as exc:
        raise HTTPException(409, detail=f"答题卡粗定位结果需重新处理：{exc}") from exc
    except Exception as exc:
        logger.exception("qwen answer-sheet template localization failed")
        raise HTTPException(
            503,
            detail="qwen3.8max 暂时无法完成答题卡粗定位；已上传页面仍可手工绑定或稍后重试",
        ) from exc
    return {
        "session": session.to_dict(public=True),
        "summary": summary,
        "localization": localization,
    }


@router.post("/sessions/{session_id}/layout/templates/use")
@_requires_answer_sheet_writes
@_serialized_session_mutation
def use_layout_template_code(
    session_id: str,
    req: TemplateCodeRequest,
    user: User = Depends(_require_user),
):
    """Apply an immutable shared template without opening any model route."""

    session = _load_owned(session_id, user.id)
    previous_pages = list(session.answer_sheet_pages)
    previous_artifacts = dict(session.layout_artifacts)
    registry = get_answer_sheet_template_registry()
    try:
        code = normalize_template_code(req.template_code)
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    try:
        # The label is bound in memory first; applying the template persists
        # it together with the frozen layout in a single save.
        with _batch_label_binding(session, code, user.id):
            template = registry.apply_to_session(session, code)
        registry.remember(user.id, template["template_code"])
    except AnswerSheetTemplateNotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except AnswerSheetTemplateIncompatible as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    _remove_layout_assets(
        session.session_dir,
        previous_pages,
        previous_artifacts,
        preserve_pages=session.answer_sheet_pages,
        preserve_artifacts=session.layout_artifacts,
    )
    return {"session": session.to_dict(public=True), "template": template}


@router.post("/sessions/{session_id}/layout/templates/publish")
@_requires_answer_sheet_writes
@_serialized_session_mutation
def publish_layout_template_code(
    session_id: str,
    user: User = Depends(_require_user),
):
    """Publish the frozen teacher-confirmed layout and remember its code."""

    session = _load_owned(session_id, user.id)
    if session.status != "frozen":
        raise HTTPException(409, detail="请先确认并冻结答题卡版面")
    registry = get_answer_sheet_template_registry()
    try:
        template = registry.publish(session, creator_teacher_id=user.id)
        with _batch_label_binding(session, template["template_code"], user.id):
            session.shared_template_code = template["template_code"]
            session.shared_template_hash = str(template.pop("_fingerprint"))
            # The confirmed grids (and who confirmed them) now live in the
            # template; the batch keeps no copy of its own.
            session.bubble_grids = []
            session.freeze_layout()
        registry.remember(user.id, template["template_code"])
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return {"session": session.to_dict(public=True), "template": template}


@router.post("/sessions/{session_id}/layout/bubble-grids/confirm")
@_requires_answer_sheet_writes
@_serialized_session_mutation
def confirm_layout_bubble_grids(
    session_id: str,
    req: BubbleGridConfirmRequest,
    user: User = Depends(_require_user),
):
    """Teacher confirms or adjusts bubble-grid placements with explicit cells."""

    session = _load_owned(session_id, user.id)
    try:
        summary = session.confirm_bubble_grids(
            [item.model_dump() for item in req.grids], teacher_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return {"session": session.to_dict(public=True), "summary": summary}


@router.post("/sessions/{session_id}/layout/bubble-grids/discard")
@_requires_answer_sheet_writes
@_serialized_session_mutation
def discard_layout_bubble_grids(
    session_id: str,
    req: BubbleGridDiscardRequest,
    user: User = Depends(_require_user),
):
    """Teacher removes proposed or confirmed grid rows before freezing."""

    session = _load_owned(session_id, user.id)
    try:
        summary = session.discard_bubble_grids([item.model_dump() for item in req.keys])
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return {"session": session.to_dict(public=True), "summary": summary}
