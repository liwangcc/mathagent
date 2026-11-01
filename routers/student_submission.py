"""Student self-upload: teacher link management and the public student pages.

Teacher endpoints use the normal login.  Student endpoints are public: the
link token selects the class, and every request carries the student's name,
student number and (optional) password, which services/student_submission.py
checks against that class's roster.  Student endpoints never return images.
"""

import asyncio
from contextlib import contextmanager
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from models_user import User
from routers.auth import _require_user
from services import student_submission as submission
from services.capture_session import MAX_IMAGE_BYTES
from services.security import read_upload_capped

router = APIRouter()

_NO_STORE = {"Cache-Control": "no-store"}


@contextmanager
def _http_errors():
    try:
        yield
    except submission.SubmissionError as exc:
        raise HTTPException(exc.status, detail=exc.message) from exc


def _client(request: Request) -> str:
    return request.client.host if request.client else ""


# ── Teacher ──


class CreateLinkRequest(BaseModel):
    class_id: int
    title: str = Field("", max_length=100)
    closes_at: datetime


class UpdateLinkRequest(BaseModel):
    title: str | None = Field(None, max_length=100)
    closes_at: datetime | None = None


class PasswordRequest(BaseModel):
    password: str = Field(..., min_length=1, max_length=64)


@router.get("/submission-links")
def list_submission_links(class_id: int, user: User = Depends(_require_user), db: Session = Depends(get_db)):
    with _http_errors():
        return {"links": submission.list_links(db, user.id, class_id)}


@router.post("/submission-links")
def create_submission_link(req: CreateLinkRequest, user: User = Depends(_require_user), db: Session = Depends(get_db)):
    with _http_errors():
        return submission.create_link(db, user.id, req.class_id, req.title, req.closes_at)


@router.put("/submission-links/{link_id}")
def update_submission_link(
    link_id: int, req: UpdateLinkRequest,
    user: User = Depends(_require_user), db: Session = Depends(get_db),
):
    with _http_errors():
        return submission.update_link(db, user.id, link_id, title=req.title, closes_at=req.closes_at)


@router.delete("/submission-links/{link_id}")
def delete_submission_link(link_id: int, user: User = Depends(_require_user), db: Session = Depends(get_db)):
    with _http_errors():
        submission.delete_link(db, user.id, link_id)
    return {"ok": True}


@router.get("/submission-links/{link_id}/submissions")
def submission_link_status(link_id: int, user: User = Depends(_require_user), db: Session = Depends(get_db)):
    with _http_errors():
        return submission.link_submissions(db, user.id, link_id)


@router.get("/submission-links/{link_id}/pages/{page_id}/image")
def submission_page_image(
    link_id: int, page_id: int,
    user: User = Depends(_require_user), db: Session = Depends(get_db),
):
    with _http_errors():
        path, media_type = submission.teacher_page_file(db, user.id, link_id, page_id)
    return FileResponse(path, media_type=media_type, headers=_NO_STORE)


@router.delete("/submission-links/{link_id}/students/{student_id}")
def clear_submission_student(
    link_id: int, student_id: int,
    user: User = Depends(_require_user), db: Session = Depends(get_db),
):
    with _http_errors():
        submission.clear_student_pages(db, user.id, link_id, student_id)
    return {"ok": True}


@router.get("/class/{class_id}/students/{student_id}/submit-password")
def get_submit_password(
    class_id: int, student_id: int,
    user: User = Depends(_require_user), db: Session = Depends(get_db),
):
    with _http_errors():
        return submission.password_state(db, user.id, class_id, student_id)


@router.put("/class/{class_id}/students/{student_id}/submit-password")
def set_submit_password(
    class_id: int, student_id: int, req: PasswordRequest,
    user: User = Depends(_require_user), db: Session = Depends(get_db),
):
    with _http_errors():
        return submission.set_password(db, user.id, class_id, student_id, req.password)


@router.delete("/class/{class_id}/students/{student_id}/submit-password")
def clear_submit_password(
    class_id: int, student_id: int,
    user: User = Depends(_require_user), db: Session = Depends(get_db),
):
    with _http_errors():
        return submission.clear_password(db, user.id, class_id, student_id)


# ── Students (public) ──


class StudentIdentity(BaseModel):
    name: str = Field(..., max_length=50)
    student_no: str = Field(..., max_length=50)
    password: str = Field("", max_length=64)


class MovePageRequest(StudentIdentity):
    to_page_no: int


def _authorize(db: Session, token: str, identity: StudentIdentity, request: Request):
    return submission.authorize(
        db, token,
        name=identity.name, student_no=identity.student_no, password=identity.password,
        client=_client(request),
    )


@router.get("/submit/{token}")
def submission_link_info(token: str, db: Session = Depends(get_db)):
    with _http_errors():
        return submission.public_link(db, token)


@router.post("/submit/{token}/status")
def student_submission_status(token: str, req: StudentIdentity, request: Request, db: Session = Depends(get_db)):
    with _http_errors():
        link, student = _authorize(db, token, req, request)
        return submission.student_status(db, link, student)


@router.post("/submit/{token}/pages")
async def student_upload_page(
    token: str,
    request: Request,
    file: UploadFile = File(...),
    page_no: int = Form(...),
    name: str = Form(..., max_length=50),
    student_no: str = Form(..., max_length=50),
    password: str = Form("", max_length=64),
    db: Session = Depends(get_db),
):
    data = await read_upload_capped(file, MAX_IMAGE_BYTES)
    identity = StudentIdentity(name=name, student_no=student_no, password=password)

    def store():
        # bcrypt, Pillow and file writes stay off the event loop.
        with _http_errors():
            link, student = _authorize(db, token, identity, request)
            return submission.upload_page(db, link, student, page_no, data)

    return await asyncio.to_thread(store)


@router.post("/submit/{token}/pages/{page_no}/delete")
def student_delete_page(
    token: str, page_no: int, req: StudentIdentity, request: Request,
    db: Session = Depends(get_db),
):
    with _http_errors():
        link, student = _authorize(db, token, req, request)
        return submission.delete_page(db, link, student, page_no)


@router.post("/submit/{token}/pages/{page_no}/move")
def student_move_page(
    token: str, page_no: int, req: MovePageRequest, request: Request,
    db: Session = Depends(get_db),
):
    with _http_errors():
        link, student = _authorize(db, token, req, request)
        return submission.move_page(db, link, student, page_no, req.to_page_no)
