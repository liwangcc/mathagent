"""One student page for the teacher: its image, its transcription evidence, its orientation.

- GET .../students/{paper_id}/image (``student_page_image``) returns the
  immutable, teacher-owned page.
- GET .../students/{paper_id}/evidence (``student_page_evidence_index``) lists
  the transcription-evidence manifests attached to the page, and
  GET .../evidence/{question_no}/{file_name} (``student_page_evidence_file``)
  returns one integrity-checked file (``_EVIDENCE_MEDIA_TYPES``).
- POST .../students/{paper_id}/orientation (``confirm_student_orientation``,
  ``OrientationConfirmRequest``) records the teacher's quarter turns.

Moved unchanged from routers/answer_sheet.py, which still exposes these names
and includes this module's router before its own routes.
Annotations are evaluated when this module is imported (no ``from __future__
import annotations``): FastAPI resolves a decorated route's string annotations
in the decorator's module, so routes here carry real types.
"""

import copy
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from models_user import User
from routers.answer_sheet_assets import (
    _require_raw_assets_available,
    _session_asset_path,
)
from routers.answer_sheet_common import (
    _load_owned,
    _requires_answer_sheet_writes,
    _run_answer_sheet_image_work,
    _serialized_session_mutation,
)
from routers.answer_sheet_student_state import _require_no_student_upload
from routers.auth import _require_user
from services.paper_orientation import create_teacher_oriented_copy, sha256_file

router = APIRouter()


class OrientationConfirmRequest(BaseModel):
    quarter_turns_clockwise: int = Field(..., ge=0, le=3)


@router.get("/sessions/{session_id}/students/{paper_id}/image")
def student_page_image(
    session_id: str,
    paper_id: str,
    user: User = Depends(_require_user),
):
    """Return one immutable, teacher-owned student answer-sheet page."""
    session = _load_owned(session_id, user.id)
    _require_raw_assets_available(session)
    page = next(
        (item for item in session.student_pages if item.get("paper_id") == paper_id),
        None,
    )
    root = Path(session.session_dir).resolve()
    image_path = _session_asset_path(
        root,
        (page or {}).get("path"),
        relative=False,
    )
    if not page or image_path is None or not image_path.is_file():
        raise HTTPException(404, detail="学生答题卡页面不存在")
    return FileResponse(
        image_path,
        headers={"Cache-Control": "private, no-store"},
    )


_EVIDENCE_MEDIA_TYPES = {
    ".png": "image/png",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
}


@router.get("/sessions/{session_id}/students/{paper_id}/evidence")
def student_page_evidence_index(
    session_id: str,
    paper_id: str,
    user: User = Depends(_require_user),
):
    """List transcription-evidence manifests recorded for one student page.

    Teacher-owned read-only view: only manifests already attached to the
    page's recognized answers are exposed; file bytes stay behind the
    integrity-checked route below.
    """

    session = _load_owned(session_id, user.id)
    _require_raw_assets_available(session)
    page = next(
        (item for item in session.student_pages if item.get("paper_id") == paper_id),
        None,
    )
    if not page:
        raise HTTPException(404, detail="学生答题卡页面不存在")
    grouped: dict[str, dict] = {}
    for answer in page.get("answers") or []:
        transcription = answer.get("transcription") if isinstance(answer.get("transcription"), dict) else None
        evidence = (transcription or {}).get("evidence")
        directory = evidence.get("directory") if isinstance(evidence, dict) else None
        if not isinstance(directory, str) or not directory:
            continue
        entry = grouped.setdefault(
            directory,
            {
                "directory": directory,
                "question_nos": [],
                "files": [
                    {"file": item.get("file"), "kind": item.get("kind"), "bytes": item.get("bytes")}
                    for item in evidence.get("files") or []
                    if isinstance(item, dict) and item.get("file")
                ],
            },
        )
        question_no = str(answer.get("question_no", ""))
        if question_no and question_no not in entry["question_nos"]:
            entry["question_nos"].append(question_no)
    return {
        "session_id": session.session_id,
        "paper_id": paper_id,
        "records": list(grouped.values()),
    }


@router.get("/sessions/{session_id}/students/{paper_id}/evidence/{question_no}/{file_name}")
def student_page_evidence_file(
    session_id: str,
    paper_id: str,
    question_no: str,
    file_name: str,
    user: User = Depends(_require_user),
):
    """One integrity-checked transcription-evidence file for teacher review."""

    session = _load_owned(session_id, user.id)
    _require_raw_assets_available(session)
    page = next(
        (item for item in session.student_pages if item.get("paper_id") == paper_id),
        None,
    )
    if not page:
        raise HTTPException(404, detail="学生答题卡页面不存在")
    answer = next(
        (
            item
            for item in page.get("answers") or []
            if str(item.get("question_no", "")) == question_no
        ),
        None,
    )
    transcription = (answer or {}).get("transcription")
    record = transcription if isinstance(transcription, dict) else None
    if not record or not isinstance(record.get("evidence"), dict):
        raise HTTPException(404, detail="转录证据不存在")
    from services.answer_sheet_column_transcription import read_evidence_file

    try:
        data = read_evidence_file(session.session_dir, record, file_name)
    except ValueError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    suffix = f".{file_name.rsplit('.', 1)[-1].lower()}" if "." in file_name else ""
    media_type = _EVIDENCE_MEDIA_TYPES.get(suffix, "application/octet-stream")
    return Response(
        content=data,
        media_type=media_type,
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/sessions/{session_id}/students/{paper_id}/orientation")
@_requires_answer_sheet_writes
@_serialized_session_mutation
async def confirm_student_orientation(
    session_id: str, paper_id: str, req: OrientationConfirmRequest,
    user: User = Depends(_require_user),
):
    session = _load_owned(session_id, user.id)
    _require_no_student_upload(session)
    page = next((item for item in session.student_pages if item.get("paper_id") == paper_id), None)
    if not page:
        raise HTTPException(404, detail="学生答题卡页面不存在")
    source_hash = await _run_answer_sheet_image_work(sha256_file, page["path"])
    try:
        copy = await _run_answer_sheet_image_work(
            create_teacher_oriented_copy,
            page["path"], os.path.join(session.session_dir, "oriented"), paper_id,
            req.quarter_turns_clockwise,
        )
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    page["working_path"] = copy["image_path"]
    page["orientation_working_path"] = copy["image_path"]
    page["orientation_status"] = "teacher_confirmed"
    page["orientation_reason"] = "teacher_confirmed_rotation"
    page["manual_quarter_turns"] = req.quarter_turns_clockwise
    page["orientation_source_sha256"] = source_hash
    page["orientation_working_sha256"] = copy["working_sha256"]
    page["orientation_confirmed_by"] = user.id
    session.invalidate_grading_drafts()
    session.save()
    return session.to_dict(public=True)
