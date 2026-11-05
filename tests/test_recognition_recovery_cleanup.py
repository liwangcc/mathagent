"""Synthetic recovery/cleanup contracts; no model or production data access."""

import asyncio
import json
import os
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import UploadFile

from models_user import User
from services.quick_grader import (
    BatchOperationBusy,
    QuickBatch,
    batch_operation_lock,
)
from tests.test_batch_calibration_flow import _confirmed_template, _events, _setup


def _pending_batch(tmp_path, *, count=2):
    batch = QuickBatch("recovery-cleanup", str(tmp_path))
    batch.teacher_id = 1
    batch.identity_mode = "anonymous"
    batch.category = "daily"
    batch.grade_mode = "auto"
    batch.source_mode = "template"
    batch.status = "recognition_interrupted"
    batch.template_id = "recovery-template"
    _confirmed_template(tmp_path, batch.template_id)
    root = Path(batch.batch_dir)
    root.mkdir(parents=True)
    for index in range(count):
        path = root / f"paper-{index}.png"
        path.write_bytes(b"synthetic image boundary")
        batch.uploads.append({
            "paper_id": f"paper-{index}", "filename": path.name,
            "image_path": str(path), "orientation_status": "auto_passed",
            "orientation_reason": "synthetic boundary",
            "orientation_confidence": 1.0, "detected_tilt_degrees": 0.0,
        })
    batch.ensure_ocr_jobs(batch.uploads)
    batch.pending_uploads = list(batch.uploads)
    batch.save_meta()
    return batch


def _recognized():
    return {"student_name": "", "student_no": "", "questions": [{
        "question_number": "1", "question_text": "1+1=?",
        "question_type": "fill", "student_answer": "2", "answer_state": "visible",
    }]}


@pytest.mark.parametrize("attempts", [1, 2])
def test_orientation_read_preserves_running_jobs_while_operation_is_locked(
    monkeypatch, tmp_path, attempts,
):
    client, _ = _setup(monkeypatch, tmp_path)
    batch = _pending_batch(tmp_path, count=1)
    batch.status = "recognizing_remaining"
    batch.ocr_jobs[0].update(status="running", attempts=attempts)
    batch.save_meta()
    before = Path(batch.meta_path).read_bytes()
    with batch_operation_lock(batch.batch_id, str(tmp_path)):
        response = client.get(f"/api/batch-grade/{batch.batch_id}/orientation")
        loaded = QuickBatch.load_meta(batch.batch_id, str(tmp_path))
    assert response.status_code == 200
    state = response.json()
    assert state["papers"][0]["ocr_status"] == "running"
    assert state["failed_papers"] == 0
    assert state["unresolved_papers"] == 1
    assert state["can_grade"] is False
    assert loaded.ocr_jobs[0]["status"] == "running"
    assert loaded.ocr_jobs[0]["attempts"] == attempts
    assert Path(batch.meta_path).read_bytes() == before


@pytest.mark.parametrize("attempts", [1, 2])
def test_resume_normalizes_stale_running_only_after_exclusive_lock(
    monkeypatch, tmp_path, attempts,
):
    client, _ = _setup(monkeypatch, tmp_path)
    from routers import grade_recognition

    batch = _pending_batch(tmp_path, count=1)
    batch.ocr_jobs[0].update(status="running", attempts=attempts)
    batch.save_meta()
    calls = []

    async def recognized(_batch, path, _cache, **_kwargs):
        calls.append(path)
        return _recognized()

    monkeypatch.setattr(grade_recognition, "_recognize_batch_upload", recognized)
    before = Path(batch.meta_path).read_bytes()
    with batch_operation_lock(batch.batch_id, str(tmp_path)):
        response = client.post(f"/api/batch-grade/{batch.batch_id}/recognize/resume")
        assert _events(response)[-1][1]["code"] == "BATCH_RECOGNITION_BUSY"
        assert Path(batch.meta_path).read_bytes() == before
        assert calls == []
    response = client.post(f"/api/batch-grade/{batch.batch_id}/recognize/resume")
    events = _events(response)
    restored = QuickBatch.load_meta(batch.batch_id, str(tmp_path))
    if attempts == 1:
        assert [event for event, _ in events][-1] == "done"
        assert len(calls) == 1
        assert restored.ocr_jobs[0]["status"] == "succeeded"
        assert restored.ocr_jobs[0]["attempts"] == 2
    else:
        assert events[-1][1]["code"] == "OCR_RETRY_EXHAUSTED"
        assert calls == []
        assert restored.ocr_jobs[0]["status"] == "failed"
        assert restored.ocr_jobs[0]["attempts"] == 2
        assert restored.ocr_jobs[0]["retryable"] is False


def test_dispatcher_error_refunds_cancelled_ocr_attempts_and_allows_resume(
    monkeypatch, tmp_path,
):
    client, _ = _setup(monkeypatch, tmp_path)
    import routers.grade as grade_router
    from routers import grade_recognition

    batch = _pending_batch(tmp_path)
    batch.ocr_jobs[0]["attempts"] = 1
    batch.pending_uploads = list(batch.uploads)
    batch.save_meta()
    phase = {"fail": True}
    dispatch = grade_router._PerPaperGradingPipeline.dispatch

    def failing_dispatch(self, paper):
        if phase["fail"]:
            raise RuntimeError("synthetic downstream dispatch failure")
        return dispatch(self, paper)

    async def recognized(_batch, path, _cache, **_kwargs):
        if not phase["fail"] or os.path.basename(path) == "paper-1.png":
            return _recognized()
        await asyncio.Event().wait()

    monkeypatch.setattr(grade_router._PerPaperGradingPipeline, "dispatch", failing_dispatch)
    monkeypatch.setattr(grade_recognition, "_recognize_batch_upload", recognized)
    monkeypatch.setattr(grade_recognition, "OCR_BATCH_DISPATCH_CONCURRENT", 2)

    def request():
        return client.post(f"/api/batch-grade/{batch.batch_id}/recognize/resume")

    response = request()
    assert response.status_code == 200, response.text
    assert _events(response)[-1][0] == "error"
    raw = json.loads(Path(batch.meta_path).read_text(encoding="utf-8"))
    assert raw["ocr_jobs"][0]["status"] == "queued"
    assert raw["ocr_jobs"][0]["attempts"] == 1
    assert {item["image_path"] for item in raw["pending_uploads"]} == {
        job["image_path"] for job in raw["ocr_jobs"]
        if job["status"] in {"queued", "running"}
    }
    phase["fail"] = False
    events = _events(request())
    assert events[-1][0] == "done"
    assert events[-1][1]["can_grade"] is True
    final = QuickBatch.load_meta(batch.batch_id, str(tmp_path))
    assert [job["status"] for job in final.ocr_jobs] == ["succeeded", "succeeded"]
    assert [job["attempts"] for job in final.ocr_jobs] == [2, 1]


@pytest.mark.parametrize("initial", [False, True])
@pytest.mark.parametrize("transport", ["iterator", "asgi"])
def test_repeated_disconnect_cancellation_settles_pipeline_before_releasing_lock(
    monkeypatch, tmp_path, initial, transport,
):
    _client, Session = _setup(monkeypatch, tmp_path, education_stage="senior")
    import routers.grade as grade_router
    from routers import grade_recognition

    batch = _pending_batch(tmp_path, count=3)
    monkeypatch.setattr(grade_recognition, "OCR_BATCH_DISPATCH_CONCURRENT", 3)
    captured_locks = []
    locked_batch_ids = []
    effects = {"exited": False, "db_closed": False, "resets": set()}
    real_lock = batch_operation_lock

    @contextmanager
    def tracked_lock(*args):
        lock = real_lock(*args)
        captured_locks.append(lock)
        locked_batch_ids.append(args[0])
        with lock:
            effects["exited"] = False
            try:
                yield
            finally:
                effects["exited"] = True

    def tracked_session():
        db = Session()
        close = db.close

        def tracked_close():
            effects["db_closed"] = True
            close()

        db.close = tracked_close
        return db

    monkeypatch.setattr(grade_router, "batch_operation_lock", tracked_lock)
    monkeypatch.setattr(grade_router, "SessionLocal", tracked_session)
    for name in (
        "reset_grading_teacher_id", "reset_vision_phase_key", "reset_vision_batch_paper_count",
    ):
        original = getattr(grade_router, name)

        def tracked_reset(token, original=original, name=name):
            effects["resets"].add(name)
            return original(token)

        monkeypatch.setattr(grade_router, name, tracked_reset)

    async def run():
        grade_started = asyncio.Event()
        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()
        cleanup_finished = asyncio.Event()
        ocr_cleanup_started = asyncio.Event()
        release_ocr_cleanup = asyncio.Event()
        ocr_stopped = asyncio.Event()
        result_sent = asyncio.Event()
        calls = []
        cancellations = []
        checkpoint_observations = []
        real_grade = grade_router._grade_paper_core
        real_save = grade_router._save_quick_batch_meta

        async def observed_save(active_batch):
            if active_batch.status == "recognition_interrupted":
                checkpoint_observations.append((
                    ocr_stopped.is_set(), cleanup_finished.is_set(),
                    effects["exited"], effects["db_closed"],
                ))
            await real_save(active_batch)

        async def recognized(_batch, path, _cache, **_kwargs):
            calls.append(path)
            if len(calls) <= 2:
                return _recognized()
            try:
                await asyncio.Event().wait()
            finally:
                ocr_cleanup_started.set()
                await release_ocr_cleanup.wait()
                cancellations.append(asyncio.current_task().cancelling())
                ocr_stopped.set()

        async def blocked_grade(*args, **kwargs):
            if args[1]["image_path"] == calls[0]:
                return await real_grade(*args, **kwargs)
            grade_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await release_cleanup.wait()
                cancellations.append(asyncio.current_task().cancelling())
                cleanup_finished.set()

        monkeypatch.setattr(grade_recognition, "_recognize_batch_upload", recognized)
        monkeypatch.setattr(grade_router, "_grade_paper_core", blocked_grade)
        monkeypatch.setattr(grade_router, "_save_quick_batch_meta", observed_save)
        if initial:
            with Session() as db:
                response = await grade_router.batch_grade_recognize(
                    files=[
                        UploadFile(filename=f"paper-{index}.ocr-ready-v1.jpg", file=BytesIO(b"synthetic"))
                        for index in range(3)
                    ],
                    category="daily", exam_name="", template_id=batch.template_id,
                    mode="auto", anonymous=True, source_paths="",
                    user=User(id=1, role="teacher", education_stage="senior", current_class_id=1), db=db,
                )
        else:
            response = await grade_router.batch_grade_resume_recognition(
                batch.batch_id, user=User(id=1, role="teacher", education_stage="senior", current_class_id=1),
            )

        async def consume():
            try:
                async for event in response.body_iterator:
                    if event.startswith("event: result\n"):
                        result_sent.set()
            finally:
                await response.body_iterator.aclose()

        disconnect = asyncio.Event()

        async def receive():
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            # Exercise the real StreamingResponse task group, not just anext().
            assert message["type"] in {"http.response.start", "http.response.body"}
            if message.get("body", b"").startswith(b"event: result\n"):
                result_sent.set()

        consumer = asyncio.create_task(
            response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send)
            if transport == "asgi" else consume()
        )
        await asyncio.wait_for(grade_started.wait(), 2)
        await asyncio.wait_for(result_sent.wait(), 2)
        active_batch_id = locked_batch_ids[-1]
        meta_path = tmp_path / f"quick_batch_{active_batch_id}" / "meta.json"
        before = json.loads(meta_path.read_text(encoding="utf-8"))
        assert len(before["results"]) == len(before["review_results"]) == 1
        checkpoint_observations.clear()  # Ignore loading the old interrupted checkpoint.
        meta_gate = grade_router._get_quick_batch_meta_io_semaphore()
        if transport == "asgi":
            # Another batch can occupy the worker-wide disk gate at disconnect.
            # This forces the interrupted snapshot to cross a real suspension.
            await meta_gate.acquire()
            disconnect.set()
        else:
            consumer.cancel()
        try:
            await asyncio.wait_for(ocr_cleanup_started.wait(), 2)
            with pytest.raises(BatchOperationBusy):
                with real_lock(active_batch_id, str(tmp_path)):
                    pass
            assert not effects["db_closed"] and not checkpoint_observations
            release_ocr_cleanup.set()
            await asyncio.wait_for(cleanup_started.wait(), 2)
            with pytest.raises(BatchOperationBusy):
                with real_lock(active_batch_id, str(tmp_path)):
                    pass
            if transport == "iterator":
                consumer.cancel()
            await asyncio.sleep(0)
        finally:
            # A failing assertion must not strand the test's artificial gates.
            release_ocr_cleanup.set()
            release_cleanup.set()
            if transport == "asgi":
                meta_gate.release()
            result = (await asyncio.wait_for(
                asyncio.gather(consumer, return_exceptions=True), 2,
            ))[0]
        if transport == "iterator":
            assert isinstance(result, asyncio.CancelledError)
        else:
            assert result is None, result
        assert cleanup_finished.is_set()
        assert ocr_stopped.is_set()
        assert cancellations == [1, 1], "Each worker is cancelled once, not during cleanup"
        assert checkpoint_observations == [(True, True, False, False)]
        after = json.loads(meta_path.read_text(encoding="utf-8"))
        assert after["status"] == "recognition_interrupted"
        assert after["results"] == before["results"]
        assert after["review_results"] == before["review_results"]
        assert [job["status"] for job in after["ocr_jobs"]] == ["succeeded", "succeeded", "queued"]
        assert [job["attempts"] for job in after["ocr_jobs"]] == [1, 1, 0]
        assert [item["image_path"] for item in after["pending_uploads"]] == [calls[-1]]
        monkeypatch.setattr(grade_router, "_grade_paper_core", real_grade)
        monkeypatch.setattr(grade_router, "_save_quick_batch_meta", real_save)
        return active_batch_id, calls[-1], before["results"][0]

    active_batch_id, pending_path, saved_result = asyncio.run(run())
    assert effects["exited"] is True
    assert effects["db_closed"] is True
    assert len(effects["resets"]) == 3
    with real_lock(active_batch_id, str(tmp_path)):
        restored = QuickBatch.load_meta(active_batch_id, str(tmp_path))
    assert restored.status == "recognition_interrupted"

    resumed_ocr = []

    async def resume_recognized(_batch, path, _cache, **_kwargs):
        resumed_ocr.append(path)
        return _recognized()

    monkeypatch.setattr(grade_recognition, "_recognize_batch_upload", resume_recognized)
    events = _events(_client.post(f"/api/batch-grade/{active_batch_id}/recognize/resume"))
    assert events[-1][0] == "done", events
    assert resumed_ocr == [pending_path], "Durable OCR must not be called again"
    assert len([data for event, data in events if event == "result"]) == 2
    final = QuickBatch.load_meta(active_batch_id, str(tmp_path))
    assert [job["attempts"] for job in final.ocr_jobs] == [1, 1, 1]
    assert len(final.results) == 3 and final.results[0] == saved_result


def test_recognition_cleanup_does_not_hide_checkpoint_error(monkeypatch, tmp_path):
    from anyio import CancelScope
    import routers.grade as grade_router

    batch = _pending_batch(tmp_path, count=1)
    order = []

    class Pipeline:
        async def aclose(self):
            await asyncio.sleep(0)
            order.append("settled")

    async def failing_save(_batch):
        await asyncio.sleep(0)
        order.append("save")
        raise OSError("synthetic checkpoint failure")

    monkeypatch.setattr(grade_router, "_save_quick_batch_meta", failing_save)

    async def run():
        with CancelScope() as scope:
            scope.cancel()
            with pytest.raises(OSError, match="synthetic checkpoint failure"):
                await grade_router._checkpoint_recognition_cleanup(batch, Pipeline())

    asyncio.run(run())
    assert order == ["settled", "save"]
