"""Recognizing a batch's student pages, and the teacher's review of one result.

- POST /answer-sheet/sessions/{session_id}/students/recognize
  (``recognize_students``) and its SSE form .../students/recognize/stream
  (``recognize_students_stream``) recognize the pending pages in fair bounded
  waves on isolated page copies (``_student_recognition_completion_stream``,
  ``_ANSWER_SHEET_OCR_BATCH_DEADLINE_SECONDS``); each page is published and
  saved before its remote asset is consumed
  (``_persist_student_recognition_result``); when one worker fails,
  ``_StudentRecognitionWorkerError`` carries its isolated page result out.
- POST .../students/{paper_id}/review-confirm (``confirm_student_review``,
  ``ReviewConfirmRequest``) records the teacher's student and answers for one
  page.

Moved unchanged from routers/answer_sheet.py, which still exposes these names
and includes this module's router before its own routes.
Annotations are evaluated when this module is imported (no ``from __future__
import annotations``): FastAPI resolves a decorated route's string annotations
in the decorator's module, so routes here carry real types.
"""

import asyncio
import copy
import logging
import os
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from config import OCR_BATCH_DISPATCH_CONCURRENT, OCR_TEACHER_WAVE_SIZE
from database import get_db
from models import Student
from models_user import User
from routers.answer_sheet_availability import _require_answer_sheet_chain
from routers.answer_sheet_common import (
    _ANSWER_SHEET_HEARTBEAT_SECONDS,
    _answer_sheet_sse,
    _answer_sheet_stream,
    _load_owned,
    _locked_owned,
    _requires_answer_sheet_writes,
    _serialized_session_mutation,
)
from routers.answer_sheet_pairing import _apply_student_page_pairing
from routers.answer_sheet_recognize_page import (
    AnswerSheetAcceleratorState,
    _recognize_one_student_page,
    _student_accelerator_message,
    _student_page_checkpoint_complete,
)
from routers.answer_sheet_student_files import (
    _commit_student_asset_replacement,
    _discard_student_asset_replacement,
    _finish_student_accelerator_replacement,
)
from routers.answer_sheet_student_state import (
    _GRADING_DRAFT_CHECKPOINT_KEY,
    _require_no_student_upload,
)
from routers.auth import _require_user
from services.answer_sheet_session import AnswerSheetSession
from services.bounded_dispatch import teacher_waved_completion_map
from services.ocr_service import (
    reset_vision_batch_deadline_s,
    reset_vision_batch_paper_count,
    reset_vision_phase_key,
    reset_vision_route_ordinal,
    set_vision_batch_deadline_s,
    set_vision_batch_paper_count,
    set_vision_phase_key,
    set_vision_route_ordinal,
)
from services.teaching_context import stream_teaching
from services.template_evidence import TemplateEvidenceMatcher

# routers.answer_sheet's logger, so these records keep their logger name.
logger = logging.getLogger("routers.answer_sheet")
router = APIRouter()


_ANSWER_SHEET_OCR_BATCH_DEADLINE_SECONDS = 600.0


class ReviewConfirmRequest(BaseModel):
    student_id: int = Field(..., ge=1)
    answers: dict[str, str] = Field(default_factory=dict)


class _StudentRecognitionWorkerError(RuntimeError):
    """Carry an isolated page result out of one bounded worker failure."""

    def __init__(self, page_index: int, page: dict, cause: Exception):
        self.page_index = page_index
        self.page = page
        self.cause = cause
        super().__init__(str(cause))


async def _persist_student_recognition_result(
    session: AnswerSheetSession,
    result: dict,
) -> None:
    """Publish and save one isolated page before consuming its remote asset."""

    page_index = int(result["page_index"])
    if not 0 <= page_index < len(session.student_pages):
        raise RuntimeError("student recognition page index is no longer valid")
    previous_page = session.student_pages[page_index]
    replacement = result.get("replacement")
    session.student_pages[page_index] = result["page"]
    try:
        await asyncio.to_thread(session.save)
    except BaseException:
        session.student_pages[page_index] = previous_page
        _discard_student_asset_replacement(session, replacement)
        raise
    _commit_student_asset_replacement(session, replacement)
    await _finish_student_accelerator_replacement(replacement)


async def _student_recognition_completion_stream(
    session: AnswerSheetSession,
    matcher: TemplateEvidenceMatcher | None,
    output_dir: str,
    db: Session,
    roster_rows: list[Student],
):
    """Recognize pending pages in fair bounded waves using isolated page copies."""

    pending = [
        (page_index, page)
        for page_index, page in enumerate(session.student_pages)
        if not _student_page_checkpoint_complete(session, page)
    ]
    if not pending:
        return

    pending_count = len(pending)
    phase_key = f"answer-sheet-students:{uuid.uuid4().hex}"

    async def worker(indexed_page: tuple[int, dict]) -> dict:
        page_index, source_page = indexed_page
        page = copy.deepcopy(source_page)
        phase_token = set_vision_phase_key(
            phase_key,
            retry_scope_key="answer-sheet-students",
        )
        batch_size_token = set_vision_batch_paper_count(pending_count)
        batch_deadline_token = set_vision_batch_deadline_s(
            _ANSWER_SHEET_OCR_BATCH_DEADLINE_SECONDS
        )
        route_token = set_vision_route_ordinal(page_index)
        try:
            replacement = await _recognize_one_student_page(
                session,
                page,
                matcher,
                output_dir,
                db,
                roster_rows,
            )
            return {
                "page_index": page_index,
                "page": page,
                "replacement": replacement,
            }
        except Exception as exc:
            raise _StudentRecognitionWorkerError(
                page_index,
                page,
                exc,
            ) from exc
        finally:
            reset_vision_route_ordinal(route_token)
            reset_vision_batch_deadline_s(batch_deadline_token)
            reset_vision_batch_paper_count(batch_size_token)
            reset_vision_phase_key(phase_token)

    result_stream = teacher_waved_completion_map(
        pending,
        worker,
        teacher_id=str(session.teacher_id or ""),
        batch_id=phase_key,
        wave_size=OCR_TEACHER_WAVE_SIZE,
        max_concurrent=OCR_BATCH_DISPATCH_CONCURRENT,
    )
    try:
        async for _position, result in result_stream:
            yield result
    except _StudentRecognitionWorkerError as exc:
        if isinstance(exc.cause, AnswerSheetAcceleratorState):
            await _persist_student_recognition_result(session, {
                "page_index": exc.page_index,
                "page": exc.page,
                "replacement": None,
            })
        raise exc.cause
    finally:
        await result_stream.aclose()


@router.post("/sessions/{session_id}/students/recognize")
@_requires_answer_sheet_writes
@_serialized_session_mutation
async def recognize_students(
    session_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    session = _load_owned(session_id, user.id)
    _require_no_student_upload(session)
    if session.status not in {"recognizing", "review"} or not session.student_pages:
        raise HTTPException(409, detail="请先上传学生答题卡")
    await _require_answer_sheet_chain(user)
    matcher = None
    output_dir = os.path.join(session.session_dir, "evidence")
    roster_rows = db.query(Student).filter(
        Student.teacher_id == session.teacher_id,
        Student.class_id == session.class_id,
    ).all()
    result_stream = _student_recognition_completion_stream(
        session,
        matcher,
        output_dir,
        db,
        roster_rows,
    )
    try:
        async for result in result_stream:
            await _persist_student_recognition_result(session, result)
        _apply_student_page_pairing(session, roster_rows)
        session.status = "review"
        session.invalidate_grading_drafts()
        session.save()
    except AnswerSheetAcceleratorState as exc:
        session.status = "recognizing"
        session.error = _student_accelerator_message(exc.state)
        session.save()
        payload = session.to_dict(public=True)
        payload["accelerator"] = {
            "status": exc.state,
            "reason_code": exc.reason_code,
            "message": session.error,
        }
        return JSONResponse(status_code=202, content=payload)
    finally:
        await result_stream.aclose()
    return session.to_dict(public=True)


@router.post("/sessions/{session_id}/students/recognize/stream")
@_requires_answer_sheet_writes
async def recognize_students_stream(
    session_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    session = _load_owned(session_id, user.id)
    _require_no_student_upload(session)
    if session.status not in {"recognizing", "review"} or not session.student_pages:
        raise HTTPException(409, detail="请先上传学生答题卡")
    roster_rows = db.query(Student).filter(
        Student.teacher_id == session.teacher_id,
        Student.class_id == session.class_id,
    ).all()

    async def events():
        try:
            with _locked_owned(session_id, user.id) as locked:
                _require_no_student_upload(locked)
                if (
                    locked.status == "review"
                    and all(
                        _student_page_checkpoint_complete(locked, page)
                        for page in locked.student_pages
                    )
                ):
                    yield _answer_sheet_sse("done", {
                        "message": "学生答题卡识别已完成",
                        "current": len(locked.student_pages),
                        "total": len(locked.student_pages),
                        "idempotent": True,
                        "session": locked.to_dict(public=True),
                    })
                    return
                await _require_answer_sheet_chain(user)
                matcher = None
                output_dir = os.path.join(locked.session_dir, "evidence")
                total = len(locked.student_pages)
                completed = sum(
                    1
                    for page in locked.student_pages
                    if _student_page_checkpoint_complete(locked, page)
                )
                if locked.grouping_decisions:
                    locked.invalidate_grading_drafts()
                locked.asset_lifecycle.pop(_GRADING_DRAFT_CHECKPOINT_KEY, None)
                locked.status = "recognizing"
                locked.error = None
                await asyncio.to_thread(locked.save)
                yield _answer_sheet_sse("progress", {
                    "message": "正在恢复学生答题卡识别" if completed else "正在识别学生答题卡",
                    "current": completed,
                    "total": total,
                })
                try:
                    result_stream = _student_recognition_completion_stream(
                        locked,
                        matcher,
                        output_dir,
                        db,
                        roster_rows,
                    )
                    next_result = asyncio.create_task(anext(result_stream))
                    try:
                        while True:
                            done, _ = await asyncio.wait(
                                {next_result},
                                timeout=_ANSWER_SHEET_HEARTBEAT_SECONDS,
                            )
                            if not done:
                                yield _answer_sheet_sse("progress", {
                                    "message": "正在并发识别学生答题卡",
                                    "current": completed,
                                    "total": total,
                                    "heartbeat": True,
                                })
                                continue
                            try:
                                result = next_result.result()
                            except StopAsyncIteration:
                                break
                            await _persist_student_recognition_result(locked, result)
                            completed += 1
                            page = result["page"]
                            yield _answer_sheet_sse("progress", {
                                "message": "当前页识别进度已保存",
                                "current": completed,
                                "total": total,
                                "checkpoint": True,
                                "paper_id": page.get("paper_id"),
                            })
                            next_result = asyncio.create_task(anext(result_stream))
                    finally:
                        if not next_result.done():
                            next_result.cancel()
                        await asyncio.gather(next_result, return_exceptions=True)
                        await result_stream.aclose()

                    _apply_student_page_pairing(locked, roster_rows)
                    locked.status = "review"
                    locked.error = None
                    await asyncio.to_thread(locked.save)
                    yield _answer_sheet_sse("done", {
                        "message": "学生答题卡识别完成",
                        "current": total,
                        "total": total,
                        "session": locked.to_dict(public=True),
                    })
                except AnswerSheetAcceleratorState as exc:
                    locked.status = "recognizing"
                    locked.error = _student_accelerator_message(exc.state)
                    await asyncio.to_thread(locked.save)
                    yield _answer_sheet_sse("error", {
                        "code": {
                            "waiting_accelerator": "ACCELERATOR_WAITING",
                            "unavailable": "ACCELERATOR_UNAVAILABLE",
                            "fault": "ACCELERATOR_FAULT",
                        }.get(exc.state, "ACCELERATOR_FAULT"),
                        "status": exc.state,
                        "reason_code": exc.reason_code,
                        "message": locked.error,
                    })
                except (asyncio.CancelledError, GeneratorExit):
                    if locked.status != "review":
                        locked.status = "recognizing"
                        locked.error = "学生答题卡识别连接中断，已保存完成页；再次执行即可恢复。"
                        await asyncio.to_thread(locked.save)
                    raise
                except Exception:
                    logger.exception("answer-sheet student recognition stream failed")
                    locked.status = "recognizing"
                    locked.error = "学生答题卡识别链路中断，已保存完成页；请稍后重试恢复。"
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


@router.post("/sessions/{session_id}/students/{paper_id}/review-confirm")
@_requires_answer_sheet_writes
@_serialized_session_mutation
def confirm_student_review(
    session_id: str, paper_id: str, req: ReviewConfirmRequest,
    db: Session = Depends(get_db), user: User = Depends(_require_user),
):
    session = _load_owned(session_id, user.id)
    _require_no_student_upload(session)
    if session.status != "review":
        raise HTTPException(409, detail="当前不在教师复核阶段")
    page = next((item for item in session.student_pages if item.get("paper_id") == paper_id), None)
    if not page:
        raise HTTPException(404, detail="学生答题卡页面不存在")
    student = db.query(Student).filter(
        Student.id == req.student_id, Student.teacher_id == user.id,
        Student.class_id == session.class_id,
    ).first()
    if not student:
        raise HTTPException(404, detail="当前班级不存在该学生")
    expected = {item["question_no"] for item in page.get("answers") or []}
    if set(req.answers) - expected:
        raise HTTPException(409, detail="提交了不属于本页的题号")
    for item in page.get("answers") or []:
        value = str(req.answers.get(item["question_no"], item.get("answer") or "")).strip()
        if not value:
            raise HTTPException(409, detail=f"第{item['question_no']}题答案仍未确认")
        item.update({"answer": value, "status": "teacher_confirmed", "reason": "teacher_confirmed"})
    page["identity"] = {
        **(page.get("identity") or {}), "status": "teacher_confirmed",
        "student_id": student.id, "student_name": student.name,
    }
    page["review_reasons"] = []
    _apply_student_page_pairing(
        session,
        db.query(Student).filter(
            Student.teacher_id == session.teacher_id,
            Student.class_id == session.class_id,
        ).all(),
    )
    session.invalidate_grading_drafts()
    session.save()
    return session.to_dict(public=True)
