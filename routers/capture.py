"""Phone capture: a teacher's phone photographs papers for the desktop grading page.

Teacher endpoints use the normal login.  Phone endpoints use only the
session-scoped ``X-Capture-Token`` from the QR code; they reply 403/410 instead
of 401 so the phone page never falls into the login redirect.
"""

import asyncio
from contextlib import contextmanager

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse

from models_user import User
from routers.auth import _require_user
from services import capture_session
from services.security import read_upload_capped
from services.teaching_context import require_teacher_stage

router = APIRouter()

_NO_STORE = {"Cache-Control": "no-store"}
_STATUS_CODES = {
    capture_session.CaptureInvalidImage: 400,
    capture_session.CaptureForbidden: 403,
    capture_session.CaptureNotFound: 404,
    capture_session.CaptureFull: 409,
    capture_session.CaptureBusy: 503,
}


@contextmanager
def _http_errors(*, phone: bool = False):
    """Report capture errors as HTTP errors with their teacher-facing message.

    The phone gets 410 instead of 404: the session or photo it asked about is
    gone for good, so it stops instead of retrying.
    """
    try:
        yield
    except capture_session.CaptureError as exc:
        status = _STATUS_CODES.get(type(exc), 503)
        if phone and status == 404:
            status = 410
        raise HTTPException(status, detail=exc.message) from exc


@router.post("/capture/sessions")
def open_capture_session(user: User = Depends(_require_user)):
    require_teacher_stage(user)
    with _http_errors():
        session, token = capture_session.open_session(user.id)
    return {**session, "token": token}


@router.get("/capture/sessions/{session_id}")
def get_capture_session(session_id: str, user: User = Depends(_require_user)):
    with _http_errors():
        return capture_session.get_teacher_session(session_id, user.id)


@router.get("/capture/sessions/{session_id}/images/{seq}")
def get_capture_image(session_id: str, seq: int, user: User = Depends(_require_user)):
    with _http_errors():
        path, media_type = capture_session.teacher_image_path(session_id, user.id, seq)
    return FileResponse(path, media_type=media_type, headers=_NO_STORE)


@router.delete("/capture/sessions/{session_id}/images/{seq}")
def delete_capture_image(session_id: str, seq: int, user: User = Depends(_require_user)):
    with _http_errors():
        return capture_session.teacher_delete_image(session_id, user.id, seq)


@router.delete("/capture/sessions/{session_id}")
def close_capture_session(session_id: str, user: User = Depends(_require_user)):
    with _http_errors():
        capture_session.close_session(session_id, user.id)
    return {"ok": True}


@router.get("/capture/phone/{session_id}")
def phone_capture_state(
    session_id: str,
    token: str = Header("", alias="X-Capture-Token"),
):
    with _http_errors(phone=True):
        return capture_session.phone_state(session_id, token)


@router.post("/capture/phone/{session_id}/images")
async def phone_capture_upload(
    session_id: str,
    file: UploadFile = File(...),
    client_id: str = Form(""),
    token: str = Header("", alias="X-Capture-Token"),
):
    data = await read_upload_capped(file, capture_session.MAX_IMAGE_BYTES)
    with _http_errors(phone=True):
        return await asyncio.to_thread(capture_session.add_image, session_id, token, data, client_id)


@router.delete("/capture/phone/{session_id}/images/{seq}")
def phone_capture_delete(
    session_id: str,
    seq: int,
    token: str = Header("", alias="X-Capture-Token"),
):
    with _http_errors(phone=True):
        return capture_session.phone_delete_image(session_id, token, seq)
