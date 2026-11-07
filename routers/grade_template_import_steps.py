"""How a template import is paced: a page-scaled deadline, one slow step at a time, and the failures a teacher sees.

- The deadline: ``_template_import_deadline_seconds`` gives one import its
  budget (the ``_TEMPLATE_IMPORT_*`` constants) and
  ``_template_page_model_deadline`` shares what is left among the remaining
  pages; both are measured on ``_template_import_clock``.
- One slow step: ``_TemplateImportSteps`` starts the step's task and polls it
  one heartbeat interval (``_TEMPLATE_COMPONENT_HEARTBEAT_S``) at a time
  through ``_poll_template_component``, which stops the task and raises
  ``_TemplateImportDeadlineExceeded`` once the deadline has passed.
  ``_run_template_cpu_component`` runs image work behind the shared CPU gate;
  ``_template_heartbeat`` and ``_template_model_progress_message`` word the
  heartbeats.
- Failures: ``_TemplateRecognitionFailure`` carries a code and a
  teacher-facing message; ``_template_recognition_failure`` maps a provider
  error to one and ``_template_import_deadline_failure`` words the timeout.

Moved unchanged from routers/grade_template.py, which still exposes these names.
"""

import asyncio

from config import MAX_TEMPLATE_PAGES
from routers.grade_common import _settle_template_component
from services.ocr_service import (
    _get_local_ocr_preprocess_semaphore,
    _run_local_ocr_image_component,
)


# A healthy page normally completes in roughly two minutes.  Keep a bounded
# eight-minute ceiling for a slow provider/failover without making teachers
# wait for the old accelerator queue (which could be tens of minutes).
# Leave one minute for admission, validation, and persistence around the
# eight-minute provider attempt.
_TEMPLATE_IMPORT_PAGE_HARD_CAP_S = 9 * 60.0
_TEMPLATE_IMPORT_PER_PAGE_DEADLINE_S = _TEMPLATE_IMPORT_PAGE_HARD_CAP_S
_TEMPLATE_IMPORT_BASE_DEADLINE_S = _TEMPLATE_IMPORT_PER_PAGE_DEADLINE_S
_TEMPLATE_IMPORT_PER_ADDITIONAL_PAGE_S = _TEMPLATE_IMPORT_PER_PAGE_DEADLINE_S
_TEMPLATE_IMPORT_MAX_DEADLINE_S = (
    _TEMPLATE_IMPORT_BASE_DEADLINE_S
    + _TEMPLATE_IMPORT_PER_ADDITIONAL_PAGE_S * (MAX_TEMPLATE_PAGES - 1)
)
_TEMPLATE_COMPONENT_HEARTBEAT_S = 15.0
_TEMPLATE_MODEL_SAVE_RESERVE_S = 30.0


class _TemplateRecognitionFailure(RuntimeError):
    """Expected template OCR failure that must be shown to the teacher."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _TemplateImportDeadlineExceeded(TimeoutError):
    """The whole template import exhausted its page-scaled time budget."""


def _template_import_clock() -> float:
    """Read the clock the import deadline is measured on: the event loop's."""
    return asyncio.get_running_loop().time()


def _template_import_deadline_seconds(page_count: int) -> float:
    pages = max(1, int(page_count))
    return min(
        _TEMPLATE_IMPORT_MAX_DEADLINE_S,
        _TEMPLATE_IMPORT_BASE_DEADLINE_S
        + _TEMPLATE_IMPORT_PER_ADDITIONAL_PAGE_S * (pages - 1),
    )


def _template_page_model_deadline(deadline_at: float, remaining_pages: int) -> float:
    """Share remaining time fairly across pages and reserve artifact/save time.

    ``deadline_at`` is on the import clock; the model stages time out on the
    event loop's clock, so the page's deadline is returned as a loop time.
    """
    now = asyncio.get_running_loop().time()
    per_page = max(0.0, deadline_at - _template_import_clock()) / max(1, remaining_pages)
    return now + max(0.0, per_page - _TEMPLATE_MODEL_SAVE_RESERVE_S)


def _template_model_progress_message(page_index: int, progress: dict) -> str:
    labels = {
        "template-merged-v1": "识别题目、定位作答区并生成标准答案",
        "template-directory-v1": "识别题目目录",
        "template-directory-drawing-v1": "定位作图区域",
    }
    running = []
    for phase, state in (progress.get("stages") or {}).items():
        if phase in labels and state.get("status") == "running":
            running.append(labels[phase])
    description = "、".join(running) or "整理已完成结果"
    return f"第 {page_index + 1} 页正在{description}"


def _template_import_deadline_failure(
    deadline_seconds: float,
) -> _TemplateRecognitionFailure:
    minutes = max(1, (int(deadline_seconds) + 59) // 60)
    return _TemplateRecognitionFailure(
        "TEMPLATE_IMPORT_TIMEOUT",
        f"模板导入已达到本次 {minutes} 分钟总时限，所有在途步骤均已清理；"
        "本次模板不可用，请重新发起导入",
    )


async def _poll_template_component(
    task: asyncio.Task,
    *,
    deadline_at: float,
    cancel_running: bool = True,
) -> tuple[bool, object | None]:
    """Wait for one heartbeat interval while enforcing the absolute deadline."""
    remaining_s = deadline_at - _template_import_clock()
    if remaining_s <= 0:
        await _settle_template_component(task, cancel_running=cancel_running)
        raise _TemplateImportDeadlineExceeded
    try:
        done, _pending = await asyncio.wait(
            {task},
            timeout=min(_TEMPLATE_COMPONENT_HEARTBEAT_S, remaining_s),
        )
        if task in done:
            return True, task.result()
        if _template_import_clock() >= deadline_at:
            await _settle_template_component(task, cancel_running=cancel_running)
            raise _TemplateImportDeadlineExceeded
        return False, None
    except asyncio.CancelledError:
        await _settle_template_component(task, cancel_running=cancel_running)
        raise


async def _run_template_cpu_component(function, /, *args, **kwargs):
    """Run heavy image work behind the shared gate and stall readiness tracker."""
    return await _run_local_ocr_image_component(
        function,
        *args,
        semaphore=_get_local_ocr_preprocess_semaphore(),
        **kwargs,
    )


def _template_recognition_failure(error: str = "") -> _TemplateRecognitionFailure:
    """Map provider details to safe, actionable teacher-facing messages."""
    normalized = str(error or "").lower()
    if any(marker in normalized for marker in ("401", "unauthorized", "invalid api key", "authentication")):
        return _TemplateRecognitionFailure(
            "VISION_AUTH_FAILED",
            "视觉识别服务认证失败，请联系管理员检查视觉识别服务的密钥配置",
        )
    if any(marker in normalized for marker in (
        "absolute_attempt_ttl",
        "first_sse_timeout",
        "response_header_timeout",
        "sse_idle_timeout",
        "pool_dispatch_deadline",
    )):
        return _TemplateRecognitionFailure(
            "VISION_TIMEOUT",
            "视觉识别服务响应超时，本次未返回完整题目；请重新识别",
        )
    if str(error or "").startswith("模板 OCR "):
        return _TemplateRecognitionFailure(
            "TEMPLATE_OUTPUT_INVALID",
            "模板识别结果格式异常，请重新识别；若仍失败请联系管理员",
        )
    if error:
        return _TemplateRecognitionFailure(
            "TEMPLATE_OCR_FAILED",
            "模板题目识别失败，请稍后重试；若持续发生请联系管理员检查视觉识别服务",
        )
    return _TemplateRecognitionFailure(
        "TEMPLATE_NO_QUESTIONS",
        "模板未识别到任何题目，请检查图片是否完整、清晰后重新上传",
    )


class _TemplateImportSteps:
    """The one slow step a template import is waiting on.

    ``start`` registers the step's task. ``poll`` waits one heartbeat interval,
    raising once the import deadline has passed, and returns True when the step
    is done, leaving its value in ``result``. ``settle`` stops a step that is
    still running when the import ends early.
    """

    def __init__(self, deadline_at: float) -> None:
        self.deadline_at = deadline_at
        self.task: asyncio.Task | None = None
        self.result = None

    def start(self, task: asyncio.Task) -> None:
        self.task = task

    async def poll(self) -> bool:
        complete, result = await _poll_template_component(
            self.task,
            deadline_at=self.deadline_at,
        )
        if complete:
            self.task = None
            self.result = result
        return complete

    async def settle(self) -> None:
        if self.task is not None:
            await _settle_template_component(self.task, cancel_running=True)


def _template_heartbeat(index: int, total: int, activity: str) -> dict:
    return {
        "message": f"第 {index + 1} 页正在{activity}",
        "current": index + 1,
        "total": total,
        "heartbeat": True,
    }
