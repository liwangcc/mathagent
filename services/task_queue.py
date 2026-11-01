"""推理层任务队列 — 状态机 + 并发控制 + 重试策略 + 幂等 + 按老师限流。

架构：
    学生卷转录 → 推理队列（本模块）→ qwen3.8-flash 判分

核心设计：
    1. asyncio.Semaphore 控制单个 worker 的本地请求窗口；provider 全局 500
       与教师 base/burst 额度由共享模型限流器裁决
    2. 每个 task 有 idempotency_key，用于进程内去重缓存（性能优化，非正确性保证）
    3. 状态机：pending → running → completed / failed / retrying
    4. 重试策略：区分 429（限流）/ 5xx（服务错误）/ 超时，指数退避
    5. 按老师限流：per-teacher semaphore，防止单人占满队列

幂等的边界（重要）：
    本模块的 _idempotent_map 是**进程内内存缓存**，作用仅限于：同一进程生命周期内，
        避免对同一 (idempotency_key) 重复调用判分模型，从而省成本、降延迟。

    它**不是**批改幂等的真相源，且天然有以下局限，调用方不得依赖它保证正确性：
      - 进程重启 / OOM 后缓存清空；
      - 多 worker 部署时各进程缓存互不可见；
      - 同一 key 的两个并发任务可能同时 miss（写入前都没命中）。

    批改结果**落库的幂等**由数据库唯一索引保证：
        grading_results(student_id, image_content_hash, category, exam_name)
    见 models.GradingResult 与 services.grading_persistence._insert_or_get_grading_result。
    即便本缓存完全失效，最坏后果是重复跑一次判分模型，落库仍只产生一行。
"""

import asyncio
import enum
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from services.retry_backoff import exponential_retry_delay, parse_retry_after_seconds

logger = logging.getLogger(__name__)

_DEFAULT_IDEMPOTENT_CACHE_TTL_SECONDS = 3600.0
_DEFAULT_IDEMPOTENT_CACHE_MAX_ENTRIES = 256


# ─── 状态机 ───

class TaskState(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"
    CANCELLED = "cancelled"


# ─── 错误分类 ───

class ErrorCategory(str, enum.Enum):
    RATE_LIMIT = "rate_limit"       # 429
    SERVER_ERROR = "server_error"   # 5xx
    TIMEOUT = "timeout"
    CLIENT_ERROR = "client_error"   # 4xx (非429)
    UNKNOWN = "unknown"


def classify_error(exc: Exception) -> ErrorCategory:
    """根据异常类型分类错误。"""
    import httpx
    if bool(getattr(exc, "retryable", False)):
        return ErrorCategory.SERVER_ERROR
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return ErrorCategory.TIMEOUT
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 429:
            return ErrorCategory.RATE_LIMIT
        if code >= 500:
            return ErrorCategory.SERVER_ERROR
        return ErrorCategory.CLIENT_ERROR
    if isinstance(exc, httpx.TransportError):
        return ErrorCategory.SERVER_ERROR
    return ErrorCategory.UNKNOWN


# ─── 重试策略 ───

@dataclass
class RetryPolicy:
    """按错误类型区分的重试策略。"""
    max_retries: int = 3
    # 429限流：等待更久，尊重 Retry-After
    rate_limit_base_delay: float = 5.0
    rate_limit_max_delay: float = 120.0
    # 5xx服务错误：指数退避
    server_error_base_delay: float = 2.0
    server_error_max_delay: float = 60.0
    # 超时：快速重试
    timeout_base_delay: float = 1.0
    timeout_max_delay: float = 30.0

    def get_delay(self, category: ErrorCategory, attempt: int,
                  retry_after: float | None = None) -> float:
        """计算第 attempt 次重试的等待秒数。"""
        if category == ErrorCategory.RATE_LIMIT:
            return exponential_retry_delay(
                attempt + 1,
                base_delay_s=self.rate_limit_base_delay,
                retry_after_s=retry_after,
                ceiling_s=self.rate_limit_max_delay,
            )
        if category == ErrorCategory.SERVER_ERROR:
            return exponential_retry_delay(
                attempt + 1,
                base_delay_s=self.server_error_base_delay,
                retry_after_s=retry_after,
                ceiling_s=self.server_error_max_delay,
            )
        if category == ErrorCategory.TIMEOUT:
            return exponential_retry_delay(
                attempt + 1,
                base_delay_s=self.timeout_base_delay,
                ceiling_s=self.timeout_max_delay,
            )
        # CLIENT_ERROR / UNKNOWN：不重试
        return 0

    def should_retry(
        self,
        category: ErrorCategory,
        attempt: int,
        retry_after: float | None = None,
    ) -> bool:
        """是否应该重试。"""
        if attempt >= self.max_retries:
            return False
        if retry_after is not None:
            delay_limit = {
                ErrorCategory.RATE_LIMIT: self.rate_limit_max_delay,
                ErrorCategory.SERVER_ERROR: self.server_error_max_delay,
            }.get(category)
            if delay_limit is not None and retry_after > delay_limit:
                return False
        return category in {
            ErrorCategory.RATE_LIMIT,
            ErrorCategory.SERVER_ERROR,
            ErrorCategory.TIMEOUT,
        }


DEFAULT_RETRY_POLICY = RetryPolicy()


def _retry_after_seconds(exc: Exception) -> float | None:
    """Read a provider Retry-After contract without retrying earlier than asked."""
    import httpx

    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    response = getattr(exc, "response", None)
    if response is None:
        return None
    return parse_retry_after_seconds(response.headers.get("Retry-After"))


# ─── Task ───

@dataclass
class Task:
    """单个推理任务。"""
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    idempotency_key: str = ""       # 防重复
    teacher_id: str = ""            # 按老师限流
    state: TaskState = TaskState.PENDING
    retry_count: int = 0
    error_category: ErrorCategory | None = None
    last_error: str = ""
    result: Any = None
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    completed_at: float = 0.0
    queue_wait_seconds: float = 0.0
    run_seconds: float = 0.0


# ─── TaskQueue ───

class TaskQueue:
    """推理层任务队列。

    用法：
        queue = TaskQueue(max_concurrent=500, per_teacher_limit=60)

        # 提交任务（coro_factory 是一个无参函数，返回 awaitable）
        result = await queue.submit(
            coro_factory=lambda: judge_paper(questions),
            idempotency_key="paper123_q3",
            teacher_id="teacher_42",
        )
    """

    def __init__(
        self,
        max_concurrent: int = 500,
        per_teacher_limit: int = 60,
        retry_policy: RetryPolicy | None = None,
        idempotent_cache_ttl_seconds: float = _DEFAULT_IDEMPOTENT_CACHE_TTL_SECONDS,
        idempotent_cache_max_entries: int = _DEFAULT_IDEMPOTENT_CACHE_MAX_ENTRIES,
    ):
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be positive")
        if per_teacher_limit < 1:
            raise ValueError("per_teacher_limit must be positive")
        if idempotent_cache_ttl_seconds <= 0:
            raise ValueError("idempotent_cache_ttl_seconds must be positive")
        if idempotent_cache_max_entries < 1:
            raise ValueError("idempotent_cache_max_entries must be positive")
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._per_teacher_limit = per_teacher_limit
        self._teacher_semaphores: dict[str, asyncio.Semaphore] = {}
        self._teacher_lock = asyncio.Lock()
        self._retry_policy = retry_policy or DEFAULT_RETRY_POLICY

        # 进程内去重缓存：idempotency_key → completed Task。
        # 仅为性能优化（省一次判分调用），非正确性保证 —— 见模块 docstring「幂等的边界」。
        # 落库幂等由 DB 唯一索引兜底，本表丢失只会导致重复跑模型，不会重复落库。
        self._idempotent_map: dict[str, Task] = {}
        self._idempotent_lock = asyncio.Lock()
        self._idempotent_cache_ttl_seconds = float(idempotent_cache_ttl_seconds)
        self._idempotent_cache_max_entries = int(idempotent_cache_max_entries)

        # 统计
        self._total_submitted = 0
        self._total_completed = 0
        self._total_failed = 0
        self._total_cancelled = 0
        self._total_retries = 0
        self._retry_counts: dict[str, int] = {}
        self._active_tasks = 0
        self._peak_active_tasks = 0
        self._queue_wait_seconds_total = 0.0
        self._run_seconds_total = 0.0

    def _prune_idempotent_cache_locked(self, now: float) -> int:
        expired = [
            key
            for key, cached in self._idempotent_map.items()
            if now - cached.completed_at > self._idempotent_cache_ttl_seconds
        ]
        for key in expired:
            del self._idempotent_map[key]
        return len(expired)

    def _make_idempotent_cache_room_locked(self) -> None:
        while len(self._idempotent_map) >= self._idempotent_cache_max_entries:
            oldest_key = min(
                self._idempotent_map,
                key=lambda key: self._idempotent_map[key].completed_at,
            )
            del self._idempotent_map[oldest_key]

    async def _get_teacher_semaphore(self, teacher_id: str) -> asyncio.Semaphore:
        """获取或创建老师的限流信号量。"""
        if not teacher_id:
            return asyncio.Semaphore(self._per_teacher_limit)  # 无限制的临时sem
        async with self._teacher_lock:
            if teacher_id not in self._teacher_semaphores:
                self._teacher_semaphores[teacher_id] = asyncio.Semaphore(
                    self._per_teacher_limit
                )
            return self._teacher_semaphores[teacher_id]

    async def submit(
        self,
        coro_factory: Callable[[], Coroutine],
        idempotency_key: str = "",
        teacher_id: str = "",
        task_id: str = "",
    ) -> Any:
        """提交一个推理任务，返回结果。

        - 去重缓存：相同 idempotency_key 在**本进程内**直接返回缓存结果（性能优化，
          非正确性保证；重启/多 worker/并发首次 miss 时会重复跑，落库幂等另由 DB 兜底）
        - 并发：进程本地信号量 + per-teacher 本地 burst 信号量；跨 worker 的
          provider 全局额度由共享模型限流器裁决
        - 重试：按错误类型指数退避

        Args:
            coro_factory: 无参协程工厂（每次重试需要新的协程）
            idempotency_key: 进程内去重键（如 "paper_123_q3" 的 hash）
            teacher_id: 老师ID（用于限流）
            task_id: 可选的任务ID

        Returns:
            协程的返回值

        Raises:
            最后一次重试仍失败时抛出原始异常
        """
        task = Task(
            task_id=task_id or uuid.uuid4().hex[:12],
            idempotency_key=idempotency_key,
            teacher_id=teacher_id,
        )
        self._total_submitted += 1

        # ── 进程内去重缓存检查（命中即省一次判分调用；未命中不影响正确性）──
        if idempotency_key:
            async with self._idempotent_lock:
                self._prune_idempotent_cache_locked(time.time())
                existing = self._idempotent_map.get(idempotency_key)
                if existing and existing.state == TaskState.COMPLETED:
                    logger.info("TaskQueue: 去重缓存命中 %s → 返回缓存结果（省一次模型调用）", idempotency_key)
                    return existing.result

        # ── 获取信号量 ──
        teacher_sem = await self._get_teacher_semaphore(teacher_id)

        last_exc: Exception | None = None

        for attempt in range(self._retry_policy.max_retries + 1):
            task.state = TaskState.RUNNING if attempt == 0 else TaskState.RETRYING
            task.retry_count = attempt

            # Acquire permits for the model request only.  In particular, the
            # teacher and process-local slots must be released before retry
            # backoff so another task can use otherwise idle capacity.
            attempt_run_started = 0.0
            attempt_counted_active = False
            try:
                async with teacher_sem:  # per-teacher limit for this attempt
                    async with self._semaphore:  # process-local request limit
                        admitted_at = time.time()
                        if task.started_at <= 0:
                            task.started_at = admitted_at
                            task.queue_wait_seconds = max(
                                0.0, admitted_at - task.created_at
                            )
                            self._queue_wait_seconds_total += task.queue_wait_seconds
                        self._active_tasks += 1
                        self._peak_active_tasks = max(
                            self._peak_active_tasks, self._active_tasks
                        )
                        attempt_counted_active = True
                        attempt_run_started = time.monotonic()
                        try:
                            result = await coro_factory()
                        finally:
                            elapsed = max(0.0, time.monotonic() - attempt_run_started)
                            task.run_seconds += elapsed
                            self._run_seconds_total += elapsed
                            self._active_tasks = max(0, self._active_tasks - 1)
                            attempt_counted_active = False
            except asyncio.CancelledError:
                # Cancellation is terminal for this logical task.  The context
                # managers above release both local permits before this branch
                # records the event, and cancellation is never retried.
                if attempt_counted_active:
                    self._active_tasks = max(0, self._active_tasks - 1)
                task.state = TaskState.CANCELLED
                task.completed_at = time.time()
                self._total_cancelled += 1
                raise
            except Exception as exc:
                last_exc = exc
                cat = classify_error(exc)
                retry_after = _retry_after_seconds(exc)
                task.error_category = cat
                task.last_error = str(exc)[:200]

                logger.warning(
                    "TaskQueue: %s 失败 (attempt=%d, %s): %s",
                    task.task_id, attempt + 1, cat.value, task.last_error,
                )

                if not self._retry_policy.should_retry(cat, attempt, retry_after):
                    break

                # Backoff deliberately happens after both semaphores have
                # exited; failed requests must not reserve capacity while idle.
                delay = self._retry_policy.get_delay(cat, attempt, retry_after)
                self._total_retries += 1
                self._retry_counts[cat.value] = self._retry_counts.get(cat.value, 0) + 1
                task.state = TaskState.RETRYING
                if delay > 0:
                    logger.info("TaskQueue: %s 退避 %.1fs 后重试", task.task_id, delay)
                    try:
                        await asyncio.sleep(delay)
                    except asyncio.CancelledError:
                        task.state = TaskState.CANCELLED
                        task.completed_at = time.time()
                        self._total_cancelled += 1
                        raise
                continue

            task.state = TaskState.COMPLETED
            task.result = result
            task.completed_at = time.time()

            # Fail-closed placeholders represent an unavailable model, not a
            # completed judgment.  Return them to the caller for fault
            # propagation, but never replay them after the provider recovers.
            cacheable = getattr(result, "__task_queue_cacheable__", True) is not False
            if idempotency_key and cacheable:
                async with self._idempotent_lock:
                    self._prune_idempotent_cache_locked(task.completed_at)
                    if idempotency_key not in self._idempotent_map:
                        self._make_idempotent_cache_room_locked()
                    self._idempotent_map[idempotency_key] = task

            elapsed = task.completed_at - task.created_at
            logger.info(
                "TaskQueue: %s 完成 (attempt=%d, %.1fs)",
                task.task_id, attempt + 1, elapsed,
            )
            self._total_completed += 1
            return result

        # 所有重试用尽
        task.state = TaskState.FAILED
        task.completed_at = time.time()
        self._total_failed += 1
        raise last_exc  # type: ignore[misc]

    def stats(self) -> dict:
        """返回队列统计信息。"""
        return {
            "total_submitted": self._total_submitted,
            "total_completed": self._total_completed,
            "total_failed": self._total_failed,
            "total_cancelled": self._total_cancelled,
            "total_retries": self._total_retries,
            "retries_by_category": dict(self._retry_counts),
            "active_tasks": self._active_tasks,
            "peak_active_tasks": self._peak_active_tasks,
            "queue_wait_seconds_total": round(self._queue_wait_seconds_total, 6),
            "run_seconds_total": round(self._run_seconds_total, 6),
            "average_queue_wait_seconds": round(
                self._queue_wait_seconds_total / self._total_submitted,
                6,
            ) if self._total_submitted else 0.0,
            "average_run_seconds": round(
                self._run_seconds_total / self._total_completed,
                6,
            ) if self._total_completed else 0.0,
            "idempotent_cache_size": len(self._idempotent_map),
            "teacher_count": len(self._teacher_semaphores),
        }


# ─── 全局单例 ───

_grading_queue: TaskQueue | None = None


def get_grading_queue() -> TaskQueue:
    """获取数学判分队列单例（判分只走 qwen3.8-flash）。"""
    global _grading_queue
    if _grading_queue is None:
        from services.flash_grading import FLASH_LOCAL_MAX_CONCURRENT
        # This is a per-worker pressure valve sized from the Flash request
        # lane.  Provider-wide capacity and cross-worker fairness are enforced
        # by the shared model limiter; retired MiMo windows must not cap it.
        local_concurrent = FLASH_LOCAL_MAX_CONCURRENT
        # Shared Redis fairness decides which waiting teacher receives the
        # next slot; a fixed local 60-slot cap would strand idle Flash capacity.
        local_teacher_limit = local_concurrent
        _grading_queue = TaskQueue(
            max_concurrent=local_concurrent,
            per_teacher_limit=local_teacher_limit,
            # A whole paper is expensive. One complete retry is enough to
            # absorb a transient provider failure; the router preserves a
            # second failure as pending work instead of looping indefinitely.
            retry_policy=RetryPolicy(max_retries=1),
        )
        logger.info(
            "TaskQueue: 判分队列初始化 — Flash 本进程并发 %d, 老师 burst %d",
            local_concurrent, local_teacher_limit,
        )
    return _grading_queue


def make_idempotency_key(grading_key: int | str, question_index: int) -> str:
    """生成进程内去重键：基于业务批改键 + 题号索引。

    grading_key 应传内容派生的稳定键（如图片 sha256），使同卷重复提交得到相同 key、
    命中进程内缓存省一次判分调用。注意：本键仅服务于 task_queue 的内存去重缓存，
    **不承担正确性**——重启/多 worker/并发首次 miss 时缓存失效是预期内的。批改落库的
    幂等真相源是数据库唯一索引（grading_results 的
    student_id+image_content_hash+category+exam_name），与本键无关。
    """
    raw = f"grade_{grading_key}_q{question_index}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]
