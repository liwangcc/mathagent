"""Answer-sheet batches and the paper templates they are built from.

- Batches: POST /answer-sheet/sessions (``create_session``) starts a batch for
  the teacher's current class (``_inherited_class_id``, ``_owned_class``) from a
  frozen paper template and, optionally, a shared answer-sheet template code,
  claiming a unique storage label (``_batch_label_binding``); GET and DELETE
  /sessions/... list, read and remove batches.
- Paper templates: list them and their confirmed sources, register a
  teacher-confirmed detailed-grading template for reuse, read one, set its
  alias; GET /templates/history lists the teacher's recent answer-sheet codes.

Moved unchanged from routers/answer_sheet.py, which still exposes these names
and includes this module's router before its own routes.
Annotations are evaluated when this module is imported (no ``from __future__
import annotations``): FastAPI resolves a decorated route's string annotations
in the decorator's module, so routes here carry real types.
"""

import os
import shutil
import uuid
from contextlib import ExitStack, contextmanager

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from config import UPLOAD_DIR
from database import get_db
from models_rate_limit import TeacherClass
from models_user import User
from routers.answer_sheet_availability import _require_answer_sheet_chain
from routers.answer_sheet_common import (
    _load_owned,
    _requires_answer_sheet_writes,
    _serialized_session_mutation,
)
from routers.auth import _require_user
from services.answer_sheet_paper_templates import (
    PaperTemplateNotFound,
    PaperTemplateRegistry,
    get_paper_template_registry,
    list_detailed_template_sources,
)
from services.answer_sheet_session import (
    AnswerSheetOperationBusy,
    AnswerSheetSession,
    BATCH_CONTRACT_VERSION,
    answer_sheet_batch_label_lock,
    list_teacher_batches,
)
from services.answer_sheet_template_registry import (
    AnswerSheetTemplateNotFound,
    get_answer_sheet_template_registry,
    normalize_template_code,
)
from services.teaching_context import make_teaching_context, require_teacher_stage

router = APIRouter()


class SessionCreateRequest(BaseModel):
    # The class is inherited from the teacher's global current class, and the
    # daily/exam grading type is no longer a batch choice.  Rejecting unknown
    # fields makes stale clients fail visibly instead of choosing silently.
    model_config = ConfigDict(extra="forbid")
    paper_template_code: str = Field(..., min_length=8, max_length=40)
    answer_sheet_template_code: str | None = Field(None, min_length=8, max_length=40)


class PaperTemplateRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_id: str = Field(..., min_length=1, max_length=64)
    alias: str = Field("", max_length=40)


class TemplateAliasRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alias: str = Field("", max_length=40)


def _owned_class(db: Session, class_id: int, user_id: int) -> TeacherClass:
    row = db.query(TeacherClass).filter(
        TeacherClass.id == class_id,
        TeacherClass.teacher_id == user_id,
        TeacherClass.deleted_at.is_(None),
    ).first()
    if not row:
        raise HTTPException(404, detail="班级不存在")
    return row


def _inherited_class_id(db: Session, user: User) -> int:
    """Every new batch inherits the teacher's global current class."""
    class_id = getattr(user, "current_class_id", None)
    if class_id is None:
        raise HTTPException(400, detail="请先在顶部导航选择当前班级后再建立批次")
    return _owned_class(db, int(class_id), user.id).id


@contextmanager
def _batch_label_binding(session: AnswerSheetSession, template_code: str, user_id: int):
    """Assign the storage label and keep it unique until the caller saves.

    Historical sessions keep their legacy identity.  The teacher/template
    label lock spans the uniqueness scan and the caller's save, so two
    simultaneous requests cannot both claim the same second; a second batch of
    the same template in the same second is rejected instead of sharing a label.
    """
    if not session.is_batch:
        yield
        return
    with ExitStack() as stack:
        try:
            stack.enter_context(
                answer_sheet_batch_label_lock(UPLOAD_DIR, user_id, template_code)
            )
        except AnswerSheetOperationBusy as exc:
            raise HTTPException(409, detail="同一答题卡模板正在建立批次，请稍后重试") from exc
        already_bound = bool(session.batch_label)
        try:
            label = session.bind_batch_label(template_code)
        except ValueError as exc:
            raise HTTPException(409, detail=str(exc)) from exc
        if not already_bound and any(
            item.get("batch_label") == label and item.get("session_id") != session.session_id
            for item in list_teacher_batches(UPLOAD_DIR, user_id, limit=10_000)
        ):
            session.batch_label = ""
            session.answer_sheet_template_code = ""
            raise HTTPException(409, detail="同一答题卡模板在同一秒内已建立批次，请稍后重试")
        yield


@router.post("/sessions")
@_requires_answer_sheet_writes
async def create_session(
    req: SessionCreateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    context = make_teaching_context(require_teacher_stage(user))
    await _require_answer_sheet_chain(user)
    class_id = _inherited_class_id(db, user)
    paper_registry = get_paper_template_registry()
    try:
        paper = paper_registry.load(req.paper_template_code, user.id)
    except PaperTemplateNotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    if paper["content"].get("teaching_context") != context:
        raise HTTPException(409, detail="试卷模板学段与当前学段不一致，请重新登记试卷模板")
    answer_sheet_code = None
    if req.answer_sheet_template_code:
        try:
            answer_sheet_code = normalize_template_code(req.answer_sheet_template_code)
        except ValueError as exc:
            raise HTTPException(409, detail=str(exc)) from exc

    session = AnswerSheetSession(
        uuid.uuid4().hex[:16], UPLOAD_DIR, teacher_id=user.id, class_id=class_id,
        teaching_context=context,
    )
    session.batch_contract_version = BATCH_CONTRACT_VERSION
    session.bind_paper_template(
        paper["template_code"],
        PaperTemplateRegistry.questions(paper),
        paper["answer_spec_hash"],
    )
    if answer_sheet_code is None:
        session.save()
    else:
        registry = get_answer_sheet_template_registry()
        try:
            with _batch_label_binding(session, answer_sheet_code, user.id):
                registry.apply_to_session(session, answer_sheet_code)
            registry.remember(user.id, answer_sheet_code)
        except BaseException as exc:
            shutil.rmtree(session.session_dir, ignore_errors=True)
            if isinstance(exc, AnswerSheetTemplateNotFound):
                raise HTTPException(404, detail=str(exc)) from exc
            if isinstance(exc, ValueError):
                raise HTTPException(409, detail=str(exc)) from exc
            raise
    paper_registry.remember(user.id, paper["template_code"])
    return session.to_dict(public=True)


@router.get("/sessions")
def list_sessions(user: User = Depends(_require_user)):
    """List the current teacher's batches by their template-code storage label."""
    return {"batches": list_teacher_batches(UPLOAD_DIR, user.id)}


@router.get("/paper-templates")
def list_paper_templates(user: User = Depends(_require_user)):
    """Return the teacher's frozen paper templates (private to the owner)."""
    return {"templates": get_paper_template_registry().list_templates(user.id)}


@router.get("/paper-templates/sources")
def list_paper_template_sources(user: User = Depends(_require_user)):
    """Confirmed detailed-grading templates that can be frozen for reuse."""
    return {"sources": list_detailed_template_sources(UPLOAD_DIR, user.id)}


@router.post("/paper-templates")
@_requires_answer_sheet_writes
def register_paper_template(
    req: PaperTemplateRegisterRequest,
    user: User = Depends(_require_user),
):
    """Freeze a teacher-confirmed detailed-grading template for reuse."""
    from services.template_session import TemplateSession

    try:
        source = TemplateSession.load_meta(req.template_id, UPLOAD_DIR)
    except (OSError, ValueError) as exc:
        raise HTTPException(404, detail="详细批改模板不存在或不属于当前教师") from exc
    if source.teacher_id != user.id or not os.path.isfile(source.meta_path):
        raise HTTPException(404, detail="详细批改模板不存在或不属于当前教师")
    try:
        template = get_paper_template_registry().register(
            source, teacher_id=user.id, alias=req.alias,
        )
    except PaperTemplateNotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return {"template": template}


@router.get("/paper-templates/{template_code}")
def get_paper_template(template_code: str, user: User = Depends(_require_user)):
    """Read-only view of a frozen paper template for teacher review."""
    registry = get_paper_template_registry()
    try:
        manifest = registry.load(template_code, user.id)
    except PaperTemplateNotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    entry = next(
        (
            item for item in registry.list_templates(user.id)
            if item["template_code"] == manifest["template_code"]
        ),
        None,
    )
    return {
        "template": entry or registry.summary(manifest),
        "questions": PaperTemplateRegistry.questions(manifest),
    }


@router.put("/paper-templates/{template_code}/alias")
@_requires_answer_sheet_writes
def set_paper_template_alias(
    template_code: str,
    req: TemplateAliasRequest,
    user: User = Depends(_require_user),
):
    try:
        template = get_paper_template_registry().set_alias(user.id, template_code, req.alias)
    except PaperTemplateNotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return {"template": template}


@router.get("/sessions/{session_id}")
def get_session(session_id: str, user: User = Depends(_require_user)):
    return _load_owned(session_id, user.id).to_dict(public=True)


@router.get("/templates/history")
def template_code_history(user: User = Depends(_require_user)):
    """Return only the current teacher's recently used shared template codes."""

    registry = get_answer_sheet_template_registry()
    return {"templates": registry.history(user.id)}


@router.delete("/sessions/{session_id}")
@_serialized_session_mutation
def delete_session(session_id: str, user: User = Depends(_require_user)):
    session = _load_owned(session_id, user.id)
    try:
        session.delete()
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return {"ok": True}
