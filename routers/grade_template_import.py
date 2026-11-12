"""The template import stream: POST /batch-grade/template/recognize and its saved status.

- ``batch_grade_template_recognize`` validates and stores the upload, then
  runs the import as a background task (``_relay_template_recognition``,
  tracked in ``_TEMPLATE_RECOGNITION_TASKS``); the SSE response only
  subscribes to its bounded progress (``_TemplateRecognitionEventBuffer``).
- ``_template_import_events`` takes every page through correction, identity,
  layout and the model stages (``_template_page_events``), then freezes each
  page's geometry (``_freeze_template_page_events``) and checks the whole
  paper's numbering (``_finish_template_import``); a failure is saved by
  ``_fail_template_import``.
- GET /batch-grade/template/{template_id}/recognition
  (``batch_grade_template_recognition_status``) reads the saved result,
  including an import that ran past its deadline.

Moved unchanged from routers/grade_template.py, which still exposes these names
and includes this module's router before its own routes.
"""

import asyncio
import copy
import hashlib
import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from database import get_db
from models_user import User
from routers.auth import _require_user
from routers.grade_common import (
    _load_owned_template_session,
    _new_template_session,
    _sse,
    _template_knowledge_classifier,
)
from routers.grade_template_availability import _require_template_recognition_chain
from routers.grade_template_import_form import (
    _TemplateImport,
    _parse_template_import_form,
    _save_template_import,
)
from routers.grade_template_import_pages import (
    _RecognizedTemplatePage,
    _bind_template_import_artifact,
    _recognized_template_page,
    _record_template_import_layout,
)
from routers.grade_template_import_steps import (
    _TemplateImportDeadlineExceeded,
    _TemplateImportSteps,
    _TemplateRecognitionFailure,
    _run_template_cpu_component,
    _template_heartbeat,
    _template_import_clock,
    _template_import_deadline_failure,
    _template_import_deadline_seconds,
    _template_model_progress_message,
    _template_page_model_deadline,
)
from services import template_knowledge
from services.analyzer import reset_grading_teacher_id, set_grading_teacher_id
from services.ocr_service import reset_vision_teacher_id, set_vision_teacher_id
from services.processing_errors import report_failure
from services.teaching_context import (
    await_teaching,
    make_teaching_context,
    require_teacher_stage,
    stream_teaching,
)
from services import template_cross_page as cross_page
from services.template_anchor_crops import AnchorCropPlanError
from services.template_column_layout import (
    TemplateCutError,
    layout_needs_review,
    recognize_column_layout,
    whole_page_fallback_layout,
)
from services.template_evidence import (
    CanonicalTemplate,
    TemplateEvidenceError,
    accept_canonical_template_candidate,
    build_template_artifact,
    canonicalize_template_image,
)
from services.template_page_identity import compute_visual_fingerprint
from services.template_page_numbering import validate_global_numbering
from services.template_recognition import recognize_template_pipeline
from services.template_session import (
    GRADED_PAGE_ROLES,
    TemplateSession,
    normalize_template_mode,
)

# routers.grade_template's logger, so these records keep their logger name.
logger = logging.getLogger("routers.grade_template")
router = APIRouter()


_TEMPLATE_EVENT_BUFFER_SIZE = 64
_TEMPLATE_RECOGNITION_TASKS: set[asyncio.Task] = set()


class _TemplateRecognitionEventBuffer:
    """Bound progress buffering without coupling work to one SSE subscriber."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[str, bool]] = asyncio.Queue(
            maxsize=_TEMPLATE_EVENT_BUFFER_SIZE
        )

    def publish(self, chunk: str) -> None:
        terminal = chunk.startswith(("event: done\n", "event: error\n"))
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self._queue.put_nowait((chunk, terminal))

    async def subscribe(self):
        while True:
            chunk, terminal = await self._queue.get()
            yield chunk
            if terminal:
                return


def _track_template_recognition_task(task: asyncio.Task) -> None:
    _TEMPLATE_RECOGNITION_TASKS.add(task)

    def finished(done: asyncio.Task) -> None:
        _TEMPLATE_RECOGNITION_TASKS.discard(done)
        if done.cancelled():
            return
        error = done.exception()
        if error is not None:
            report_failure(error, stage="template")

    task.add_done_callback(finished)


async def _relay_template_recognition(
    source,
    events: _TemplateRecognitionEventBuffer,
    sess: TemplateSession,
) -> None:
    """Run recognition independently and publish only bounded SSE progress."""
    try:
        async for chunk in source:
            events.publish(chunk)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        sess.error_info = report_failure(exc, stage="template", task_id=sess.template_id)
        public_message = sess.error_info["message"]
        sess.status = "failed"
        sess.error_code = "TEMPLATE_BACKGROUND_TASK_FAILED"
        sess.error = public_message
        try:
            sess.save_meta()
        except Exception:
            logger.exception("模板后台故障状态保存失败 template=%s", sess.template_id)
        events.publish(_sse("error", {
            "code": sess.error_code,
            "message": public_message,
            "error_info": sess.error_info,
        }))


def _template_recognition_status_payload(sess: TemplateSession) -> dict:
    status = str(sess.status or "pending")
    error_code = str(sess.error_code or "")
    error = str(sess.error or "")
    if status == "recognizing" and sess.recognition_deadline_at:
        try:
            deadline = datetime.fromisoformat(sess.recognition_deadline_at)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= deadline.astimezone(timezone.utc):
                status = "failed"
                error_code = "TEMPLATE_IMPORT_TIMEOUT"
                error = "模板导入已超过服务器总时限，本次模板不可用；请重新发起导入"
        except (TypeError, ValueError):
            status = "failed"
            error_code = "TEMPLATE_SESSION_INVALID"
            error = "模板任务状态损坏，本次模板不可用；请重新发起导入"

    ready = status in {"ocr_done", "confirmed", "frozen"}
    terminal = ready or status == "failed"
    return {
        "success": ready,
        "template_id": sess.template_id,
        "teaching_context": copy.deepcopy(sess.teaching_context),
        "template_mode": sess.template_mode,
        "column_mode": getattr(sess, "column_mode", "double") or "double",
        "status": status,
        "terminal": terminal,
        "page_count": len(sess.pages),
        "pages": sess.pages if ready else [],
        "category": sess.category,
        "exam_name": sess.exam_name,
        "error_code": error_code,
        "error": error,
        "error_info": getattr(sess, "error_info", {}),
        "message": (
            "模板识别已完成"
            if ready
            else error
            if terminal
            else "模板仍在服务器处理中，请勿重复提交"
        ),
        "recognition_started_at": sess.recognition_started_at,
        "recognition_deadline_at": sess.recognition_deadline_at,
        "retry_after_ms": 1000 if not terminal else 0,
    }


@router.get("/batch-grade/template/{template_id}/recognition")
def batch_grade_template_recognition_status(
    template_id: str,
    user: User = Depends(_require_user),
):
    """Read a persisted template result without requiring model readiness."""
    sess = _load_owned_template_session(template_id, user.id)
    if not os.path.isfile(sess.meta_path):
        raise HTTPException(404, detail="模板任务不存在")
    return _template_recognition_status_payload(sess)


@router.post("/batch-grade/template/recognize")
async def batch_grade_template_recognize(
    files: list[UploadFile] = File(...),
    canonical_files: list[UploadFile] | None = File(None),
    canonical_metadata: str | None = Form(None),
    has_answers: bool = Form(True),
    template_mode: str = Form(""),
    column_mode: str = Form("double"),
    layout_mode: str = Form(""),
    category: str = Form(...),
    exam_name: str = Form(""),
    page_roles: str = Form(""),
    page_labels: str = Form(""),
    user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    """模板阶段一：OCR N 张模板图（N=试卷页数），返回逐页题目供老师确认（SSE）。

    ``template_mode`` 是模板区域合同的一部分：blank 由视觉模型定位空白
    模板的印刷作答区域，marked 定位已有手写豁免区域；两种模式都复用
    同一份题号位置。两种模式都由独立数学模型生成标准答案；``has_answers``
    仅为旧客户端保留，不决定标准答案。

    SSE events:
      - progress: {message, current, total}
      - page:     {page_index, question_count}
      - done:     {template_id, page_count, pages:[...]}
      - error:    {message}
    """
    form = _parse_template_import_form(
        files,
        canonical_files,
        canonical_metadata,
        has_answers=has_answers,
        template_mode=template_mode,
        column_mode=column_mode,
        layout_mode=layout_mode,
        category=category,
        exam_name=exam_name,
        page_roles=page_roles,
        page_labels=page_labels,
    )
    teaching_context = make_teaching_context(require_teacher_stage(user))
    await _require_template_recognition_chain(user)

    teacher_id = int(user.id)
    template_id = uuid.uuid4().hex[:12]
    sess = _new_template_session(template_id, teacher_id, teaching_context)
    # Keep the legacy field readable for old clients, but normalize new sessions
    # to the generated-answer semantics. No new code may use it to decide how
    # template OCR or differential evidence is produced.
    sess.has_answers = False
    sess.template_mode = normalize_template_mode(
        form.template_mode,
        default="marked",
    )
    sess.column_mode = form.column_mode
    sess.category = form.category
    sess.exam_name = form.exam_name
    os.makedirs(sess.template_dir, exist_ok=True)

    job = await _save_template_import(
        sess,
        teacher_id,
        form,
        files,
        canonical_files,
        _template_import_deadline_seconds(len(files)),
    )
    recognition_started = datetime.now(timezone.utc)
    sess.status = "recognizing"
    sess.recognition_started_at = recognition_started.isoformat()
    sess.recognition_deadline_at = (
        recognition_started + timedelta(seconds=job.deadline_s)
    ).isoformat()
    try:
        sess.save_meta()
    except Exception:
        shutil.rmtree(sess.template_dir, ignore_errors=True)
        raise

    events = _TemplateRecognitionEventBuffer()
    recognition_task = asyncio.create_task(
        _relay_template_recognition(
            stream_teaching(sess.teaching_context, _template_import_events(job)),
            events,
            sess,
        ),
        name=f"template-recognition-{template_id}",
    )
    _track_template_recognition_task(recognition_task)
    return StreamingResponse(
        events.subscribe(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Template-ID": template_id,
        },
    )


async def _fail_template_import(
    sess: TemplateSession,
    error: BaseException,
    *,
    code: str,
    message: str = "",
    kind: str,
) -> str:
    """Persist a failed import and return the teacher-facing SSE error."""
    sess.error_info = report_failure(error, stage="template", task_id=sess.template_id)
    message = message or sess.error_info["message"]
    sess.status = "failed"
    sess.error_code = code
    sess.error = message
    try:
        await asyncio.to_thread(sess.save_meta)
    except Exception:
        logger.exception("模板%s状态保存失败 template=%s", kind, sess.template_id)
    return _sse("error", {"code": code, "message": message, "error_info": sess.error_info})


@dataclass
class _TemplatePageImport:
    """One uploaded template page as the import works through its phases."""

    index: int
    path: str
    role: str
    identity: dict | None = None
    canonical: CanonicalTemplate | None = None
    layout_result: dict | None = None
    layout_review_pending: bool = False
    # What the model stages read: a whole-page fallback while the proposed
    # column layout waits for the teacher's review.
    model_layout: dict | None = None
    model_output: dict | None = None
    recognized: _RecognizedTemplatePage | None = None
    artifact_questions: list[dict] | None = None
    # The page uploaded before this one: a question it cut off continues here.
    previous: cross_page.PreviousPage | None = None


async def _correct_template_page_events(
    job: _TemplateImport,
    steps: _TemplateImportSteps,
    page: _TemplatePageImport,
):
    """Accept the client's canonical candidate or rectify the photo locally."""
    idx = page.index
    if job.canonical_inputs is not None:
        source_path, candidate_path, metadata = job.canonical_inputs[idx]
        try:
            steps.start(asyncio.create_task(_run_template_cpu_component(
                accept_canonical_template_candidate,
                source_path,
                candidate_path,
                metadata,
                job.sess.template_dir,
                idx,
            )))
            while not await steps.poll():
                yield _sse("progress", _template_heartbeat(idx, job.total, "校验标准模板候选"))
        except ValueError as exc:
            raise _TemplateRecognitionFailure(
                "TEMPLATE_CANONICAL_CANDIDATE_INVALID",
                "客户端标准模板候选校验失败；本次模板不可用，请重新导入",
            ) from exc
        page.canonical = steps.result
        return
    try:
        steps.start(asyncio.create_task(_run_template_cpu_component(
            canonicalize_template_image,
            page.path,
            job.sess.template_dir,
            idx,
        )))
        while not await steps.poll():
            yield _sse("progress", _template_heartbeat(idx, job.total, "本地完成页面矫正"))
        canonical = steps.result
        if (
            not isinstance(canonical, CanonicalTemplate)
            or not canonical.image_path
            or not isinstance(canonical.canonicalization, dict)
        ):
            raise ValueError("local canonicalization result is invalid")
    except _TemplateImportDeadlineExceeded:
        raise
    except Exception as exc:
        raise _TemplateRecognitionFailure(
            "TEMPLATE_CANONICALIZATION_FAILED",
            "模板页面矫正失败，请确认图片边界完整、方向正确且清晰后重新导入",
        ) from exc
    page.canonical = canonical


async def _template_page_identity(job: _TemplateImport, page: _TemplatePageImport) -> dict:
    """The printed label the teacher gave the page, plus its visual fingerprint."""
    identity = {"printed_page_label": job.page_labels[page.index]}
    try:
        identity["visual_fingerprint"] = await asyncio.to_thread(
            compute_visual_fingerprint, page.canonical.image_path,
        )
    except (OSError, ValueError):
        # Routing keeps its number/label gates without this hint.
        logger.warning(
            "模板页视觉指纹生成失败 template=%s page=%s", job.sess.template_id, page.index + 1,
        )
    return identity


async def _template_layout_events(
    job: _TemplateImport,
    steps: _TemplateImportSteps,
    page: _TemplatePageImport,
):
    """Propose the page's column layout.

    A low-confidence or rejected proposal stays a teacher-review draft: the
    model stages fall back to one whole-page input instead of unverified
    crops, and confirmation is refused until the teacher saves the boundaries.
    The template is not failed.
    """
    try:
        steps.start(asyncio.create_task(recognize_column_layout(
            page.canonical.image_path,
            teacher_id=str(job.teacher_id),
            column_mode=job.column_mode,
        )))
        while not await steps.poll():
            yield _sse("progress", _template_heartbeat(page.index, job.total, "识别题目布局"))
        page.layout_result = steps.result
        page.layout_review_pending = layout_needs_review(page.layout_result)
        page.model_layout = (
            whole_page_fallback_layout(page.layout_result)
            if page.layout_review_pending
            else page.layout_result
        )
    except (asyncio.CancelledError, _TemplateImportDeadlineExceeded):
        # The deadline is a TimeoutError, i.e. an OSError: report it
        # as the import timeout, not as a crop failure.
        raise
    except (TemplateCutError, AnchorCropPlanError, TemplateEvidenceError, OSError, ValueError) as exc:
        raise _TemplateRecognitionFailure(
            "TEMPLATE_CROP_FAILED",
            "模板分栏裁剪失败，已停止整页 OCR，请重新导入清晰完整的模板",
        ) from exc


async def _template_model_events(
    job: _TemplateImport,
    steps: _TemplateImportSteps,
    page: _TemplatePageImport,
):
    """Run the model stages under this page's share of the import deadline.

    Transcription, directory, generated answers and drawing positions; each
    heartbeat reports the stages' progress.
    """
    from services.template_directory_pipeline import template_import_context

    sess = job.sess
    idx = page.index
    model_progress = {
        "trace_id": hashlib.sha256(sess.template_id.encode()).hexdigest()[:12] + f"p{idx + 1}",
        "stages": {},
    }
    operation = recognize_template_pipeline(
        page.canonical.image_path,
        template_mode=sess.template_mode,
        # Review-pending pages send one whole-page input; the
        # teacher-facing review draft is persisted separately.
        layout_result=page.model_layout,
        on_text_ready=lambda transcription: template_knowledge.start_page_classification(
            sess, idx, transcription, classifier=_template_knowledge_classifier,
        ),
    )
    # The task copies the teacher ids and the deadline context when created.
    vision_token = set_vision_teacher_id(str(job.teacher_id))
    grading_token = set_grading_teacher_id(str(job.teacher_id))
    try:
        with template_import_context(
            deadline_at=_template_page_model_deadline(job.deadline_at, job.total - idx),
            progress=model_progress,
        ), cross_page.after(page.previous, role=page.role):
            steps.start(asyncio.create_task(
                await_teaching(sess.teaching_context, operation)
            ))
    finally:
        reset_grading_teacher_id(grading_token)
        reset_vision_teacher_id(vision_token)
    while not await steps.poll():
        yield _sse("progress", {
            "message": _template_model_progress_message(idx, model_progress),
            "current": idx + 1,
            "total": job.total,
            "heartbeat": True,
            "model_stages": copy.deepcopy(model_progress["stages"]),
        })
    page.model_output = steps.result
    yield _sse("progress", {
        "message": f"第 {idx + 1} 页模型阶段已结束，正在保存识别草稿",
        "current": idx + 1, "total": job.total,
        "model_stages": copy.deepcopy(model_progress["stages"]),
    })


async def _save_recognized_template_page(job: _TemplateImport, page: _TemplatePageImport) -> None:
    """Save the recognized page before a later geometry stage or the connection can fail."""
    sess = job.sess
    idx = page.index
    model_output, recognized = _recognized_template_page(
        page.model_output,
        template_id=sess.template_id,
        page_index=idx,
        question_offset=sum(len(saved.get("questions") or []) for saved in sess.pages),
        layout_result=page.layout_result,
        layout_review_pending=page.layout_review_pending,
    )
    page.identity["printed_question_labels"] = [
        str((question.get("printed_path") or [""])[-1] or "")
        for question in recognized.questions
    ]
    sess.add_page(idx, recognized.questions, page_role=page.role, page_identity=page.identity)
    try:
        template_knowledge.bind_page_knowledge(sess, idx, recognized.questions)
    except Exception as exc:
        logger.warning("模板知识点绑定暂缓 template=%s: %s", sess.template_id, type(exc).__name__)
    if isinstance(model_output.get("question_directory"), dict):
        sess.question_directories[f"page_{idx + 1}"] = model_output["question_directory"]
    await asyncio.to_thread(sess.save_meta)
    page.recognized = recognized
    page.artifact_questions = recognized.artifact_questions()
    if recognized.layout_result is not None:
        _record_template_import_layout(job, idx, recognized)
        await asyncio.to_thread(sess.save_meta)


async def _template_page_events(
    job: _TemplateImport,
    steps: _TemplateImportSteps,
    page: _TemplatePageImport,
):
    """Correct, lay out, recognize and save one template page.

    A page the teacher declared ungraded is only a routing target: it is
    saved after correction, with no layout, model or artifact work.
    """
    sess = job.sess
    idx = page.index
    yield _sse("progress", {"message": f"识别第 {idx + 1} 页", "current": idx + 1, "total": job.total})
    async for event in _correct_template_page_events(job, steps, page):
        yield event
    page.identity = await _template_page_identity(job, page)
    if page.role not in GRADED_PAGE_ROLES:
        sess.add_page(idx, [], page_role=page.role, page_identity=page.identity)
        await asyncio.to_thread(sess.save_meta)
        yield _sse("page", {"page_index": idx, "question_count": 0, "page_role": page.role})
        return
    async for event in _template_layout_events(job, steps, page):
        yield event
    async for event in _template_model_events(job, steps, page):
        yield event
    await _save_recognized_template_page(job, page)
    yield _sse("page", {"page_index": idx, "question_count": len(page.recognized.questions)})


async def _freeze_template_page_events(
    job: _TemplateImport,
    steps: _TemplateImportSteps,
    page: _TemplatePageImport,
):
    """Build the page's template artifact from its recognized geometry.

    Blank templates store model-marked printed answer areas; marked templates
    store explicit handwriting exemptions. Neither mode reuses another
    batch's coordinates or treats student pixels as template authority.
    """
    sess = job.sess
    recognized = page.recognized
    try:
        steps.start(asyncio.create_task(_run_template_cpu_component(
            build_template_artifact,
            page.canonical.image_path,
            sess.template_dir,
            page.index,
            page.artifact_questions,
            blank_detection_enabled=False,
            canonicalization=page.canonical.canonicalization,
            question_anchors=recognized.question_anchors,
            template_answer_regions=recognized.template_answer_regions,
            template_mode=sess.template_mode,
            # Both legacy modes persist the model's complete
            # question-to-exemption contract. An empty regions
            # list is the explicit no-handwriting result.
            template_handwriting_regions=recognized.template_handwriting_regions,
        )))
        while not await steps.poll():
            yield _sse("progress", _template_heartbeat(page.index, job.total, "生成矫正与裁剪基准"))
    except _TemplateImportDeadlineExceeded:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise _TemplateRecognitionFailure(
            "TEMPLATE_LAYOUT_FAILED",
            "本次模板的答案区域定位失败，请确认图片完整清晰后重新导入",
        ) from exc
    _bind_template_import_artifact(job, page.index, page.artifact_questions, steps.result)
    await asyncio.to_thread(sess.save_meta)


async def _finish_template_import(job: _TemplateImport) -> None:
    """Check whole-paper numbering, then mark the template recognized."""
    sess = job.sess
    if _template_import_clock() >= job.deadline_at:
        raise _TemplateImportDeadlineExceeded
    try:
        validate_global_numbering(sess.pages)
    except ValueError as exc:
        raise _TemplateRecognitionFailure(
            "TEMPLATE_OUTPUT_INVALID",
            "模板题号无法合并为整卷连续编号，请重新导入模板",
        ) from exc
    sess.status = "ocr_done"
    sess.error = None
    sess.error_code = ""
    sess.error_info = {}
    await asyncio.to_thread(sess.save_meta)


async def _template_import_events(job: _TemplateImport):
    """Recognize every uploaded template page, then freeze each page's geometry.

    Every slow step runs as a task tracked by ``steps``: the stream keeps sending
    heartbeats while it works, the whole import stays inside its deadline, and
    whatever is still running is stopped before the session is marked failed.
    Each completed stage is checkpointed off the event loop. The phase helpers
    hold no cleanup of their own, so they are iterated directly.
    """
    sess = job.sess
    template_id = sess.template_id
    steps = _TemplateImportSteps(job.deadline_at)
    try:
        deadline_minutes = max(1, (int(job.deadline_s) + 59) // 60)
        yield _sse("progress", {
            "message": (
                f"正在识别 {job.total} 页模板；本次总时限为 {deadline_minutes} 分钟，"
                "处理中会持续显示进度"
            ),
            "current": 0,
            "total": job.total,
            "template_id": template_id,
            "teaching_context": copy.deepcopy(sess.teaching_context),
        })
        graded_pages: list[_TemplatePageImport] = []
        previous = None
        for idx, path in enumerate(job.paths):
            page = _TemplatePageImport(index=idx, path=path, role=job.page_roles[idx], previous=previous)
            async for event in _template_page_events(job, steps, page):
                yield event
            if page.recognized is not None:
                graded_pages.append(page)
            previous = cross_page.PreviousPage(page.role, page.canonical.image_path, page.model_output)
        # Every import owns and freezes its geometry, after all pages are saved.
        for page in graded_pages:
            async for event in _freeze_template_page_events(job, steps, page):
                yield event
        await _finish_template_import(job)
        yield _sse("done", {
            "template_id": template_id,
            "teaching_context": copy.deepcopy(sess.teaching_context),
            "page_count": len(sess.pages),
            "template_mode": sess.template_mode,
            "column_mode": getattr(sess, "column_mode", "double") or "double",
            "pages": sess.pages,
        })
    except _TemplateImportDeadlineExceeded:
        failure = _template_import_deadline_failure(job.deadline_s)
        yield await _fail_template_import(
            sess, failure, code=failure.code, message=failure.message, kind="超时",
        )
    except asyncio.CancelledError:
        logger.info("模板导入连接中断 template=%s", template_id)
        raise
    except _TemplateRecognitionFailure as exc:
        yield await _fail_template_import(
            sess, exc, code=exc.code, message=exc.message, kind="失败",
        )
    except Exception as exc:
        yield await _fail_template_import(sess, exc, code="TEMPLATE_IMPORT_FAILED", kind="故障")
    finally:
        await steps.settle()
        shutil.rmtree(job.candidate_dir, ignore_errors=True)
        if sess.status not in {"ocr_done", "failed"}:
            sess.status = "failed"
            sess.error_code = "TEMPLATE_IMPORT_INTERRUPTED"
            sess.error = (
                "模板导入连接已中断，所有在途步骤均已清理；"
                "本次模板不可用，请重新发起导入"
            )
            try:
                await asyncio.to_thread(sess.save_meta)
            except Exception:
                logger.exception("模板中断状态保存失败 template=%s", template_id)
