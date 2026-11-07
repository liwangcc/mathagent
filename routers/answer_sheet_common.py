"""Plumbing every answer-sheet route shares.

- SSE: ``_answer_sheet_sse`` formats one event, ``_answer_sheet_stream`` wraps a
  generator as the response, and ``_with_answer_sheet_heartbeats`` yields
  progress every ``_ANSWER_SHEET_HEARTBEAT_SECONDS`` while one slow operation runs.
- The teacher's own session: ``_load_owned`` loads it (404 for anyone else),
  ``_locked_owned`` reloads it under the session's operation lock, and
  ``_serialized_session_mutation`` runs a whole route under that lock.
- Writes: ``_answer_sheet_writes_enabled`` and the ``_requires_answer_sheet_writes``
  route decorator fail closed while answer-sheet grading is switched off.
- Image work: ``_run_answer_sheet_image_work`` runs CPU-heavy image steps behind
  the shared local preprocessing gate.

Moved unchanged from routers/answer_sheet.py, which still exposes these names.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from contextlib import contextmanager
from functools import wraps
from typing import AsyncIterator, Awaitable

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from config import UPLOAD_DIR
from services.answer_sheet_session import (
    AnswerSheetOperationBusy,
    AnswerSheetSession,
    answer_sheet_operation_lock,
)
from services.ocr_service import (
    _get_local_ocr_preprocess_semaphore,
    _run_local_ocr_image_component,
)
from services.teaching_context import use_teaching_context


_ANSWER_SHEET_HEARTBEAT_SECONDS = 15.0


def _answer_sheet_sse(event: str, payload: dict) -> str:
    return (
        f"event: {event}\n"
        f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


def _answer_sheet_stream(events: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _with_answer_sheet_heartbeats(
    awaitable: Awaitable,
    *,
    message: str,
    current: int,
    total: int,
):
    """Yield progress while one model or image operation is still alive."""
    task = asyncio.create_task(awaitable)
    try:
        while True:
            done, _ = await asyncio.wait(
                {task}, timeout=_ANSWER_SHEET_HEARTBEAT_SECONDS
            )
            if done:
                yield "result", task.result()
                return
            yield "progress", {
                "message": message,
                "current": current,
                "total": total,
                "heartbeat": True,
            }
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _load_owned(session_id: str, user_id: int) -> AnswerSheetSession:
    try:
        session = AnswerSheetSession.load(session_id, UPLOAD_DIR)
    except (ValueError, OSError):
        raise HTTPException(404, detail="答题卡会话不存在")
    try:
        session.ensure_owned(user_id)
    except PermissionError:
        raise HTTPException(404, detail="答题卡会话不存在")
    return session


@contextmanager
def _locked_owned(session_id: str, user_id: int):
    """Serialize grading mutations and always reload after lock acquisition."""
    _load_owned(session_id, user_id)
    try:
        with answer_sheet_operation_lock(session_id, UPLOAD_DIR):
            yield _load_owned(session_id, user_id)
    except AnswerSheetOperationBusy as exc:
        raise HTTPException(
            409,
            detail="该答题卡会话正在执行另一项操作，请稍后重试",
        ) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, detail="答题卡会话不存在") from exc


def _serialized_session_mutation(function):
    """Serialize one complete metadata mutation while preserving FastAPI's signature."""
    signature = inspect.signature(function)

    def lock_arguments(args, kwargs):
        bound = signature.bind_partial(*args, **kwargs)
        return str(bound.arguments["session_id"]), bound.arguments["user"]

    if inspect.iscoroutinefunction(function):
        @wraps(function)
        async def async_wrapper(*args, **kwargs):
            session_id, user = lock_arguments(args, kwargs)
            with _locked_owned(session_id, user.id):
                return await function(*args, **kwargs)

        return async_wrapper

    @wraps(function)
    def sync_wrapper(*args, **kwargs):
        session_id, user = lock_arguments(args, kwargs)
        with _locked_owned(session_id, user.id):
            return function(*args, **kwargs)

    return sync_wrapper


def _answer_sheet_writes_enabled() -> None:
    """Fail closed for every grading mutation while read-only evidence stays available."""
    import config

    if not config.ANSWER_SHEET_GRADING_ENABLED:
        raise HTTPException(
            503,
            detail="答题卡批阅暂时不可用：真实学生答题卡裁剪验收尚未通过。",
        )


def _requires_answer_sheet_writes(function):
    """Preserve FastAPI signatures while guarding sync and async write routes."""
    signature = inspect.signature(function)

    def task_context(args, kwargs):
        bound = signature.bind_partial(*args, **kwargs)
        session_id = bound.arguments.get("session_id")
        user = bound.arguments.get("user")
        if session_id and user is not None:
            return getattr(_load_owned(str(session_id), user.id), "teaching_context", {})
        return {}

    if inspect.iscoroutinefunction(function):
        @wraps(function)
        async def async_wrapper(*args, **kwargs):
            _answer_sheet_writes_enabled()
            with use_teaching_context(task_context(args, kwargs)):
                return await function(*args, **kwargs)

        return async_wrapper

    @wraps(function)
    def sync_wrapper(*args, **kwargs):
        _answer_sheet_writes_enabled()
        with use_teaching_context(task_context(args, kwargs)):
            return function(*args, **kwargs)

    return sync_wrapper


async def _run_answer_sheet_image_work(function, /, *args, **kwargs):
    return await _run_local_ocr_image_component(
        function,
        *args,
        semaphore=_get_local_ocr_preprocess_semaphore(),
        **kwargs,
    )
