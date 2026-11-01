"""批阅公共组件 — 批次持久化 + 选择题机械比对。

供统一批改流程（routers/grade.py）复用：
  - QuickBatch：批次状态持久化到 uploads/quick_batch_{id}/meta.json，
    原子写入（写临时文件后 os.replace），中途崩溃不会留下半个 JSON。
  - OCR 任务台账（mark_ocr_outcome、batch_ocr_summary 等）：识别任务的
    状态迁移与汇总，供首次识别与恢复识别共用。
  - _compare_one：选择题等机械比对（非选择题走 LLM，见 services/grader.py）。
"""

import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from services.grading_contract import normalize_choice_answer
from services.matchers.math_matcher import MathMatcher
from services.numeric_answers import parse_number

logger = logging.getLogger(__name__)


class BatchOperationBusy(RuntimeError):
    """The same QuickBatch is being mutated by another review operation."""


# OS locks are process-scoped on some platforms, so an in-process lock is also
# required to reject concurrent FastAPI threads deterministically.  The file
# lock covers separate workers / processes which share QuickBatch metadata.
_batch_locks_guard = threading.Lock()
_batch_local_locks: dict[str, threading.Lock] = {}


def _local_batch_lock(lock_path: str) -> threading.Lock:
    with _batch_locks_guard:
        return _batch_local_locks.setdefault(lock_path, threading.Lock())


@contextmanager
def batch_operation_lock(batch_id: str, upload_dir: str) -> Iterator[None]:
    """Acquire a non-blocking, cross-worker lock for one QuickBatch.

    Every review mutation must hold this from loading metadata through database
    commit and `save_meta()`.  The OS releases the advisory lock when a worker
    crashes; the lock file itself is intentionally retained as a harmless
    coordination inode, not a stale-lock marker.
    """
    batch_dir = os.path.join(upload_dir, f"quick_batch_{batch_id}")
    os.makedirs(batch_dir, exist_ok=True)
    lock_path = os.path.abspath(os.path.join(batch_dir, ".review-operation.lock"))
    local_lock = _local_batch_lock(lock_path)
    if not local_lock.acquire(blocking=False):
        raise BatchOperationBusy(batch_id)

    handle = None
    locked = False
    try:
        handle = open(lock_path, "a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise BatchOperationBusy(batch_id) from exc
        yield
    finally:
        if handle is not None:
            if locked:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    logger.warning("failed to unlock QuickBatch operation lock: %s", lock_path)
            handle.close()
        local_lock.release()


class QuickBatch:
    """快速批阅批次状态。

    status 主路径：recognizing_remaining → ocr_done → grading → review_ready →
    archived / voided。可恢复的中间态：recognition_interrupted、
    ocr_partial_failed、orientation_retrying、grading_interrupted；failed
    表示整批识别失败。
    """

    def __init__(self, batch_id: str, upload_dir: str, *, teaching_context: dict | None = None):
        from services.teaching_context import validate_teaching_context

        self.batch_id = batch_id
        self.teaching_context = validate_teaching_context(teaching_context)
        self.upload_dir = upload_dir
        self.status = "pending"
        self.created_at = datetime.now(timezone.utc).isoformat()
        # Ownership and class are frozen when the upload is accepted.  Later
        # stages must not follow a teacher switching the UI to another class.
        self.teacher_id: int | None = None
        self.class_id: int | None = None
        self.identity_mode: str = "identified"  # identified | anonymous
        # Teacher opt-in, frozen at upload: blank pages inherit the previous
        # page's identity (services/identity_inheritance.py).
        self.identity_inheritance: bool = False
        # Always "template"; batches from the retired no-template flow fail closed.
        self.source_mode: str = ""
        self.template_id: str = ""
        self.template_mode: str = ""           # blank | marked
        self.category: str = ""
        self.exam_name: str = ""
        self.grade_mode: str = "precise"
        # JSON-safe upload state used between human confirmation stages.  No
        # temporary Student rows are needed.
        self.uploads: list[dict] = []
        self.pending_uploads: list[dict] = []
        # Each genuinely started recovery stream gets a fresh OCR scheduler
        # deadline/retry ledger.  The counter is durable so a later worker
        # cannot accidentally reuse an exhausted phase after reconnecting.
        self.recognition_resume_generation: int = 0
        # Durable OCR task ledger.  It contains only batch-local file pointers and
        # state, never image bytes or model credentials. Read-only reloads preserve
        # running jobs; recovery normalizes them only under the operation lock.
        self.ocr_jobs: list[dict] = []
        # OCR 阶段
        self.questions: list[dict] = []   # [{question_no, question_text}]
        self.papers: list[dict] = []      # [{student_name, image_path, questions:[{question_no, student_answer}]}]
        self.confirmation_questions: list[dict] = []
        # 匹配阶段
        self.answer_map: dict = {}        # {question_no: {matched, standard_answer, question_text, source}}
        # 批改阶段：结果先暂存，老师审阅后才归档到正式学生记录。
        self.results: list[dict] = []     # 兼容旧 SSE 重放摘要
        self.review_results: list[dict] = []
        self.review_ready_at: str = ""
        self.review_archived_at: str = ""
        # Grading stopwatch: first paper's grading start (pipeline or confirm)
        # to review_ready, in wall-clock seconds.
        self.grading_started_at: str = ""
        self.grading_elapsed_seconds: float | None = None
        # Raw paper images are temporary review evidence.  Structured results
        # remain in this metadata after the evidence retention window ends.
        self.asset_lifecycle: dict = {}
        # Freeze the teacher-confirmed grading inputs across SSE reconnects.
        # Partial results may be resumed, but must never be mixed with a
        # different answer/identity contract.
        self.grading_contract_hash: str = ""
        self.grading_contract: dict = {}
        # 版本戳（幂等身份 = paper_id + rubric_hash）：识别/批阅按当前 rubric 与
        # 模板合同打戳；戳不匹配的结果一律拒收，旧任务不能覆盖新结果。
        self.grading_rubric_hash: str = ""
        self.template_contract_hash: str = ""
        # Optimistic concurrency token for teacher mutations in the review workspace.
        self.review_revision: int = 0
        # Batch-level teaching report.  The report is generated after grading so
        # the result table never waits on a second model call.
        self.report_status: str = "not_started"
        self.report_text: str = ""
        self.report_stats: dict = {}
        self.report_error: str = ""
        self.report_input_hash: str = ""
        self.report_updated_at: str = ""
        self.error: Optional[str] = None

    @property
    def batch_dir(self) -> str:
        return os.path.join(self.upload_dir, f"quick_batch_{self.batch_id}")

    @property
    def meta_path(self) -> str:
        return os.path.join(self.batch_dir, "meta.json")

    def ensure_ocr_jobs(self, uploads: list[dict]) -> None:
        """Create durable OCR ledger rows for uploads not already represented."""
        known = {str(job.get("image_path") or "") for job in self.ocr_jobs}
        for index, item in enumerate(uploads):
            image_path = str(item.get("image_path") or "")
            if not image_path or image_path in known:
                continue
            self.ocr_jobs.append({
                "job_id": f"ocr-{index}-{os.path.basename(image_path)}",
                "paper_id": str(item.get("paper_id") or ""),
                "filename": str(item.get("filename") or "unknown"),
                "image_path": image_path,
                "status": "queued",
                "attempts": 0,
                "last_error": "",
                "retryable": False,
                "error_code": "",
            })
            known.add(image_path)

    def mark_ocr_job(
        self,
        image_path: str,
        status: str,
        error: str = "",
        *,
        retryable: bool = False,
        error_code: str = "",
    ) -> None:
        """Persist a transition for one batch-local OCR job."""
        for job in self.ocr_jobs:
            if str(job.get("image_path") or "") != image_path:
                continue
            job["status"] = status
            if status == "running":
                job["attempts"] = int(job.get("attempts") or 0) + 1
            job["last_error"] = str(error or "")[:500]
            job["retryable"] = bool(retryable) if status == "failed" else False
            job["error_code"] = (
                str(error_code or "")[:80] if status == "failed" else ""
            )
            return
        raise ValueError(f"unknown OCR job image path: {image_path}")

    def replace_ocr_job_path(self, old_path: str, upload: dict) -> None:
        """Move one stable paper job to a teacher-confirmed working image."""
        paper_id = str(upload.get("paper_id") or "")
        new_path = str(upload.get("image_path") or "")
        for job in self.ocr_jobs:
            same_paper = paper_id and str(job.get("paper_id") or "") == paper_id
            same_path = str(job.get("image_path") or "") == str(old_path or "")
            if not (same_paper or same_path):
                continue
            job.update({
                "paper_id": paper_id,
                "filename": str(upload.get("filename") or job.get("filename") or "unknown"),
                "image_path": new_path,
                "status": "queued",
                "last_error": "",
                "retryable": False,
                "error_code": "",
            })
            return
        self.ocr_jobs.append({
            "job_id": f"ocr-{paper_id or os.path.basename(new_path)}",
            "paper_id": paper_id,
            "filename": str(upload.get("filename") or "unknown"),
            "image_path": new_path,
            "status": "queued",
            "attempts": 0,
            "last_error": "",
            "retryable": False,
            "error_code": "",
        })

    def to_dict(self) -> dict:
        from services.teaching_context import validate_teaching_context

        return {
            "batch_id": self.batch_id,
            "teaching_context": validate_teaching_context(self.teaching_context),
            "status": self.status,
            "created_at": self.created_at,
            "teacher_id": self.teacher_id,
            "class_id": self.class_id,
            "identity_mode": self.identity_mode,
            "identity_inheritance": self.identity_inheritance,
            "source_mode": self.source_mode,
            "template_id": self.template_id,
            "template_mode": self.template_mode,
            "category": self.category,
            "exam_name": self.exam_name,
            "grade_mode": self.grade_mode,
            "uploads": self.uploads,
            "pending_uploads": self.pending_uploads,
            "recognition_resume_generation": self.recognition_resume_generation,
            "ocr_jobs": self.ocr_jobs,
            "questions": self.questions,
            "papers": self.papers,
            "confirmation_questions": self.confirmation_questions,
            "answer_map": self.answer_map,
            "results": self.results,
            "review_results": self.review_results,
            "review_ready_at": self.review_ready_at,
            "review_archived_at": self.review_archived_at,
            "grading_started_at": self.grading_started_at,
            "grading_elapsed_seconds": self.grading_elapsed_seconds,
            "asset_lifecycle": self.asset_lifecycle,
            "grading_contract_hash": self.grading_contract_hash,
            "grading_contract": self.grading_contract,
            "grading_rubric_hash": self.grading_rubric_hash,
            "template_contract_hash": self.template_contract_hash,
            "review_revision": self.review_revision,
            "report_status": self.report_status,
            "report_text": self.report_text,
            "report_stats": self.report_stats,
            "report_error": self.report_error,
            "report_input_hash": self.report_input_hash,
            "report_updated_at": self.report_updated_at,
            "error": self.error,
        }

    def serialize_meta(self) -> bytes:
        """Encode one consistent snapshot of the current batch state.

        ``json.dumps`` without indentation runs in the C encoder, which does
        not yield the GIL mid-document. Callers that write from a worker thread
        must snapshot here on the event-loop thread first: the pure-Python
        encoder used by ``json.dump`` iterates live dicts and can observe (or
        crash on) concurrent pipeline mutations.
        """
        return json.dumps(self.to_dict(), ensure_ascii=False).encode("utf-8")

    def save_meta(self, *, snapshot: bytes | None = None) -> None:
        """Atomically persist ``snapshot`` (or the current state when omitted)."""
        data = self.serialize_meta() if snapshot is None else snapshot
        os.makedirs(self.batch_dir, exist_ok=True)
        # Keep the same atomic-write guarantees as TemplateSession, including
        # short-lived Windows sharing locks during background batch saves.
        tmp = f"{self.meta_path}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(5):
                try:
                    os.replace(tmp, self.meta_path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.01 * (attempt + 1))
        finally:
            try:
                os.remove(tmp)
            except FileNotFoundError:
                pass

    def recover_interrupted_ocr_jobs(self, *, max_attempts: int = 2) -> None:
        """Normalize stale jobs only after acquiring the batch operation lock.

        A plain read cannot tell a live worker from a restart. Unlike explicit
        cancellation, a stale checkpoint cannot prove a call never completed,
        so preserve its attempt count and fail closed at the existing limit.
        """
        for job in self.ocr_jobs:
            if job.get("status") != "running":
                continue
            try:
                attempts = max(0, int(job.get("attempts") or 0))
            except (TypeError, ValueError):
                attempts = 0
            exhausted = attempts >= max_attempts
            job.update({
                "attempts": attempts,
                "status": "failed" if exhausted else "queued",
                "last_error": (
                    "服务中断且自动识别次数已用尽"
                    if exhausted else "服务重启后等待恢复"
                ),
                "retryable": False,
                "error_code": (
                    "worker_interrupted_attempts_exhausted"
                    if exhausted else "worker_interrupted"
                ),
            })

    @classmethod
    def load_meta(cls, batch_id: str, upload_dir: str) -> "QuickBatch":
        batch = cls(batch_id, upload_dir)
        if not os.path.exists(batch.meta_path):
            return batch
        with open(batch.meta_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        from services.teaching_context import validate_teaching_context
        batch.teaching_context = validate_teaching_context(data.get("teaching_context"))
        batch.status = data.get("status", "pending")
        batch.created_at = data.get("created_at") or batch.created_at
        batch.teacher_id = data.get("teacher_id")
        batch.class_id = data.get("class_id")
        batch.identity_mode = (
            "anonymous"
            if str(data.get("identity_mode") or "identified") == "anonymous"
            else "identified"
        )
        batch.identity_inheritance = (
            data.get("identity_inheritance") is True
            and batch.identity_mode != "anonymous"
        )
        batch.source_mode = data.get("source_mode", "") or ""
        batch.template_id = str(data.get("template_id") or "")
        batch.template_mode = str(data.get("template_mode") or "")
        batch.category = str(data.get("category") or "")
        batch.exam_name = str(data.get("exam_name") or "")
        batch.grade_mode = str(data.get("grade_mode") or "precise")
        batch.uploads = data.get("uploads", []) or []
        batch.pending_uploads = data.get("pending_uploads", []) or []
        try:
            batch.recognition_resume_generation = max(
                0, int(data.get("recognition_resume_generation", 0) or 0)
            )
        except (TypeError, ValueError):
            batch.recognition_resume_generation = 0
        batch.ocr_jobs = data.get("ocr_jobs", []) or []
        for job in batch.ocr_jobs:
            job.setdefault("retryable", False)
            job.setdefault("error_code", "")
        batch.questions = data.get("questions", []) or []
        batch.papers = data.get("papers", []) or []
        batch.confirmation_questions = data.get("confirmation_questions", []) or []
        batch.answer_map = data.get("answer_map", {}) or {}
        batch.results = data.get("results", []) or []
        batch.review_results = data.get("review_results", []) or []
        batch.review_ready_at = str(data.get("review_ready_at") or "")
        batch.review_archived_at = str(data.get("review_archived_at") or "")
        batch.grading_started_at = str(data.get("grading_started_at") or "")
        elapsed = data.get("grading_elapsed_seconds")
        batch.grading_elapsed_seconds = (
            float(elapsed)
            if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool) and elapsed >= 0
            else None
        )
        lifecycle = data.get("asset_lifecycle")
        batch.asset_lifecycle = lifecycle if isinstance(lifecycle, dict) else {}
        batch.grading_contract_hash = str(data.get("grading_contract_hash") or "")
        batch.grading_contract = data.get("grading_contract", {}) or {}
        batch.grading_rubric_hash = str(data.get("grading_rubric_hash") or "")
        batch.template_contract_hash = str(data.get("template_contract_hash") or "")
        try:
            batch.review_revision = max(0, int(data.get("review_revision", 0) or 0))
        except (TypeError, ValueError):
            batch.review_revision = 0
        batch.report_status = str(data.get("report_status") or "not_started")
        batch.report_text = str(data.get("report_text") or "")
        report_stats = data.get("report_stats")
        batch.report_stats = report_stats if isinstance(report_stats, dict) else {}
        batch.report_error = str(data.get("report_error") or "")
        batch.report_input_hash = str(data.get("report_input_hash") or "")
        batch.report_updated_at = str(data.get("report_updated_at") or "")
        batch.error = data.get("error")
        return batch


# ── OCR job ledger ──────────────────────────────────────────────────────────
# Transitions over ``QuickBatch.ocr_jobs`` shared by the recognition routes.
# Callers hold the batch operation lock and checkpoint the batch afterwards.

OCR_JOB_TERMINAL_STATES = {"succeeded", "failed", "needs_review", "excluded"}
OCR_MAX_JOB_ATTEMPTS = 2


def ocr_job_attempts(job: dict) -> int:
    """Attempts already spent on ``job``; a malformed count reads as zero."""
    try:
        return max(0, int(job.get("attempts") or 0))
    except (TypeError, ValueError):
        return 0


def requeue_interrupted_ocr_jobs(batch: QuickBatch) -> int:
    """Undo request-bound reservations that produced no durable OCR outcome."""
    requeued = 0
    for job in batch.ocr_jobs:
        if str(job.get("status") or "") != "running":
            continue
        job["status"] = "queued"
        job["attempts"] = max(0, ocr_job_attempts(job) - 1)
        job["last_error"] = "识别连接中断，等待恢复"
        job["retryable"] = False
        job["error_code"] = ""
        requeued += 1
    return requeued


def finalize_exhausted_ocr_jobs(batch: QuickBatch) -> int:
    """Fail closed for stale queued work that already consumed both calls."""
    finalized = 0
    for job in batch.ocr_jobs:
        if (
            str(job.get("status") or "") == "queued"
            and ocr_job_attempts(job) >= OCR_MAX_JOB_ATTEMPTS
        ):
            job["status"] = "failed"
            job["last_error"] = "自动识别次数已用尽"
            job["retryable"] = False
            job["error_code"] = "attempts_exhausted"
            finalized += 1
    return finalized


def requeue_retryable_ocr_failures(batch: QuickBatch) -> int:
    """Queue failed jobs that may still be retried automatically."""
    requeued = 0
    for job in batch.ocr_jobs:
        if (
            str(job.get("status") or "") == "failed"
            and bool(job.get("retryable"))
            and ocr_job_attempts(job) < OCR_MAX_JOB_ATTEMPTS
        ):
            job["status"] = "queued"
            requeued += 1
    return requeued


def mark_ocr_outcome(
    batch: QuickBatch,
    image_path: str,
    paper_data: dict,
) -> tuple[bool, bool]:
    """Record one OCR result on its job and return ``(failed, can_retry)``.

    An accelerator pause is not a failure: the job is queued again and the
    attempt it spent is refunded.
    """
    accelerator_state = str(paper_data.get("_accelerator_state") or "")
    if accelerator_state in {"pending", "unavailable"}:
        job = next(
            (
                item for item in batch.ocr_jobs
                if str(item.get("image_path") or "") == image_path
            ),
            None,
        )
        if job is not None:
            job["status"] = "queued"
            job["attempts"] = max(0, ocr_job_attempts(job) - 1)
            job["last_error"] = str(
                paper_data.get("error")
                or "加速节点不可用，请使用客户端"
            )[:500]
            job["retryable"] = False
            job["error_code"] = str(
                paper_data.get("_accelerator_reason_code")
                or "accelerator_waiting"
            )[:80]
            job["_accelerator_state"] = accelerator_state
            job["_accelerator_reason_code"] = job["error_code"]
        return True, True
    failed = bool(paper_data.get("error") or (
        not paper_data.get("questions") and not paper_data.get("_not_graded_page")
    ))
    retryable = bool(paper_data.get("_ocr_retryable")) if failed else False
    batch.mark_ocr_job(
        image_path,
        "failed" if failed else "succeeded",
        str(paper_data.get("error") or "") if failed else "",
        retryable=retryable,
        error_code=(
            str(paper_data.get("_ocr_error_code") or "ocr_failed")
            if failed
            else ""
        ),
    )
    job = next(
        (
            item for item in batch.ocr_jobs
            if str(item.get("image_path") or "") == image_path
        ),
        {},
    )
    job.pop("_accelerator_state", None)
    job.pop("_accelerator_reason_code", None)
    attempts = ocr_job_attempts(job)
    if failed and retryable and attempts >= OCR_MAX_JOB_ATTEMPTS:
        job["retryable"] = False
    can_retry = bool(
        failed
        and retryable
        and attempts < OCR_MAX_JOB_ATTEMPTS
    )
    return failed, can_retry


def is_accelerator_pause_result(paper_data: dict) -> bool:
    """True when the accelerator deferred this page instead of failing it."""
    return str(paper_data.get("_accelerator_state") or "") in {
        "pending",
        "unavailable",
    }


def queued_recognition_uploads(batch: QuickBatch) -> list[dict]:
    """Uploads whose OCR job is queued with an attempt left, in upload order."""
    queued_paths = {
        os.path.realpath(str(job.get("image_path") or ""))
        for job in batch.ocr_jobs
        if str(job.get("status") or "") == "queued"
        and ocr_job_attempts(job) < OCR_MAX_JOB_ATTEMPTS
        and str(job.get("image_path") or "")
    }
    return [
        upload
        for upload in batch.uploads
        if os.path.realpath(str(upload.get("image_path") or "")) in queued_paths
    ]


def sync_pending_recognition_uploads(batch: QuickBatch) -> None:
    """Rebuild ``batch.pending_uploads`` from the queued and running jobs."""
    unfinished_paths = {
        os.path.realpath(str(job.get("image_path") or ""))
        for job in batch.ocr_jobs
        if str(job.get("status") or "") in {"queued", "running"}
        and str(job.get("image_path") or "")
    }
    batch.pending_uploads = [
        {
            "filename": str(upload.get("filename") or "unknown"),
            "image_path": str(upload.get("image_path") or ""),
        }
        for upload in batch.uploads
        if os.path.realpath(str(upload.get("image_path") or "")) in unfinished_paths
    ]


def batch_ocr_summary(batch: QuickBatch) -> dict:
    """Count OCR job outcomes and decide whether the whole batch can be graded."""
    expected = len(batch.uploads)
    jobs = batch.ocr_jobs
    succeeded = sum(job.get("status") == "succeeded" for job in jobs)
    manual_review = sum(job.get("status") == "needs_review" for job in jobs)
    failed = sum(job.get("status") == "failed" for job in jobs)
    # A teacher explicitly removed these unrecoverable pages from this batch.
    # They are resolved (never silently dropped) but are not graded.
    excluded = sum(job.get("status") == "excluded" for job in jobs)
    retryable_failed = sum(
        job.get("status") == "failed"
        and bool(job.get("retryable"))
        and ocr_job_attempts(job) < OCR_MAX_JOB_ATTEMPTS
        for job in jobs
    )
    unresolved = sum(
        job.get("status") not in OCR_JOB_TERMINAL_STATES
        for job in jobs
    )
    gradable = expected - excluded
    can_grade = bool(
        expected
        and gradable > 0
        and len(jobs) == expected
        and len(batch.papers) == gradable
        and succeeded + manual_review == gradable
        and failed == 0
        and unresolved == 0
    )
    return {
        "uploaded_papers": expected,
        "succeeded_papers": succeeded,
        "manual_review_papers": manual_review,
        "failed_papers": failed,
        "excluded_papers": excluded,
        "retryable_failed_papers": retryable_failed,
        "unresolved_papers": unresolved,
        "can_grade": can_grade,
    }


_PUNCT_MAP = str.maketrans({
    "（": "(", "）": ")", "，": ",", "。": ".",
    "；": ";", "：": ":", "？": "?",
})


def _deep_normalize(s: str) -> str:
    if not s:
        return ""
    s = s.strip().translate(_PUNCT_MAP)
    s = re.sub(r"\s+", "", s)
    if len(s) >= 2 and s.startswith("(") and s.endswith(")"):
        s = s[1:-1]
    return s


def _compare_one(
    student_ans: str,
    standard_ans: Optional[str],
    variants: Optional[list] = None,
    *,
    question_type: str = "",
    choice_mode: str = "",
) -> str:
    """单题机械对比，返回 correct / partial / wrong。

    variants: 标准答案的等价变体列表（如题库 answer_variants），传入后参与数学等价判断。
    """
    if standard_ans is None or standard_ans == "":
        return "wrong"
    sa = student_ans or ""
    ca = standard_ans

    # Choice answers are compared only inside an explicit teacher-confirmed
    # single/multiple contract. Multi-select uses exact set equality and never
    # awards partial credit.
    if question_type == "choice" or choice_mode in {"single", "multiple"}:
        cs = normalize_choice_answer(sa)
        cc = normalize_choice_answer(ca)
        if not cs or not cc:
            return "wrong"
        if choice_mode == "single" and (len(cs) != 1 or len(cc) != 1):
            return "wrong"
        if choice_mode == "multiple" and (len(cs) < 2 or len(cc) < 2):
            return "wrong"
        choice_candidates = [cc]
        for variant in variants or []:
            normalized = normalize_choice_answer(variant)
            if normalized:
                choice_candidates.append(normalized)
        return "correct" if cs in choice_candidates else "wrong"

    # 数值字面量（整数/小数/分数/带分数）按精确值比较，必须先于去空格：
    # 否则带分数 25 2/5（=25.4）会被压成 252/5。
    if parse_number(sa) is not None and parse_number(ca) is not None:
        return "correct" if MathMatcher.is_equivalent(sa, ca, variants) else "wrong"

    # 直接相等（去空格）
    if sa.strip().replace(" ", "") == ca.strip().replace(" ", ""):
        return "correct"

    # 数学等价
    if MathMatcher.is_equivalent(sa, ca, variants):
        return "correct"

    # 深度规范化
    ns, nc = _deep_normalize(sa), _deep_normalize(ca)
    if ns and ns == nc:
        return "correct"
    if MathMatcher.is_equivalent(ns, nc, variants):
        return "correct"

    # 部分一致：一个包含另一个（如学生少写单位、漏一项）
    if ns and nc and (ns in nc or nc in ns):
        return "partial"

    return "wrong"
