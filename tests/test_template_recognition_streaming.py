"""Template import and answer-sheet SSE streams: deadlines, heartbeats, checkpoints.

The first tests here used to read ``routers/grade_template.py`` and
``routers/answer_sheet.py`` and count strings in them (``steps.start(`` at
least five times, ``await asyncio.to_thread(sess.save_meta)`` at least six,
no ``locked.save()``).  They now drive the real endpoints and assert what a
client and the next request observe:

* every slow template-import step streams its own heartbeat while it runs,
  and a heartbeat can only come from the deadline-enforcing poll loop;
* the import deadline scales with the uploaded pages; when it passes during a
  step the step is cancelled first, then the template is persisted as failed;
* a reader that closes the stream between two events gets the running step
  cancelled and awaited before its inputs are removed and the template is
  persisted as interrupted;
* recognized pages, their page roles and frozen artifacts are persisted;
* every checkpoint of these streams is written off the event loop thread.

A held component is released only after the client has received the
heartbeat of its step, and the deadline is passed by moving the event loop's
clock forward — no sleeps and no wall-clock races.
"""
import asyncio
import contextlib
import io
import json
import sys
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from services.answer_sheet_session import AnswerSheetSession
from services.template_session import TemplateSession
from tests.test_answer_sheet_session import (  # noqa: F401 - client_env is a fixture
    _canonical_submission,
    _different_png_bytes,
    _png_bytes,
    _question,
    _sse_events,
    _student_upload_session,
    client_env,
)
from tests.test_template_session import (  # noqa: F401 - autouse readiness stubs
    _patch_template_layout,
    _ready_batch_grade_chain,
    _template_png_bytes,
)


def _on_event_loop() -> bool:
    """True when called from the thread that is running an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


_LoopBase = asyncio.ProactorEventLoop if sys.platform == "win32" else asyncio.SelectorEventLoop


class _MovableClockLoop(_LoopBase):
    """An event loop whose clock a test moves forward to pass a deadline."""

    def __init__(self):
        self.offset = 0.0
        super().__init__()

    def time(self):
        return super().time() + self.offset


_WATCHDOG_S = 20.0


def _run(scenario, *, loop_factory=None, releases=()):
    """Run ``scenario`` on a fresh loop.

    The watchdog only turns a hang into a failure (real time, so a moved loop
    clock cannot trip it); nothing here waits for it.  Held components are
    released on the way out so no worker thread outlives the test.
    """
    async def guarded():
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        watchdog = threading.Timer(_WATCHDOG_S, lambda: loop.call_soon_threadsafe(task.cancel))
        watchdog.daemon = True
        watchdog.start()
        try:
            return await scenario()
        finally:
            watchdog.cancel()
            for release in releases:
                release()

    with asyncio.Runner(loop_factory=loop_factory) as runner:
        return runner.run(guarded())


def _model_output() -> dict:
    """One page's model result, numbered from 1 as the model does per page."""
    return {
        "questions": [{
            "question_number": "1",
            "question_text": "1+1=?",
            "question_type": "fill",
            "standard_answer": "2",
            "answer_bbox": [500, 400, 850, 500],
        }],
        "question_anchors": [{
            "question_number": "1",
            "question_number_bbox": [100, 100, 140, 125],
        }],
        "template_handwriting_regions": [{
            "question_number": "1",
            "regions": [[500, 400, 850, 500]],
        }],
    }


def _sse_chunk_events(chunk) -> list[list]:
    text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
    events = []
    for block in text.split("\n\n"):
        lines = block.splitlines()
        name = next((line[7:] for line in lines if line.startswith("event: ")), None)
        data = next((line[6:] for line in lines if line.startswith("data: ")), None)
        if name and data:
            events.append([name, json.loads(data)])
    return events


class _TemplateImport:
    """Drive ``batch_grade_template_recognize`` directly on the test's own loop.

    Records every ``TemplateSession.save_meta`` call (thread, status, pages,
    artifacts) and an ordered ``trace`` shared with the held components.
    """

    def __init__(self, monkeypatch, tmp_path):
        import routers.grade as grade
        from routers import grade_pipeline
        from routers import grade_template

        self.grade_template = grade_template
        self.tmp_path = tmp_path
        monkeypatch.setattr(grade_pipeline, "UPLOAD_DIR", str(tmp_path))
        monkeypatch.setattr(grade, "UPLOAD_DIR", str(tmp_path))
        monkeypatch.setattr(grade, "_check_limits_or_raise", lambda db, uid: None)
        # Heartbeat cadence only; nothing below waits for an interval to pass.
        monkeypatch.setattr(grade_template, "_TEMPLATE_COMPONENT_HEARTBEAT_S", 0.01)
        _patch_template_layout(monkeypatch, grade)
        self.trace: list = []
        self.saves: list[dict] = []
        self.releases: list = []
        save_meta = TemplateSession.save_meta

        def recording_save_meta(sess, *args, **kwargs):
            self.saves.append({
                "on_event_loop": _on_event_loop(),
                "status": sess.status,
                "error_code": getattr(sess, "error_code", ""),
                "pages": len(sess.pages),
                "artifacts": len(sess.page_artifacts),
            })
            self.trace.append(("save", sess.status))
            return save_meta(sess, *args, **kwargs)

        monkeypatch.setattr(TemplateSession, "save_meta", recording_save_meta)

        async def model(_path, **_kwargs):
            return _model_output()

        monkeypatch.setattr(grade_template, "recognize_template_pipeline", model)

    async def start(self, pages: int = 1, *, page_roles=None, canonical: bool = False):
        raws = [_template_png_bytes(chr(ord("A") + index)) for index in range(pages)]
        files = [
            UploadFile(file=io.BytesIO(raw), filename=f"page-{index + 1}.png",
                       headers=Headers({"content-type": "image/png"}))
            for index, raw in enumerate(raws)
        ]
        canonical_files = canonical_metadata = None
        if canonical:
            submissions = [
                _canonical_submission(self.tmp_path, raw, f"page-{index + 1}")
                for index, raw in enumerate(raws)
            ]
            canonical_files = [
                UploadFile(file=io.BytesIO(png), filename=f"canonical-{index + 1}.png",
                           headers=Headers({"content-type": "image/png"}))
                for index, (png, _metadata) in enumerate(submissions)
            ]
            canonical_metadata = json.dumps([metadata for _png, metadata in submissions])
        return await self.grade_template.batch_grade_template_recognize(
            files=files,
            canonical_files=canonical_files,
            canonical_metadata=canonical_metadata,
            has_answers=False,
            template_mode="",
            column_mode="double",
            layout_mode="",
            category="daily",
            exam_name="",
            page_roles=json.dumps(page_roles) if page_roles else "",
            page_labels="",
            user=SimpleNamespace(id=1, education_stage="senior"),
            db=object(),
        )

    @staticmethod
    async def consume(response, on_event=None) -> list[list]:
        events = []
        async for chunk in response.body_iterator:
            for event in _sse_chunk_events(chunk):
                events.append(event)
                if on_event is not None:
                    on_event(*event)
        return events

    def session(self) -> TemplateSession:
        directory = next(self.tmp_path.glob("template_*"))
        return TemplateSession.load_meta(directory.name.removeprefix("template_"), str(self.tmp_path))


# Slow steps of one page, the heartbeat that names each, and the component.
_STEP_HEARTBEATS = {
    "canonical_candidate": "第 1 页正在校验标准模板候选",
    "local_correction": "第 1 页正在本地完成页面矫正",
    "layout": "第 1 页正在识别题目布局",
    "model": None,  # model heartbeats carry the model stages instead
    "freeze": "第 1 页正在生成矫正与裁剪基准",
}
_STEP_COMPONENTS = {
    "canonical_candidate": ("accept_canonical_template_candidate", "thread"),
    "local_correction": ("canonicalize_template_image", "thread"),
    "layout": ("recognize_column_layout", "async"),
    "model": ("recognize_template_pipeline", "async"),
    "freeze": ("build_template_artifact", "thread"),
}


def _is_step_heartbeat(step: str, event: str, payload: dict) -> bool:
    if event != "progress" or payload.get("heartbeat") is not True:
        return False
    if step == "model":
        return "model_stages" in payload
    return payload.get("message") == _STEP_HEARTBEATS[step]


def _hold(monkeypatch, run: _TemplateImport, step: str):
    """Hold ``step``'s component until released; returns the release callback."""
    name, kind = _STEP_COMPONENTS[step]
    original = getattr(run.grade_template, name)
    if kind == "thread":
        gate = threading.Event()

        def held(*args, **kwargs):
            gate.wait()
            run.trace.append(("returned", step))
            return original(*args, **kwargs)

        monkeypatch.setattr(run.grade_template, name, held)
        run.releases.append(gate.set)
        return gate.set

    gates: list[asyncio.Event] = []

    def gate_for_loop() -> asyncio.Event:
        if not gates:
            gates.append(asyncio.Event())
        return gates[0]

    async def held_async(*args, **kwargs):
        try:
            await gate_for_loop().wait()
        except asyncio.CancelledError:
            run.trace.append(("cancelled", step))
            raise
        run.trace.append(("returned", step))
        return await original(*args, **kwargs)

    monkeypatch.setattr(run.grade_template, name, held_async)
    run.releases.append(lambda: gate_for_loop().set())
    return lambda: gate_for_loop().set()


@pytest.mark.parametrize("step", list(_STEP_HEARTBEATS))
def test_every_slow_import_step_heartbeats_while_it_runs(monkeypatch, tmp_path, step):
    run = _TemplateImport(monkeypatch, tmp_path)
    release = _hold(monkeypatch, run, step)

    def on_event(event, payload):
        if _is_step_heartbeat(step, event, payload) and ("heartbeat", step) not in run.trace:
            run.trace.append(("heartbeat", step))
            release()
        elif event in {"done", "error"}:
            release()  # never leave a held worker behind on a failed assertion

    async def scenario():
        response = await run.start(canonical=step == "canonical_candidate")
        return await run.consume(response, on_event)

    events = _run(scenario, releases=run.releases)
    assert events[-1][0] == "done", events
    heartbeat = next(payload for event, payload in events if _is_step_heartbeat(step, event, payload))
    assert {key: heartbeat[key] for key in ("current", "total", "heartbeat")} == {
        "current": 1, "total": 1, "heartbeat": True,
    }
    assert run.trace.index(("heartbeat", step)) < run.trace.index(("returned", step))


@pytest.mark.parametrize("pages", [1, 2])
def test_import_deadline_scales_with_the_uploaded_pages(monkeypatch, tmp_path, pages):
    run = _TemplateImport(monkeypatch, tmp_path)
    deadline_s = run.grade_template._template_import_deadline_seconds(pages)

    async def scenario():
        return await run.consume(await run.start(pages))

    events = _run(scenario, releases=run.releases)
    assert events[-1][0] == "done", events
    first = events[0][1]
    assert f"本次总时限为 {int(deadline_s) // 60} 分钟" in first["message"]
    session = run.session()
    span = (
        datetime.fromisoformat(session.recognition_deadline_at)
        - datetime.fromisoformat(session.recognition_started_at)
    ).total_seconds()
    assert span == deadline_s


def test_deadline_during_a_step_cancels_it_before_the_template_fails(monkeypatch, tmp_path):
    run = _TemplateImport(monkeypatch, tmp_path)
    _hold(monkeypatch, run, "model")  # never released: only the deadline ends it

    def on_event(event, payload):
        if _is_step_heartbeat("model", event, payload) and ("deadline", "model") not in run.trace:
            run.trace.append(("deadline", "model"))
            asyncio.get_running_loop().offset += 10 * run.grade_template._template_import_deadline_seconds(1)

    async def scenario():
        return await run.consume(await run.start(), on_event)

    events = _run(scenario, loop_factory=_MovableClockLoop, releases=run.releases)
    assert [event for event, _ in events].count("done") == 0
    error = events[-1]
    assert error[0] == "error" and error[1]["code"] == "TEMPLATE_IMPORT_TIMEOUT", events
    assert "9 分钟总时限" in error[1]["message"]
    assert run.trace.index(("cancelled", "model")) < run.trace.index(("save", "failed"))
    session = run.session()
    assert (session.status, session.error_code) == ("failed", "TEMPLATE_IMPORT_TIMEOUT")
    assert session.pages == [] and session.page_artifacts == {}


def test_stopped_import_cancels_the_running_step_and_marks_the_template_interrupted(
    monkeypatch, tmp_path,
):
    run = _TemplateImport(monkeypatch, tmp_path)
    _hold(monkeypatch, run, "model")

    async def scenario():
        response = await run.start()
        recognition = next(
            task for task in asyncio.all_tasks()
            if task.get_name().startswith("template-recognition-")
        )
        iterator = response.body_iterator
        async for chunk in iterator:
            if any(_is_step_heartbeat("model", *event) for event in _sse_chunk_events(chunk)):
                break
        recognition.cancel()  # e.g. the server stopping the background import
        with contextlib.suppress(asyncio.CancelledError):
            await recognition
        await iterator.aclose()

    _run(scenario, releases=run.releases)
    assert run.trace.index(("cancelled", "model")) < run.trace.index(("save", "failed"))
    session = run.session()
    assert (session.status, session.error_code) == ("failed", "TEMPLATE_IMPORT_INTERRUPTED")
    assert run.saves[-1]["on_event_loop"] is False


def _read_import_stream_directly(monkeypatch, run: _TemplateImport) -> dict:
    """Make the test the import stream's only reader instead of the relay task.

    The relay never pauses between two events, so a stop always lands inside a
    step's poll, which settles the step itself.  A reader that closes the stream
    between two events (a disconnecting client) leaves it parked at a ``yield``
    with the step still running; only the stream's own cleanup can stop it.
    """
    captured: dict = {}
    import_events = run.grade_template._template_import_events

    def recording_import_events(job):
        captured["job"] = job
        return import_events(job)

    async def no_relay():
        return None

    def capture_stream(source, _events, _sess):
        captured["stream"] = source
        return no_relay()

    monkeypatch.setattr(run.grade_template, "_template_import_events", recording_import_events)
    monkeypatch.setattr(run.grade_template, "_relay_template_recognition", capture_stream)
    return captured


def _hold_until_cancelled(monkeypatch, run: _TemplateImport, step: str, captured: dict) -> dict:
    """Hold async ``step``'s component until it is cancelled; record its task.

    Its cleanup is asynchronous, so it has only run if the stream awaited the
    cancelled component rather than just requesting the cancellation.
    """
    name, kind = _STEP_COMPONENTS[step]
    assert kind == "async", step
    held: dict = {}

    async def component(*_args, **_kwargs):
        held["task"] = asyncio.current_task()
        try:
            await asyncio.Event().wait()  # only a cancellation ends it
        finally:
            candidate_dir = captured["job"].candidate_dir
            present = await asyncio.to_thread(Path(candidate_dir).is_dir)
            run.trace.append(("cleaned", step, present))

    monkeypatch.setattr(run.grade_template, name, component)
    return held


@pytest.mark.parametrize("step", ["layout", "model"])
def test_reader_closing_the_stream_between_events_settles_the_running_step_first(
    monkeypatch, tmp_path, step,
):
    run = _TemplateImport(monkeypatch, tmp_path)
    captured = _read_import_stream_directly(monkeypatch, run)
    held = _hold_until_cancelled(monkeypatch, run, step, captured)

    async def scenario():
        await run.start(canonical=True)
        stream = captured["stream"]
        async for chunk in stream:
            if any(_is_step_heartbeat(step, *event) for event in _sse_chunk_events(chunk)):
                break
        candidate_dir = Path(captured["job"].candidate_dir)
        assert candidate_dir.is_dir() and not held["task"].done()
        await stream.aclose()  # the client goes away while the step is running
        # Observed when the stream has closed; the loop's own shutdown would
        # cancel a stranded component later and hide the leak.
        task = held["task"]
        return {
            "task_done": task.done(),
            "task_cancelled": task.cancelled(),
            "trace": list(run.trace),
            "candidate_dir_exists": candidate_dir.exists(),
            "session": run.session(),
        }

    closed = _run(scenario, releases=run.releases)
    assert (closed["task_done"], closed["task_cancelled"]) == (True, True), closed["trace"]
    # The component finished its cleanup while its inputs were still on disk,
    # and before the template was recorded as failed.
    trace = closed["trace"]
    assert ("cleaned", step, True) in trace, trace
    assert trace.index(("cleaned", step, True)) < trace.index(("save", "failed")), trace
    assert closed["candidate_dir_exists"] is False
    session = closed["session"]
    assert (session.status, session.error_code) == ("failed", "TEMPLATE_IMPORT_INTERRUPTED")
    assert session.error == (
        "模板导入连接已中断，所有在途步骤均已清理；本次模板不可用，请重新发起导入"
    )
    assert run.saves[-1] == {"on_event_loop": False, "status": "failed",
                             "error_code": "TEMPLATE_IMPORT_INTERRUPTED",
                             "pages": 0, "artifacts": 0}


def _built_artifacts(monkeypatch, run: _TemplateImport) -> list[dict]:
    built = []
    original = run.grade_template.build_template_artifact

    def recording(*args, **kwargs):
        artifact = original(*args, **kwargs)
        built.append(artifact)
        return artifact

    monkeypatch.setattr(run.grade_template, "build_template_artifact", recording)
    return built


def test_recognized_pages_keep_their_roles_and_frozen_artifacts(monkeypatch, tmp_path):
    run = _TemplateImport(monkeypatch, tmp_path)
    built = _built_artifacts(monkeypatch, run)

    async def scenario():
        return await run.consume(await run.start(2, page_roles=["answer_page", "answer_sheet"]))

    events = _run(scenario, releases=run.releases)
    assert events[-1][0] == "done", events
    session = run.session()
    assert [page.get("page_role") for page in session.pages] == ["answer_page", "answer_sheet"]
    assert [[q["question_no"] for q in page["questions"]] for page in session.pages] == [["1"], ["2"]]
    assert sorted(session.page_artifacts) == ["page_1", "page_2"]
    assert [session.page_artifacts[f"page_{index}"]["sha256"] for index in (1, 2)] == [
        artifact["sha256"] for artifact in built
    ]


def test_import_checkpoints_every_stage_off_the_event_loop(monkeypatch, tmp_path):
    run = _TemplateImport(monkeypatch, tmp_path)

    async def scenario():
        return await run.consume(await run.start(2))

    events = _run(scenario, releases=run.releases)
    assert events[-1][0] == "done", events
    # The endpoint writes the first "recognizing" record before the stream
    # starts; everything the import stream checkpoints is written off-loop.
    preflight, *stream = run.saves
    assert preflight["status"] == "recognizing"
    assert stream and not [save for save in stream if save["on_event_loop"]], stream
    # Each recognized page is saved before any geometry is frozen.
    first_artifact = next(index for index, save in enumerate(stream) if save["artifacts"])
    assert [save["pages"] for save in stream[:first_artifact]][-1] == 2
    assert stream[-1] == {"on_event_loop": False, "status": "ocr_done", "error_code": "",
                          "pages": 2, "artifacts": 2}


def test_import_failure_is_checkpointed_off_the_event_loop(monkeypatch, tmp_path):
    run = _TemplateImport(monkeypatch, tmp_path)

    async def empty_page(_path, **_kwargs):
        return {"questions": []}

    monkeypatch.setattr(run.grade_template, "recognize_template_pipeline", empty_page)

    async def scenario():
        return await run.consume(await run.start())

    events = _run(scenario, releases=run.releases)
    assert events[-1][0] == "error", events
    failed = run.saves[-1]
    assert (failed["status"], failed["on_event_loop"]) == ("failed", False)
    assert run.session().status == "failed"


# ── answer-sheet SSE streams ────────────────────────────────────────────────


def _record_answer_sheet_saves(monkeypatch) -> dict:
    """Record ``AnswerSheetSession.save`` threads while ``active`` is set."""
    recorder = {"active": False, "saves": []}
    save = AnswerSheetSession.save

    def recording_save(session, *args, **kwargs):
        if recorder["active"]:
            recorder["saves"].append(_on_event_loop())
        return save(session, *args, **kwargs)

    monkeypatch.setattr(AnswerSheetSession, "save", recording_save)
    return recorder


def _source_stream_case(monkeypatch, client, upload_dir):
    from routers import answer_sheet

    session = AnswerSheetSession("1" * 16, upload_dir, teacher_id=1, class_id=10)
    session.status = "source_uploaded"
    session.answer_source = "embedded"
    session.source_exam_pages = [
        {"page_id": "page_1", "path": str(Path(session.session_dir) / "first.png"), "sha256": "first-hash",
         "recognition_checkpoint": {"mode": "embedded", "sha256": "first-hash", "questions": [_question("1")]}},
        {"page_id": "page_2", "path": str(Path(session.session_dir) / "second.png"), "sha256": "second-hash"},
    ]
    session.save()

    async def recognize(_path):
        return {"questions": [_question("2")]}

    monkeypatch.setattr(answer_sheet, "recognize_template_with_answers", recognize)
    return f"/api/answer-sheet/sessions/{session.session_id}/source/recognize/stream"


def _upload_stream_case(monkeypatch, client, upload_dir):
    from routers import answer_sheet

    session = _student_upload_session(upload_dir, "9" * 16)
    started = client.post(
        f"/api/answer-sheet/sessions/{session.session_id}/students/upload/start",
        json={"restart": False},
    ).json()
    upload_id = started["student_upload"]["upload_id"]
    for filename, raw in (("first.png", _png_bytes().getvalue()), ("second.png", _different_png_bytes())):
        uploaded = client.post(
            f"/api/answer-sheet/sessions/{session.session_id}/students/upload",
            data={"upload_id": upload_id},
            files=[("files", (filename, io.BytesIO(raw), "image/png"))],
        )
        assert uploaded.status_code == 200, uploaded.text

    def inspect(_path):
        return {"orientation_status": "auto_passed", "orientation_reason": "test", "orientation_confidence": 1.0}

    monkeypatch.setattr(answer_sheet, "_accelerator_managed_orientation", inspect)
    return f"/api/answer-sheet/sessions/{session.session_id}/students/upload/{upload_id}/process/stream"


def _students_stream_case(monkeypatch, client, upload_dir):
    from routers import answer_sheet

    session = AnswerSheetSession("2" * 16, upload_dir, teacher_id=1, class_id=10)
    session.status = "recognizing"
    session.layout_hash = "layout"
    session.student_pages = [
        {"paper_id": f"student_{index}", "path": str(Path(session.session_dir) / f"s{index}.png"),
         "sha256": f"hash-{index}", "review_reasons": []}
        for index in (1, 2)
    ]
    session.save()
    monkeypatch.setattr(answer_sheet, "TemplateEvidenceMatcher", lambda *_args: object())

    async def recognize_page(locked, page, *_args):
        page["recognition_checkpoint"] = {
            "signature": answer_sheet._student_recognition_signature(locked, page),
            "state": "completed",
        }

    monkeypatch.setattr(answer_sheet, "_recognize_one_student_page", recognize_page)
    return f"/api/answer-sheet/sessions/{session.session_id}/students/recognize/stream"


def _grading_stream_case(monkeypatch, client, upload_dir):
    from services import grader

    session = AnswerSheetSession("3" * 16, upload_dir, teacher_id=1, class_id=10)
    session.status = "review"
    session.review_revision = 7
    session.layout_hash = "layout-hash"
    session.answer_spec_hash = "answer-hash"
    session.answer_spec_snapshot = [_question("1"), _question("2")]
    session.student_pages = [{
        "paper_id": f"student_{index}",
        "orientation_status": "teacher_confirmed",
        "review_reasons": [],
        "registration": {"status": "passed", "diagnostics": {"answer_source_complete": True}},
        "identity": {"status": "teacher_confirmed", "student_id": student_id, "student_name": f"学生{index}"},
        "answers": [{"question_no": str(index), "answer": "A", "status": "teacher_confirmed"}],
    } for index, student_id in enumerate((101, 102), 1)]
    session.save()

    async def grade(_paper, _answers, _student_name, *_args, **_kwargs):
        return {"total": 1, "needs_review_count": 0, "details": [{"question_no": "1", "result": "correct"}]}

    monkeypatch.setattr(grader, "grade_paper_unified", grade)
    return f"/api/answer-sheet/sessions/{session.session_id}/grading/draft/stream"


@pytest.mark.parametrize("case", [
    _source_stream_case, _upload_stream_case, _students_stream_case, _grading_stream_case,
], ids=["source_recognize", "student_upload_process", "students_recognize", "grading_draft"])
def test_answer_sheet_stream_checkpoints_run_off_the_event_loop(client_env, monkeypatch, case):
    make_client, upload_dir = client_env
    client = make_client(1)
    recorder = _record_answer_sheet_saves(monkeypatch)
    endpoint = case(monkeypatch, client, upload_dir)

    recorder["active"] = True
    response = client.post(endpoint)
    recorder["active"] = False

    assert response.status_code == 200
    assert _sse_events(response)[-1][0] == "done", _sse_events(response)
    assert recorder["saves"], "the stream must checkpoint its progress"
    assert True not in recorder["saves"], recorder["saves"]


def test_template_import_deadline_scales_with_pages_and_has_a_hard_cap():
    import routers.grade as grade_router
    from routers import grade_template

    one_page = grade_template._template_import_deadline_seconds(1)
    ten_pages = grade_template._template_import_deadline_seconds(10)
    max_upload = grade_template._template_import_deadline_seconds(400)

    assert one_page == grade_template._TEMPLATE_IMPORT_BASE_DEADLINE_S
    assert one_page < ten_pages < max_upload
    assert max_upload == grade_template._TEMPLATE_IMPORT_MAX_DEADLINE_S


def test_template_import_deadline_uses_bounded_local_page_cap():
    import routers.grade as grade_router
    from routers import grade_template

    # Template correction and frozen-column crops are local CPU work.  The
    # request must no longer inherit the retired accelerator queue budget.
    assert grade_template._TEMPLATE_IMPORT_PAGE_HARD_CAP_S == 9 * 60.0
    assert grade_template._TEMPLATE_IMPORT_BASE_DEADLINE_S == (
        grade_template._TEMPLATE_IMPORT_PAGE_HARD_CAP_S
    )
    assert grade_template._TEMPLATE_IMPORT_PER_ADDITIONAL_PAGE_S == (
        grade_template._TEMPLATE_IMPORT_PAGE_HARD_CAP_S
    )
    assert grade_template._template_import_deadline_seconds(1) == 9 * 60.0
    assert grade_template._template_import_deadline_seconds(2) == 18 * 60.0
    assert grade_template._template_import_deadline_seconds(
        grade_template.MAX_TEMPLATE_PAGES
    ) == grade_template._TEMPLATE_IMPORT_MAX_DEADLINE_S


@pytest.mark.parametrize("remaining,pages,expected", [(480, 1, 450), (960, 2, 450), (600, 2, 270), (20, 1, 0), (-1, 1, 0)])
def test_page_model_budget_shares_remaining_time_and_reserves_save(monkeypatch, remaining, pages, expected):
    import routers.grade as grade_router
    from routers import grade_template

    class Clock:
        @staticmethod
        def time():
            return 100.0

    monkeypatch.setattr(grade_router.asyncio, "get_running_loop", lambda: Clock())
    assert grade_template._template_page_model_deadline(100.0 + remaining, pages) == 100.0 + expected


def test_page_model_budget_is_measured_on_the_import_clock_and_given_as_loop_time(monkeypatch):
    import routers.grade as grade_router
    from routers import grade_template

    class Loop:
        @staticmethod
        def time():
            return 100.0

    monkeypatch.setattr(grade_router.asyncio, "get_running_loop", lambda: Loop())
    monkeypatch.setattr(grade_template, "_template_import_clock", lambda: 7.0)
    # 480 s of the import remain on its own clock; the model stages time out
    # on the event loop's, so the page deadline is a loop time.
    assert grade_template._template_page_model_deadline(7.0 + 480, 1) == 100.0 + 450


def test_model_progress_displays_only_known_running_phases():
    import routers.grade as grade_router
    from routers import grade_template

    progress = {"stages": {
        "template-directory-v1": {"status": "completed"},
        "template-drawing-reference-v1": {"status": "running"},
        "template-directory-drawing-v1": {"status": "running"},
        "untrusted prompt content": {"status": "running"},
    }}
    assert grade_template._template_model_progress_message(1, progress) == "第 2 页正在定位作图区域"
    assert grade_template._template_model_progress_message(0, {}) == "第 1 页正在整理已完成结果"


def test_template_component_timeout_cancels_and_awaits_cleanup():
    import routers.grade as grade_router
    from routers import grade_template

    async def scenario():
        cleaned = asyncio.Event()

        async def component():
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        task = asyncio.create_task(component())
        await asyncio.sleep(0)
        with pytest.raises(grade_template._TemplateImportDeadlineExceeded):
            await grade_template._poll_template_component(
                task,
                deadline_at=asyncio.get_running_loop().time() - 1,
            )
        assert task.done()
        assert cleaned.is_set()

    asyncio.run(scenario())


def test_template_component_preserves_its_own_timeout_error():
    import routers.grade as grade_router
    from routers import grade_template

    async def scenario():
        async def component():
            raise TimeoutError("provider read timed out")

        task = asyncio.create_task(component())
        with pytest.raises(TimeoutError, match="provider read timed out"):
            await grade_template._poll_template_component(
                task,
                deadline_at=asyncio.get_running_loop().time() + 60,
            )

    asyncio.run(scenario())


def test_template_cpu_component_cancellation_waits_for_worker_thread():
    import routers.grade as grade_router
    from routers import grade_template

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_component():
        started.set()
        release.wait(timeout=2)
        finished.set()

    async def scenario():
        component_task = asyncio.create_task(
            grade_template._run_template_cpu_component(blocking_component)
        )
        while not started.is_set():
            await asyncio.sleep(0.001)

        poll_task = asyncio.create_task(grade_template._poll_template_component(
            component_task,
            deadline_at=asyncio.get_running_loop().time() + 60,
        ))
        await asyncio.sleep(0)
        poll_task.cancel()
        for _ in range(100):
            if poll_task.done():
                break
            await asyncio.sleep(0.01)
        assert poll_task.done()
        assert not finished.is_set()

        with pytest.raises(asyncio.CancelledError):
            await poll_task

        release.set()
        async with asyncio.timeout(1):
            while not finished.is_set():
                await asyncio.sleep(0.005)
        assert component_task.done()

    asyncio.run(scenario())


def test_detached_template_event_buffer_is_bounded_and_preserves_terminal_event():
    import routers.grade as grade_router
    from routers import grade_template

    async def scenario():
        events = grade_template._TemplateRecognitionEventBuffer()
        for index in range(grade_template._TEMPLATE_EVENT_BUFFER_SIZE * 3):
            events.publish(grade_router._sse("progress", {"current": index}))
        events.publish(grade_router._sse("done", {"template_id": "template-1"}))

        chunks = []
        async for chunk in events.subscribe():
            chunks.append(chunk)
        assert len(chunks) == grade_template._TEMPLATE_EVENT_BUFFER_SIZE
        assert chunks[-1].startswith("event: done\n")

    asyncio.run(scenario())
