"""The original paper of an answer-sheet session: upload it, recognize its questions, confirm the answers.

- POST /answer-sheet/sessions/{session_id}/source/upload (``upload_source``)
  stores the paper pages and, when the answers come separately, the answer pages.
- POST .../source/recognize (``recognize_source``) and its SSE form
  POST .../source/recognize/stream (``recognize_source_stream``, events from
  ``_recognize_source_stream_events``) recognize the questions and generate
  standard answers; a recognition that stopped resumes from its per-page
  checkpoint (``_source_recognition_is_resume``, ``_source_recognition_phase``,
  ``_source_page_checkpoint``).
- POST .../answers/confirm (``confirm_answers``) saves the teacher-confirmed
  questions (``AnswerConfirmRequest``).

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
from typing import AsyncIterator

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from config import MAX_TEMPLATE_PAGES, MAX_UPLOAD_TOTAL_SIZE
from models_user import User
from routers.answer_sheet_assets import _remove_layout_assets, _save_images
from routers.answer_sheet_availability import _require_answer_sheet_chain
from routers.answer_sheet_common import (
    _answer_sheet_sse,
    _answer_sheet_stream,
    _load_owned,
    _locked_owned,
    _requires_answer_sheet_writes,
    _serialized_session_mutation,
    _with_answer_sheet_heartbeats,
)
from routers.auth import _require_user
from services.answer_sheet_session import AnswerSheetSession
from services.ocr_service import reset_vision_phase_key, set_vision_phase_key
from services.teaching_context import stream_teaching
from services.template_recognition import (
    generate_template_answers_batch,
    recognize_template_blank,
    recognize_template_with_answers,
)

# routers.answer_sheet's logger, so these records keep their logger name.
logger = logging.getLogger("routers.answer_sheet")
router = APIRouter()


class AnswerConfirmRequest(BaseModel):
    questions: list[dict] = Field(..., min_length=1, max_length=200)


@router.post("/sessions/{session_id}/source/upload")
@_requires_answer_sheet_writes
@_serialized_session_mutation
async def upload_source(
    session_id: str,
    source_files: list[UploadFile] = File(...),
    answer_files: list[UploadFile] | None = File(None),
    answer_source: str = Form("embedded"),
    user: User = Depends(_require_user),
):
    if len(source_files) > MAX_TEMPLATE_PAGES:
        raise HTTPException(413, detail=f"原始试卷最多上传 {MAX_TEMPLATE_PAGES} 页")
    if answer_files and len(answer_files) > MAX_TEMPLATE_PAGES:
        raise HTTPException(413, detail=f"答案文件最多上传 {MAX_TEMPLATE_PAGES} 页")
    if answer_source not in {"embedded", "separate", "ai_generated"}:
        raise HTTPException(400, detail="答案来源无效")
    if answer_source == "separate" and not answer_files:
        raise HTTPException(400, detail="分离答案模式必须上传答案文件")
    session = _load_owned(session_id, user.id)
    if session.status not in {"draft", "failed"}:
        raise HTTPException(409, detail="当前阶段不能重新上传原始试卷")
    previous_source_pages = list(session.source_exam_pages)
    previous_answer_pages = list(session.answer_pages)
    previous_state = (
        session.answer_source,
        session.source_recognition_generation,
        list(session.source_question_drafts),
        session.status,
        session.error,
    )
    source_dir = os.path.join(
        session.session_dir,
        f"source_{uuid.uuid4().hex}",
    )
    try:
        source_pages = await _save_images(source_files, source_dir, "source")
        remaining_budget = MAX_UPLOAD_TOTAL_SIZE - sum(
            os.path.getsize(page["path"])
            for page in source_pages
        )
        saved_answer_pages = (
            await _save_images(
                answer_files,
                source_dir,
                "answer",
                total_byte_budget=remaining_budget,
            )
            if answer_files
            else []
        )
        session.source_exam_pages = source_pages
        session.answer_pages = saved_answer_pages
        session.answer_source = answer_source
        session.source_recognition_generation = 0
        session.source_question_drafts = []
        session.status = "source_uploaded"
        session.error = None
        session.save()
    except BaseException:
        shutil.rmtree(source_dir, ignore_errors=True)
        session.source_exam_pages = previous_source_pages
        session.answer_pages = previous_answer_pages
        (
            session.answer_source,
            session.source_recognition_generation,
            session.source_question_drafts,
            session.status,
            session.error,
        ) = previous_state
        raise
    _remove_layout_assets(
        session.session_dir,
        [*previous_source_pages, *previous_answer_pages],
        {},
        preserve_pages=[*source_pages, *saved_answer_pages],
    )
    return session.to_dict(public=True)


def _source_recognition_is_resume(session: AnswerSheetSession) -> bool:
    if session.status == "failed" and not session.source_question_drafts:
        return True
    if session.status != "source_uploaded":
        return False
    if session.error:
        return True
    return any(
        isinstance(page.get("recognition_checkpoint"), dict)
        for page in [*session.source_exam_pages, *session.answer_pages]
    )


def _source_recognition_phase(session: AnswerSheetSession):
    return set_vision_phase_key(
        f"answer-sheet-source:{session.source_recognition_generation}",
        retry_scope_key="answer-sheet-source",
    )


@router.post("/sessions/{session_id}/source/recognize")
@_requires_answer_sheet_writes
@_serialized_session_mutation
async def recognize_source(session_id: str, user: User = Depends(_require_user)):
    """Recognize source-exam semantics; blank answer-sheet pixels are never used."""
    session = _load_owned(session_id, user.id)
    if session.status not in {"source_uploaded", "failed"} or not session.source_exam_pages:
        raise HTTPException(409, detail="请先上传原始试卷")
    await _require_answer_sheet_chain(user)
    if _source_recognition_is_resume(session):
        session.source_recognition_generation += 1
        session.save()
    vision_phase_token = _source_recognition_phase(session)
    try:
        drafts: list[dict] = []
        if session.answer_source == "embedded":
            for page in session.source_exam_pages:
                result = await recognize_template_with_answers(page["path"])
                if result.get("error") or not result.get("questions"):
                    raise ValueError(result.get("error") or "原始试卷未识别到题目")
                drafts.extend(result["questions"])
        else:
            for page in session.source_exam_pages:
                result = await recognize_template_blank(page["path"])
                if result.get("error") or not result.get("questions"):
                    raise ValueError(result.get("error") or "原始试卷未识别到题目")
                drafts.extend(result["questions"])
            if session.answer_source == "ai_generated":
                generated = await generate_template_answers_batch(drafts)
                if generated.get("error"):
                    raise ValueError(generated["error"])
                for question in drafts:
                    number = str(question.get("question_number") or question.get("question_no") or "").strip()
                    answer = generated["answers"].get(number)
                    if not answer:
                        raise ValueError(f"第{number}题缺少AI候选答案")
                    question["standard_answer"] = answer["standard_answer"]
                    question["solution_steps"] = answer["model_answer"]
            else:
                answer_drafts: list[dict] = []
                for page in session.answer_pages:
                    result = await recognize_template_with_answers(page["path"])
                    if result.get("error") or not result.get("questions"):
                        raise ValueError(result.get("error") or "答案文件未识别到答案")
                    answer_drafts.extend(result["questions"])
                by_number = {
                    str(item.get("question_number") or item.get("question_no") or "").strip(): item
                    for item in answer_drafts
                }
                for question in drafts:
                    number = str(question.get("question_number") or question.get("question_no") or "").strip()
                    answer = by_number.get(number)
                    if not answer or not str(answer.get("standard_answer") or "").strip():
                        raise ValueError(f"答案文件缺少第{number}题")
                    question["standard_answer"] = answer["standard_answer"]
                    question["solution_steps"] = answer.get("solution_steps") or []
        numbers = [str(item.get("question_number") or item.get("question_no") or "").strip() for item in drafts]
        if not numbers or any(not number for number in numbers) or len(numbers) != len(set(numbers)):
            raise ValueError("原始试卷题号为空或跨页重复，请教师检查")
        session.source_question_drafts = drafts
        session.status = "source_recognized"
        session.error = None
        session.save()
        return session.to_dict(public=True)
    except ValueError as exc:
        session.status = "failed"
        session.error = str(exc)
        session.save()
        raise HTTPException(422, detail=str(exc)) from exc
    finally:
        reset_vision_phase_key(vision_phase_token)


def _source_page_checkpoint(page: dict, mode: str, teaching_context=None) -> list[dict] | None:
    checkpoint = page.get("recognition_checkpoint")
    if not isinstance(checkpoint, dict):
        return None
    if checkpoint.get("mode") != mode:
        return None
    if checkpoint.get("sha256") != page.get("sha256"):
        return None
    if (checkpoint.get("teaching_context") or {}) != (teaching_context or {}):
        return None
    questions = checkpoint.get("questions")
    return list(questions) if isinstance(questions, list) and questions else None


async def _recognize_source_stream_events(
    session: AnswerSheetSession,
) -> AsyncIterator[str]:
    source_mode = "embedded" if session.answer_source == "embedded" else "blank"
    work: list[tuple[dict, str, str]] = [
        (page, source_mode, "原始试卷") for page in session.source_exam_pages
    ]
    if session.answer_source == "separate":
        work.extend((page, "embedded", "答案文件") for page in session.answer_pages)
    total = len(work) + (1 if session.answer_source == "ai_generated" else 0)
    completed = 0

    yield _answer_sheet_sse("progress", {
        "message": "正在恢复已保存进度" if any(
            _source_page_checkpoint(page, mode, session.teaching_context) is not None
            for page, mode, _label in work
        ) else "正在识别原始试卷",
        "current": 0,
        "total": total,
    })

    for page, mode, label in work:
        questions = _source_page_checkpoint(page, mode, session.teaching_context)
        if questions is None:
            recognize = (
                recognize_template_with_answers
                if mode == "embedded"
                else recognize_template_blank
            )
            result = None
            async for event, payload in _with_answer_sheet_heartbeats(
                recognize(page["path"]),
                message=f"正在识别{label}第 {completed + 1} 项",
                current=completed,
                total=total,
            ):
                if event == "progress":
                    yield _answer_sheet_sse("progress", payload)
                else:
                    result = payload
            if not isinstance(result, dict) or result.get("error") or not result.get("questions"):
                message = result.get("error") if isinstance(result, dict) else None
                raise ValueError(message or f"{label}未识别到题目")
            questions = list(result["questions"])
            page["recognition_checkpoint"] = {
                "mode": mode,
                "sha256": page.get("sha256"),
                "questions": questions,
                "teaching_context": session.teaching_context,
            }
            session.status = "source_uploaded"
            session.error = None
            await asyncio.to_thread(session.save)
        completed += 1
        yield _answer_sheet_sse("progress", {
            "message": f"{label}识别进度已保存",
            "current": completed,
            "total": total,
            "checkpoint": True,
        })

    drafts = [
        question
        for page in session.source_exam_pages
        for question in (_source_page_checkpoint(page, source_mode, session.teaching_context) or [])
    ]
    if session.answer_source == "ai_generated":
        generated = None
        async for event, payload in _with_answer_sheet_heartbeats(
            generate_template_answers_batch(drafts),
            message="正在生成参考答案",
            current=completed,
            total=total,
        ):
            if event == "progress":
                yield _answer_sheet_sse("progress", payload)
            else:
                generated = payload
        if not isinstance(generated, dict) or generated.get("error"):
            message = generated.get("error") if isinstance(generated, dict) else None
            raise ValueError(message or "AI候选答案生成失败")
        for question in drafts:
            number = str(
                question.get("question_number") or question.get("question_no") or ""
            ).strip()
            answer = generated.get("answers", {}).get(number)
            if not answer:
                raise ValueError(f"第{number}题缺少AI候选答案")
            question["standard_answer"] = answer["standard_answer"]
            question["solution_steps"] = answer["model_answer"]
        completed += 1
    elif session.answer_source == "separate":
        answer_drafts = [
            question
            for page in session.answer_pages
            for question in (_source_page_checkpoint(page, "embedded", session.teaching_context) or [])
        ]
        by_number = {
            str(item.get("question_number") or item.get("question_no") or "").strip(): item
            for item in answer_drafts
        }
        for question in drafts:
            number = str(
                question.get("question_number") or question.get("question_no") or ""
            ).strip()
            answer = by_number.get(number)
            if not answer or not str(answer.get("standard_answer") or "").strip():
                raise ValueError(f"答案文件缺少第{number}题")
            question["standard_answer"] = answer["standard_answer"]
            question["solution_steps"] = answer.get("solution_steps") or []

    numbers = [
        str(item.get("question_number") or item.get("question_no") or "").strip()
        for item in drafts
    ]
    if not numbers or any(not number for number in numbers) or len(numbers) != len(set(numbers)):
        raise ValueError("原始试卷题号为空或跨页重复，请教师检查")

    session.source_question_drafts = drafts
    for page, _mode, _label in work:
        page.pop("recognition_checkpoint", None)
    session.status = "source_recognized"
    session.error = None
    await asyncio.to_thread(session.save)
    yield _answer_sheet_sse("done", {
        "message": "原始试卷识别完成",
        "current": total,
        "total": total,
        "session": session.to_dict(public=True),
    })


@router.post("/sessions/{session_id}/source/recognize/stream")
@_requires_answer_sheet_writes
async def recognize_source_stream(
    session_id: str,
    user: User = Depends(_require_user),
):
    session = _load_owned(session_id, user.id)
    if session.status not in {"source_uploaded", "source_recognized", "failed"} or not session.source_exam_pages:
        raise HTTPException(409, detail="请先上传原始试卷")

    async def events():
        try:
            with _locked_owned(session_id, user.id) as locked:
                if locked.status == "source_recognized" and locked.source_question_drafts:
                    yield _answer_sheet_sse("done", {
                        "message": "原始试卷识别已完成",
                        "current": len(locked.source_exam_pages),
                        "total": len(locked.source_exam_pages),
                        "idempotent": True,
                        "session": locked.to_dict(public=True),
                    })
                    return
                await _require_answer_sheet_chain(user)
                if _source_recognition_is_resume(locked):
                    locked.source_recognition_generation += 1
                    await asyncio.to_thread(locked.save)
                vision_phase_token = _source_recognition_phase(locked)
                try:
                    try:
                        async for event in _recognize_source_stream_events(locked):
                            yield event
                    except (asyncio.CancelledError, GeneratorExit):
                        if locked.status != "source_recognized":
                            locked.status = "source_uploaded"
                            locked.error = "题目识别连接中断，已保存完成页；再次执行即可恢复。"
                            await asyncio.to_thread(locked.save)
                        raise
                    except ValueError as exc:
                        locked.status = "failed"
                        locked.error = str(exc)
                        await asyncio.to_thread(locked.save)
                        yield _answer_sheet_sse("error", {
                            "code": "TEMPLATE_OCR_FAILED",
                            "message": str(exc),
                        })
                    except Exception:
                        logger.exception("answer-sheet source recognition stream failed")
                        locked.status = "source_uploaded"
                        locked.error = "题目识别链路中断，已保存完成页；请稍后重试恢复。"
                        await asyncio.to_thread(locked.save)
                        yield _answer_sheet_sse("error", {
                            "code": "GRADING_CHAIN_FAILED",
                            "message": locked.error,
                        })
                finally:
                    reset_vision_phase_key(vision_phase_token)
        except HTTPException as exc:
            yield _answer_sheet_sse("error", {
                "code": "ANSWER_SHEET_OPERATION_BUSY" if exc.status_code == 409 else "GRADING_CHAIN_FAILED",
                "message": str(exc.detail),
            })

    return _answer_sheet_stream(stream_teaching(session.teaching_context, events()))


@router.post("/sessions/{session_id}/answers/confirm")
@_requires_answer_sheet_writes
@_serialized_session_mutation
def confirm_answers(
    session_id: str,
    req: AnswerConfirmRequest = Body(...),
    user: User = Depends(_require_user),
):
    session = _load_owned(session_id, user.id)
    try:
        session.confirm_answers(req.questions)
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return session.to_dict(public=True)
