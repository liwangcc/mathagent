"""Saving and freezing the answer-sheet layout, the last two routes of the router.

- PUT /answer-sheet/sessions/{session_id}/layout (``save_layout``,
  ``LayoutSaveRequest``) saves the teacher's answer and identity regions.
- POST .../layout/freeze (``freeze_layout``) builds each page's template
  artifact from those regions, adds its column layout and crop plan, checks
  the accelerator region plan and freezes the layout for student recognition.

They come after grading in the original route order, so they live apart from
answer_sheet_layout and are included last.

Moved unchanged from routers/answer_sheet.py, which still exposes these names
and includes this module's router.
Annotations are evaluated when this module is imported (no ``from __future__
import annotations``): FastAPI resolves a decorated route's string annotations
in the decorator's module, so routes here carry real types.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from models_user import User
from routers.answer_sheet_common import (
    _load_owned,
    _requires_answer_sheet_writes,
    _serialized_session_mutation,
)
from routers.auth import _require_user
from services.answer_sheet_acceleration import build_answer_sheet_acceleration_plan
from services.template_anchor_crops import AnchorCropPlanError, build_anchor_crop_plan
from services.template_column_layout import TemplateCutError, recognize_column_layout
from services.template_evidence import build_template_artifact

router = APIRouter()


class LayoutSaveRequest(BaseModel):
    answer_regions: list[dict] = Field(default_factory=list, max_length=200)
    identity_regions: list[dict] = Field(default_factory=list, max_length=100)


@router.put("/sessions/{session_id}/layout")
@_requires_answer_sheet_writes
@_serialized_session_mutation
def save_layout(
    session_id: str,
    req: LayoutSaveRequest,
    user: User = Depends(_require_user),
):
    session = _load_owned(session_id, user.id)
    try:
        summary = session.save_layout(req.answer_regions, req.identity_regions)
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return {"session": session.to_dict(public=True), "summary": summary}


@router.post("/sessions/{session_id}/layout/freeze")
@_requires_answer_sheet_writes
@_serialized_session_mutation
async def freeze_layout(session_id: str, user: User = Depends(_require_user)):
    session = _load_owned(session_id, user.id)
    try:
        artifacts: dict[str, dict] = {}
        for page_index, page in enumerate(session.answer_sheet_pages):
            page_id = str(page.get("page_id") or "")
            regions = [item for item in session.answer_regions if item["page_id"] == page_id]
            artifact_questions = [{
                "question_number": item["region_id"],
                "question_type": (
                    "choice" if item["region_type"].startswith("choice_") else item["region_type"]
                ),
                "answer_bbox": item["bbox"],
            } for item in regions]
            active_path = page.get("working_path") or page["path"]
            artifacts[page_id] = build_template_artifact(
                active_path, session.session_dir, page_index, artifact_questions,
                blank_detection_enabled=True,
                canonicalization=page.get("canonicalization"),
            )
            expected_question_numbers = [
                str(region.get("question_no") or "").strip()
                for region in session.answer_regions
                if region.get("page_id") == page_id
                and str(region.get("question_no") or "").strip()
            ]
            localization = (
                session.layout_localization
                if isinstance(session.layout_localization, dict)
                else {}
            )
            localized_pages = localization.get("pages")
            localized_page = next((
                item for item in (localized_pages or [])
                if isinstance(item, dict) and item.get("page_id") == page_id
            ), None)
            if (
                localization.get("provider_route") == "qwen3.8max"
                and isinstance(localized_page, dict)
                and isinstance(localized_page.get("column_layout"), dict)
            ):
                # The first-time rough localization already paid for a Qwen
                # page-layout proposal. Reuse that checked proposal when
                # freezing instead of opening a second model request.
                layout_result = {
                    "column_layout": localized_page["column_layout"],
                    "source": {
                        "sha256": artifacts[page_id]["sha256"],
                        "width": artifacts[page_id]["width"],
                        "height": artifacts[page_id]["height"],
                    },
                }
            else:
                layout_result = await recognize_column_layout(
                    Path(session.session_dir)
                    / artifacts[page_id]["template_image"],
                    teacher_id=str(user.id),
                    # Compatibility path for manually bound historical
                    # sessions that predate Qwen answer-sheet localization.
                    column_mode="triple",
                )
            artifacts[page_id]["question_crop_plan"] = build_anchor_crop_plan(
                layout_result,
                artifact_sha256=artifacts[page_id]["sha256"],
                expected_question_numbers=expected_question_numbers,
            )
        # Validate the same immutable contract used when student jobs are
        # enqueued, while the teacher can still correct a crossing region.
        build_answer_sheet_acceleration_plan(
            artifacts,
            session.identity_regions,
            session.answer_regions,
        )
        session.layout_artifacts = artifacts
        session.freeze_layout()
    except TemplateCutError as exc:
        status_code = 503 if exc.retryable or exc.code in {
            "CREDENTIAL_UNAVAILABLE",
            "PROVIDER_BUSY",
        } else 502
        raise HTTPException(
            status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except AnchorCropPlanError as exc:
        raise HTTPException(
            409,
            detail="答题卡题目分栏计划生成失败，请检查模板版面后重试",
        ) from exc
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return session.to_dict(public=True)
