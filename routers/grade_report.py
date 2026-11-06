"""The teaching report of a graded batch: generated in the background, read by the teacher.

- ``_start_batch_report_generation``: snapshot the report inputs, mark the
  report ``generating`` and run ``_run_batch_report_generation`` as one task
  per batch (``_batch_report_tasks``).
- The run first settles the batch's knowledge labels (``_refresh_batch_knowledge``),
  then asks the model, and commits the text only if the review data did not
  change meanwhile (``_finish_batch_report_sync``); otherwise it is queued again.
- GET /batch-grade/report/{batch_id} (``batch_grade_get_report``) returns the
  stored report and starts generation lazily for older batches.

Moved unchanged from routers/grade.py, which still exposes these names and
includes this module's router after its own routes.
"""

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from config import UPLOAD_DIR
from models_user import User
from routers.auth import _require_user
from routers.grade_common import (
    _load_owned_review_batch,
    _load_owned_template_session,
    _save_quick_batch_meta,
    _template_knowledge_classifier,
)
from services import template_knowledge
from services.batch_report import build_batch_report_payload, generate_batch_report
from services.quick_grader import BatchOperationBusy, QuickBatch, batch_operation_lock

# routers.grade's logger, so these records keep their logger name.
logger = logging.getLogger("routers.grade")
router = APIRouter()

_batch_report_tasks: dict[str, asyncio.Task] = {}


def _batch_report_response(batch: QuickBatch) -> dict:
    return {
        "status": str(batch.report_status or "not_started"),
        "text": str(batch.report_text or ""),
        "stats": batch.report_stats if isinstance(batch.report_stats, dict) else {},
        "error": str(batch.report_error or ""),
        "updated_at": str(batch.report_updated_at or ""),
    }


def _batch_knowledge_template(batch: QuickBatch, teacher_id: int):
    if not str(batch.template_id or "").strip():
        return None
    sess = _load_owned_template_session(batch.template_id, teacher_id)
    if (sess.teacher_id != teacher_id or sess.status not in {"confirmed", "frozen"}
            or sess.recognition_contract_error()
            or sess.teaching_context != batch.teaching_context
            or (batch.template_contract_hash and batch.template_contract_hash != sess.confirmed_contract_hash)):
        raise ValueError("模板知识点版本与本批次不一致")
    return sess


def _refresh_batch_knowledge(batch: QuickBatch, teacher_id: int) -> dict:
    """Only labels are mutable here; callers own the ordinary batch save lock."""
    try:
        sess = _batch_knowledge_template(batch, teacher_id)
        if sess is None:
            return {"status": "ready", "error": ""}
        template_knowledge.reconcile_batch_knowledge(batch, sess)
        return template_knowledge.knowledge_state(sess)
    except Exception as exc:
        logger.warning("批次知识点刷新暂缓 batch=%s: %s", batch.batch_id, type(exc).__name__)
        return {"status": "failed", "error": "知识点分类暂未完成，请稍后重试报告；已有成绩不受影响"}


def _reconcile_batch_report_sync(batch_id: str, teacher_id: int):
    with batch_operation_lock(batch_id, UPLOAD_DIR):
        batch = QuickBatch.load_meta(batch_id, UPLOAD_DIR)
        if batch.teacher_id != teacher_id:
            raise ValueError("batch report owner changed")
        state = _refresh_batch_knowledge(batch, teacher_id)
        payload = build_batch_report_payload(batch)
        batch.report_stats = payload["stats"]
        batch.report_input_hash = payload["input_hash"]
        batch.report_status = "generating" if state["status"] == "ready" else (
            "queued" if state["status"] == "pending" else "failed")
        batch.report_text = ""
        batch.report_error = state["error"]
        batch.report_updated_at = datetime.now(timezone.utc).isoformat()
        batch.save_meta()
        return payload, state


async def _prepare_batch_report_knowledge(batch_id: str, teacher_id: int):
    batch = await asyncio.to_thread(QuickBatch.load_meta, batch_id, UPLOAD_DIR)
    try:
        sess = _batch_knowledge_template(batch, teacher_id)
        if sess is not None:
            await template_knowledge.ensure_session_knowledge(
                sess, classifier=_template_knowledge_classifier,
            )
    except Exception as exc:
        logger.warning("批次知识点分类暂缓 batch=%s: %s", batch_id, type(exc).__name__)
    # Grading may still hold the batch lock while finishing its SSE. Do not
    # perform provider work again while waiting for that narrow commit window.
    for attempt in range(60):
        try:
            return await asyncio.to_thread(_reconcile_batch_report_sync, batch_id, teacher_id)
        except BatchOperationBusy:
            if attempt == 59:
                raise
            await asyncio.sleep(0.1)


def _finish_batch_report_sync(
    batch_id: str,
    expected_input_hash: str,
    text: str,
    provider_note: str,
) -> None:
    """Commit a report only if the review data did not change while generating."""
    with batch_operation_lock(batch_id, UPLOAD_DIR):
        batch = QuickBatch.load_meta(batch_id, UPLOAD_DIR)
        state = _refresh_batch_knowledge(batch, batch.teacher_id)
        current = build_batch_report_payload(batch)
        if state["status"] != "ready":
            batch.report_status = "queued" if state["status"] == "pending" else "failed"
            batch.report_text = ""
            batch.report_stats = current["stats"]
            batch.report_error = state["error"]
        elif current["input_hash"] != expected_input_hash:
            batch.report_status = "queued"
            batch.report_text = ""
            batch.report_stats = current["stats"]
            batch.report_error = "批改结果已更新，正在重新生成报告"
        else:
            batch.report_status = "ready"
            batch.report_text = str(text or "")
            batch.report_stats = current["stats"]
            batch.report_error = "；".join(note for note in (state["error"], str(provider_note or "")) if note)
        batch.report_input_hash = current["input_hash"]
        batch.report_updated_at = datetime.now(timezone.utc).isoformat()
        batch.save_meta()


def _fail_batch_report_sync(batch_id: str, expected_input_hash: str, error: str) -> None:
    with batch_operation_lock(batch_id, UPLOAD_DIR):
        batch = QuickBatch.load_meta(batch_id, UPLOAD_DIR)
        current = build_batch_report_payload(batch)
        if current["input_hash"] != expected_input_hash:
            batch.report_status = "queued"
            batch.report_error = "批改结果已更新，等待重新生成报告"
        else:
            batch.report_status = "failed"
            batch.report_error = str(error or "报告生成失败，请稍后重试")[:300]
        batch.report_input_hash = current["input_hash"]
        batch.report_stats = current["stats"]
        batch.report_updated_at = datetime.now(timezone.utc).isoformat()
        batch.save_meta()


async def _run_batch_report_generation(
    batch_id: str,
    teacher_id: int,
    expected_input_hash: str,
    stats: dict,
) -> None:
    try:
        payload, state = await _prepare_batch_report_knowledge(batch_id, teacher_id)
        if state["status"] != "ready":
            return
        expected_input_hash, stats = payload["input_hash"], payload["stats"]
        text, provider_note = await generate_batch_report(stats, teacher_id=teacher_id)
        await asyncio.to_thread(
            _finish_batch_report_sync,
            batch_id,
            expected_input_hash,
            text,
            provider_note,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("batch report generation failed batch=%s: %s", batch_id, exc, exc_info=True)
        try:
            await asyncio.to_thread(
                _fail_batch_report_sync,
                batch_id,
                expected_input_hash,
                "报告生成失败，请稍后重试",
            )
        except Exception:
            logger.exception("failed to persist batch report failure batch=%s", batch_id)


def _discard_batch_report_task(task: asyncio.Task, batch_id: str) -> None:
    if _batch_report_tasks.get(batch_id) is task:
        _batch_report_tasks.pop(batch_id, None)


async def _start_batch_report_generation(batch: QuickBatch, teacher_id: int) -> None:
    batch_id = str(batch.batch_id)
    existing = _batch_report_tasks.get(batch_id)
    if existing is not None and not existing.done():
        return
    payload = build_batch_report_payload(batch)
    batch.report_status = "generating"
    batch.report_text = ""
    batch.report_stats = payload["stats"]
    batch.report_error = ""
    batch.report_input_hash = payload["input_hash"]
    batch.report_updated_at = datetime.now(timezone.utc).isoformat()
    await _save_quick_batch_meta(batch)
    task = asyncio.create_task(
        _run_batch_report_generation(
            batch_id,
            int(teacher_id),
            payload["input_hash"],
            payload["stats"],
        ),
        name=f"batch-report-{batch_id}",
    )
    _batch_report_tasks[batch_id] = task
    task.add_done_callback(lambda done: _discard_batch_report_task(done, batch_id))


async def _ensure_batch_report_generation(batch: QuickBatch, teacher_id: int) -> QuickBatch:
    state = _refresh_batch_knowledge(batch, teacher_id)
    payload = build_batch_report_payload(batch)
    existing = _batch_report_tasks.get(str(batch.batch_id))
    report_is_current = (
        str(batch.report_input_hash or "") == payload["input_hash"]
        and str(batch.report_status or "") == "ready"
        and state["status"] == "ready"
    )
    if not report_is_current and (existing is None or existing.done()):
        # Reload under the review lock, never save a request's stale snapshot
        # over a teacher decision while refreshing derived report metadata.
        try:
            with batch_operation_lock(batch.batch_id, UPLOAD_DIR):
                batch = QuickBatch.load_meta(batch.batch_id, UPLOAD_DIR)
                _refresh_batch_knowledge(batch, teacher_id)
                await _start_batch_report_generation(batch, teacher_id)
        except BatchOperationBusy:
            pass
        batch = QuickBatch.load_meta(batch.batch_id, UPLOAD_DIR)
    return batch


@router.get("/batch-grade/report/{batch_id}")
async def batch_grade_get_report(
    batch_id: str,
    user: User = Depends(_require_user),
):
    """Return the durable teaching report, starting it lazily for old batches."""
    batch = _load_owned_review_batch(batch_id, user)
    batch = await _ensure_batch_report_generation(batch, user.id)
    return {
        "ok": True,
        "batch_id": batch.batch_id,
        "report": _batch_report_response(batch),
    }
