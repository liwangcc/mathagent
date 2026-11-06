"""Student recognition OCR phase: queued uploads recognized while grading runs.

``routers.grade`` sequences both recognition streams (the first run and a
resume): it takes the ``_RecognitionLease``, builds the ``_RecognitionRun`` and
drains the grading pipeline.  The OCR phase in between lives here: dispatch the
durable OCR job queue, interleave per-paper grading results, and stream each
outcome.  Moved unchanged from ``routers/grade.py``, which re-exports these names.
"""
from __future__ import annotations

import asyncio
from contextlib import aclosing

from services.ocr_service import (
    ocr_failure_result,
    reset_vision_route_ordinal,
    set_vision_route_ordinal,
)
from services.bounded_dispatch import teacher_waved_completion_map
from services.paper_cache import PaperCache
from services.paper_orientation import orientation_is_ready
from services.quick_grader import (
    QuickBatch,
    is_accelerator_pause_result,
    sync_pending_recognition_uploads,
)
from config import (
    OCR_BATCH_DISPATCH_CONCURRENT,
    OCR_TEACHER_WAVE_SIZE,
)
from routers.grade_common import (
    _GRADE_HEARTBEAT_SECONDS,
    _settle_template_component,
    _sse,
)
from routers.grade_papers import (
    _recognize_batch_upload,
)


async def _recognize_upload(
    batch: QuickBatch,
    upload: dict,
    paper_cache,
    *,
    teacher_id: int,
    roster_names: list[str],
) -> dict:
    """OCR one upload, refusing a page whose direction is not cleared."""
    orientation = await _ensure_batch_upload_inspected(batch, upload)
    if not orientation_is_ready(orientation):
        return {
            "questions": [],
            "student_name": "",
            "student_no": "",
            "error": "试卷方向异常或未确认，已拒绝进入 OCR",
            "orientation": orientation,
        }
    result = await _recognize_batch_upload(
        batch,
        str(upload.get("image_path") or ""),
        paper_cache,
        teacher_id=str(teacher_id),
        paper_id=str(upload.get("paper_id") or ""),
        roster_names=roster_names,
    )
    result["orientation"] = orientation
    return result


def _ocr_outcome_message(filename: str, *, accelerator_wait: bool, failed: bool) -> str:
    if accelerator_wait:
        return f"{filename} 正在等待加速节点"
    if failed:
        return f"识别失败 {filename}，继续处理其余试卷"
    return f"已识别 {filename}"


async def _ocr_and_grading_events(result_stream, pipeline: "_PerPaperGradingPipeline"):
    """Interleave OCR completions with per-paper grading results.

    Yields ``("ocr", (position, paper_data))`` for each finished upload,
    ``("graded", item)`` for each pipeline result, and ``("heartbeat", None)``
    when nothing finished within one heartbeat interval. On exit the pending
    waits are settled and the OCR stream is closed; consume this under
    ``contextlib.aclosing`` so that cleanup runs before the caller's own.
    """
    next_result = asyncio.create_task(anext(result_stream))
    next_graded = asyncio.create_task(pipeline.queue.get())
    try:
        while True:
            done, _ = await asyncio.wait(
                {next_result, next_graded}, timeout=_GRADE_HEARTBEAT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                yield "heartbeat", None
                continue
            if next_graded in done:
                graded = next_graded.result()
                next_graded = asyncio.create_task(pipeline.queue.get())
                yield "graded", graded
                continue
            try:
                position, paper_data = next_result.result()
            except StopAsyncIteration:
                break
            next_result = asyncio.create_task(anext(result_stream))
            yield "ocr", (position, paper_data)
    finally:
        await _settle_template_component(next_graded, cancel_running=True)
        await _settle_template_component(next_result, cancel_running=True)
        await result_stream.aclose()


def _ocr_job_worker(run: _RecognitionRun, paper_cache: PaperCache, roster_names: list[str]):
    """The dispatcher's worker: reserve one queued OCR job, recognize it on its route."""
    batch = run.batch

    async def recognize(indexed_upload: tuple[int, dict]) -> dict:
        route_ordinal, upload = indexed_upload
        batch.mark_ocr_job(str(upload.get("image_path") or ""), "running")
        if run.flavor.syncs_pending_uploads:
            sync_pending_recognition_uploads(batch)
        route_token = set_vision_route_ordinal(route_ordinal)
        try:
            return await _recognize_upload(
                batch, upload, paper_cache,
                teacher_id=run.teacher_id, roster_names=roster_names,
            )
        except Exception as exc:
            logger.error(run.flavor.ocr_failure_log, upload.get("filename"), exc)
            return ocr_failure_result(exc, message="识别失败")
        finally:
            reset_vision_route_ordinal(route_token)

    return recognize


async def _ocr_phase_events(
    run: _RecognitionRun,
    uploads: list[dict],
    paper_cache: PaperCache,
    failure_questions: list[dict],
    roster_names: list[str],
):
    """SSE while ``uploads`` are recognized, interleaved with per-paper grading.

    并发段：dispatcher 只保留固定数量的实际 OCR 生命周期任务；其余上传文件
    保持在持久 ocr_jobs 队列中等待，不提前 Base64/占内存。在途窗口取批调度
    并发（OCR_BATCH_DISPATCH_CONCURRENT）；图像内存由载荷字节预算与图像闩
    兜底。Consume under ``contextlib.aclosing`` so the OCR stream is settled
    before the caller's own cleanup.
    """
    pipeline = run.pipeline
    result_stream = teacher_waved_completion_map(
        list(enumerate(uploads)),
        _ocr_job_worker(run, paper_cache, roster_names),
        teacher_id=str(run.teacher_id),
        batch_id=f"{run.batch.batch_id}{run.flavor.dispatch_suffix}",
        wave_size=OCR_TEACHER_WAVE_SIZE,
        max_concurrent=OCR_BATCH_DISPATCH_CONCURRENT,
    )
    async with aclosing(_ocr_and_grading_events(result_stream, pipeline)) as events:
        async for kind, value in events:
            if kind == "heartbeat":
                yield _sse("progress", run.progress_payload(
                    run.flavor.ocr_heartbeat_message, heartbeat=True,
                ))
                continue
            if kind == "graded":
                yield _pipeline_result_sse(pipeline, value)
                continue
            position, paper_data = value
            upload = uploads[position]
            error_payload = await run.record_ocr_outcome(upload, paper_data, failure_questions)
            # A failed provider call is still a terminal outcome for this
            # sheet. Stream it immediately so a batch whose first OCR attempts
            # time out cannot look frozen at 0/N.
            accelerator_wait = is_accelerator_pause_result(paper_data)
            yield _sse("progress", run.progress_payload(_ocr_outcome_message(
                str(upload.get("filename") or "unknown"),
                accelerator_wait=accelerator_wait,
                failed=bool(error_payload),
            )))
            if run.flavor.streams_failed_page(error_payload, accelerator_wait):
                yield _sse("paper", error_payload)


# routers.grade imports this module only after defining everything below, and
# re-exports the names above; importing it last works in either import order.
# ``logger`` is routers.grade's, so these records keep their logger name.
from routers.grade import (  # noqa: E402
    _RecognitionRun,
    _ensure_batch_upload_inspected,
    logger,
)
# The per-paper grading pipeline lives in routers/grade_pipeline.py.
from routers.grade_pipeline import (  # noqa: E402
    _PerPaperGradingPipeline,
    _pipeline_result_sse,
)
