"""Where a batch's student papers stand.

- An upload in progress: ``_valid_student_upload_id``, ``_student_upload_is_active``
  (``_STUDENT_UPLOAD_ACTIVE_STATES``) and ``_require_no_student_upload``, which
  refuses other work while one is running.
- ``_student_recognition_source_path`` is the file a student page is recognized
  from: a teacher-oriented copy when there is one, never an earlier aligned output.
- ``_GRADING_DRAFT_CHECKPOINT_KEY`` names the grading-draft checkpoint in the
  session's asset lifecycle, which new or re-recognized papers clear.

Moved unchanged from routers/answer_sheet.py, which still exposes these names.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from services.answer_sheet_session import AnswerSheetSession


_GRADING_DRAFT_CHECKPOINT_KEY = "grading_draft_checkpoint"


_STUDENT_UPLOAD_ACTIVE_STATES = {"uploading", "processing"}


def _valid_student_upload_id(value: object) -> bool:
    text = str(value or "")
    return len(text) == 32 and all(char in "0123456789abcdef" for char in text)


def _student_upload_is_active(session: AnswerSheetSession) -> bool:
    upload = session.student_upload
    return (
        isinstance(upload, dict)
        and upload.get("status") in _STUDENT_UPLOAD_ACTIVE_STATES
        and _valid_student_upload_id(upload.get("upload_id"))
    )


def _require_no_student_upload(session: AnswerSheetSession) -> None:
    if _student_upload_is_active(session):
        raise HTTPException(
            409,
            detail="学生答题卡仍在上传或方向处理中，请先完成或放弃本次上传",
        )


def _student_recognition_source_path(page: dict) -> str:
    """Return an immutable/oriented source, never a previous aligned output."""
    oriented = str(page.get("orientation_working_path") or "").strip()
    if oriented and Path(oriented).name.startswith("teacher-oriented-"):
        return oriented
    working = str(page.get("working_path") or "").strip()
    if working and Path(working).name.startswith("teacher-oriented-"):
        return working
    return str(page["path"])
