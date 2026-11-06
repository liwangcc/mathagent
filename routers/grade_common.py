"""Plumbing shared by the batch-grading routes in routers/grade*.py.

- SSE: ``_sse`` formats one event; ``_GRADE_HEARTBEAT_SECONDS`` paces keep-alives.
- Settling: ``_settle_template_component`` waits a task out through cancellation.
- Checkpoints: ``_save_quick_batch_meta`` writes a QuickBatch snapshot off the
  event loop, one snapshot at a time per worker.
- Ownership: templates and batches load only for the teacher who owns them; the
  ``_locked_*`` forms hold the operation lock while the caller changes them.
- Review rows: small lookups over a batch's review results.

Moved unchanged from routers/grade.py, which still exposes these names.
"""

import asyncio
import hashlib
import json as _json
import os
from contextlib import contextmanager
from datetime import datetime, timezone

from anyio import CancelScope
from fastapi import HTTPException

from config import UPLOAD_DIR
from models_user import User
from services.grading_persistence import image_content_hash
from services.processing_errors import current_correlation_id, report_failure
from services.quick_grader import BatchOperationBusy, QuickBatch, batch_operation_lock
from services.template_session import TemplateSession


# ── SSE events and task settling ─────────────────────────────────────────
_GRADE_HEARTBEAT_SECONDS = 15.0


# Resumable scheduling pauses, not failures: they must not mint an error id or
# be logged as processing.failed (each accelerator wait used to add one).
_SSE_PAUSE_CODES = {"ACCELERATOR_WAITING", "ACCELERATOR_UNAVAILABLE"}


def _sse(event: str, payload: dict) -> str:
    if (
        event == "error"
        and not payload.get("error_info")
        and str(payload.get("code") or "") not in _SSE_PAUSE_CODES
    ):
        error = RuntimeError(str(payload.get("message") or "processing failed"))
        error.code = str(payload.get("code") or "")
        stage = "template" if "TEMPLATE" in error.code else "grading"
        payload = {**payload, "error_info": report_failure(error, stage=stage)}
    if current_correlation_id():
        payload = {"correlation_id": current_correlation_id(), **payload}
    return f"event: {event}\ndata: {_json.dumps(payload, ensure_ascii=False)}\n\n"


async def _settle_template_component(
    task: asyncio.Task,
    *,
    cancel_running: bool,
) -> None:
    """Cancel when possible and do not return until the component is settled."""
    # StreamingResponse cancels an AnyIO scope: asyncio.shield alone cannot
    # protect the waiter from level cancellation (including the final gather).
    with CancelScope(shield=True):
        if cancel_running and not task.done():
            task.cancel()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # Repeated direct asyncio cancellation must not strand cleanup.
                continue
            except BaseException:
                break
        # The task is done: retrieve its exception synchronously so even a
        # second direct Task.cancel() cannot interrupt a final gather checkpoint.
        if not task.cancelled():
            task.exception()
# ── QuickBatch checkpoints ───────────────────────────────────────────────
_quick_batch_meta_io_semaphore: asyncio.Semaphore | None = None
_quick_batch_meta_io_loop: asyncio.AbstractEventLoop | None = None


def _get_quick_batch_meta_io_semaphore() -> asyncio.Semaphore:
    """Serialize large QuickBatch snapshots per worker to avoid disk thrash."""
    global _quick_batch_meta_io_loop, _quick_batch_meta_io_semaphore
    loop = asyncio.get_running_loop()
    if (
        _quick_batch_meta_io_semaphore is None
        or _quick_batch_meta_io_loop is not loop
    ):
        _quick_batch_meta_io_loop = loop
        _quick_batch_meta_io_semaphore = asyncio.Semaphore(1)
    return _quick_batch_meta_io_semaphore


async def _save_quick_batch_meta(batch: QuickBatch) -> None:
    """Write one atomic batch checkpoint without blocking SSE heartbeats."""
    semaphore = _get_quick_batch_meta_io_semaphore()
    async with semaphore:
        # Snapshot on the event-loop thread: pipeline tasks keep mutating the
        # live batch while the worker thread writes the file.
        snapshot = batch.serialize_meta()
        task = asyncio.create_task(asyncio.to_thread(batch.save_meta, snapshot=snapshot))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # The thread cannot be cancelled. Keep the batch operation lock until
            # its atomic replace has settled, then propagate the disconnect.
            await _settle_template_component(task, cancel_running=False)
            raise
# ── Templates, for their owner only ──────────────────────────────────────
def _new_template_session(
    template_id: str,
    teacher_id: int,
    teaching_context: dict,
) -> TemplateSession:
    """A new template session, under the upload root the owner checks read."""
    return TemplateSession(
        template_id, UPLOAD_DIR, teacher_id=teacher_id, teaching_context=teaching_context,
    )


def _load_owned_template_session(template_id: str, teacher_id: int) -> TemplateSession:
    """Load a template only when its persisted owner matches the caller."""
    try:
        sess = TemplateSession.load_meta(template_id, UPLOAD_DIR)
    except ValueError as exc:
        raise HTTPException(400, detail="非法的模板标识") from exc
    if sess.teacher_id is None or sess.teacher_id != teacher_id:
        # 所有会话（含 pending）都必须有归属：pending 阶段也已经在创建时写入
        # teacher_id，仅凭不可枚举的模板 ID 不足以授权。
        raise HTTPException(403, detail="无权访问该模板")
    return sess


@contextmanager
def _locked_owned_template_session(template_id: str, teacher_id: int):
    """Serialize template metadata changes and reload after taking the lock."""
    # Validate the identifier and ownership before creating a lock inode.  The
    # second load is the compare-and-save guard against a stale confirmed copy.
    _load_owned_template_session(template_id, teacher_id)
    lock_root = os.path.join(UPLOAD_DIR, ".template-locks")
    try:
        with batch_operation_lock(f"template-{template_id}", lock_root):
            yield _load_owned_template_session(template_id, teacher_id)
    except BatchOperationBusy as exc:
        raise HTTPException(409, detail="该模板正在执行另一项操作，请稍后重试") from exc


async def _template_knowledge_classifier(questions):
    """Label template questions one by one against the stage catalog."""
    from services.analyzer import classify_questions_knowledge
    return await classify_questions_knowledge(questions, per_question=True)
# ── Batches and their review results, for their owner only ───────────────
def _require_owned_batch(batch: QuickBatch, user: User) -> None:
    """Reject legacy/unowned batches instead of guessing ownership or class."""
    if batch.teacher_id is None or batch.teacher_id != user.id:
        raise HTTPException(404, detail="批次不存在或不属于当前老师")
    if batch.identity_mode != "anonymous" and batch.class_id is None:
        raise HTTPException(400, detail="批次未冻结班级，请重新上传")


def _load_owned_review_batch(batch_id: str, user: User) -> QuickBatch:
    batch = QuickBatch.load_meta(batch_id, UPLOAD_DIR)
    if not batch.review_results:
        raise HTTPException(404, detail="批次不存在或尚无待审阅结果")
    _require_owned_batch(batch, user)
    return batch


# Per-paper pipeline grading checkpoints review rows while recognition and
# grading are still running, so "has review rows" no longer means "graded".
_REVIEW_COMPLETE_STATUSES = {"review_ready", "voided", "archived", "done"}


def _require_review_complete(batch: QuickBatch) -> None:
    if batch.status not in _REVIEW_COMPLETE_STATUSES:
        raise HTTPException(
            409,
            detail="本批次批阅尚未全部完成，请先继续批阅；完成后才能人工判定、作废或入档",
        )


@contextmanager
def _locked_owned_review_batch(batch_id: str, user: User):
    """Serialize review mutations so stale QuickBatch snapshots cannot overwrite each other."""
    # Validate existence/ownership before creating a lock inode, then reload after
    # acquisition so the mutation always starts from the newest serialized state.
    _require_review_complete(_load_owned_review_batch(batch_id, user))
    try:
        with batch_operation_lock(batch_id, UPLOAD_DIR):
            batch = _load_owned_review_batch(batch_id, user)
            _require_review_complete(batch)
            yield batch
    except BatchOperationBusy as exc:
        raise HTTPException(409, detail="该批次正在执行另一项审阅操作，请稍后重试") from exc


def _review_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _review_avg_score(rows: list[dict]) -> float | None:
    """Average only resolved provisional scores; pending papers have score=None."""
    scores = [float(row["score"]) for row in rows if row.get("score") is not None]
    return round(sum(scores) / len(scores), 4) if scores else None


def _find_review_result(batch: QuickBatch, paper_id: str) -> dict:
    review = next(
        (item for item in batch.review_results if str(item.get("paper_id") or item.get("review_id") or "") == paper_id),
        None,
    )
    if review is None:
        raise HTTPException(404, detail="该学生试卷不属于本批次")
    return review


def _stable_review_image_hash(batch_id: str, paper_id: str, image_path: str) -> str:
    """Return a non-empty idempotency key even if the uploaded image was cleaned up.

    The real image SHA-256 is preferred.  A deterministic batch+paper fallback
    prevents a committed archive from duplicating formal rows when meta.json
    was not saved before a process crash.
    """
    actual = image_content_hash(image_path)
    if actual:
        return actual
    material = f"quick-batch-review:{batch_id}:{paper_id}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()
