"""Recognition streams keep a client informed while work is still in flight.

This replaces a frontend test that regex-matched the source of
``routers/grade.py`` (the multiplexer call, its ``asyncio.wait`` arguments and
a message literal).  That only proved the text existed; these tests prove the
behaviour, so the backend can be restructured without editing frontend tests:

* the OCR/grading multiplexer wakes on whichever result arrives first and
  emits a heartbeat when neither arrived within one heartbeat interval;
* initial recognition, a resume and the grading drain each stream their own
  heartbeat progress event *while* the slow work is still pending.

The slow work is released only after the client has received a heartbeat, so
nothing here depends on wall-clock timing beyond a 10 ms heartbeat interval.
"""
import asyncio
from contextlib import aclosing

import pytest

from models_user import User
from tests.test_batch_calibration_flow import _setup
from tests.test_recognition_stream_characterization import (
    _Boundary,
    _checkpoint,
    _decode,
    _direct_recognize,
    _template,
)

OCR_WAIT = "正在进行本地配准与答案区域预处理，请稍候"
RESUME_OCR_WAIT = "正在恢复未完成的试卷识别，请保持页面打开"
GRADING_WAIT = "正在批阅已识别的试卷，请保持页面打开"


@pytest.fixture(autouse=True)
def fast_heartbeats(monkeypatch):
    import routers.grade as grade
    from services import task_queue

    async def no_report(*_args):
        return None

    monkeypatch.setattr(grade, "_GRADE_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(task_queue, "_grading_queue", None)
    monkeypatch.setattr(grade, "_start_batch_report_generation", no_report)


class _Pipeline:
    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()


def _run(scenario):
    async def bounded():
        return await asyncio.wait_for(scenario(), timeout=10)

    return asyncio.run(bounded())


def test_multiplexer_heartbeats_then_wakes_on_whichever_result_arrives():
    import routers.grade as grade

    release_ocr = asyncio.Event()
    closed = []

    async def ocr_results():
        try:
            await release_ocr.wait()
            yield 0, {"questions": []}
        finally:
            closed.append(True)

    async def scenario():
        pipeline = _Pipeline()
        seen = []
        async with aclosing(grade._ocr_and_grading_events(ocr_results(), pipeline)) as events:
            async for kind, value in events:
                seen.append([kind, value])
                if kind == "heartbeat" and len(seen) == 1:
                    await pipeline.queue.put(("graded paper", None, {}))
                elif kind == "graded":
                    release_ocr.set()
        return seen

    seen = _run(scenario)
    kinds = [kind for kind, _ in seen]
    assert kinds[0] == "heartbeat"
    assert ["graded", ("graded paper", None, {})] in seen
    assert kinds.index("graded") < kinds.index("ocr")
    assert seen[-1] == ["ocr", (0, {"questions": []})]
    assert closed == [True], "the OCR stream is closed once it is exhausted"


def test_multiplexer_closed_early_settles_waits_and_closes_ocr_stream():
    import routers.grade as grade

    closed = []

    async def ocr_results():
        try:
            await asyncio.Event().wait()
            yield 0, {}
        finally:
            closed.append(True)

    async def scenario():
        events = grade._ocr_and_grading_events(ocr_results(), _Pipeline())
        first = await anext(events)
        await events.aclose()
        return first

    assert _run(scenario) == ("heartbeat", None)
    assert closed == [True]


def _hold_until(monkeypatch, attribute_owner, name, heard: asyncio.Event, *, wrap):
    original = getattr(attribute_owner, name)
    monkeypatch.setattr(attribute_owner, name, wrap(original, heard))


def _block_ocr(original, heard):
    async def blocked(image_path, *args, **kwargs):
        result = await original(image_path, *args, **kwargs)
        await heard.wait()
        return result
    return blocked


def _block_grading(original, heard):
    async def blocked(questions, detail_level="detailed", *, teacher_material=None):
        await heard.wait()
        return await original(questions, detail_level, teacher_material=teacher_material)
    return blocked


async def _consume(response, heard: asyncio.Event, *, message: str) -> list[list]:
    """Read the whole stream; release the held work once the heartbeat arrives."""
    events = []
    async for chunk in response.body_iterator:
        event, payload = _decode(chunk)
        events.append([event, payload])
        if event == "progress" and payload.get("heartbeat") and payload.get("message") == message:
            heard.set()
    return events


def _heartbeats(events, message):
    return [payload for event, payload in events
            if event == "progress" and payload.get("heartbeat") and payload.get("message") == message]


def _first(events, predicate):
    return next(index for index, item in enumerate(events) if predicate(*item))


def test_initial_recognition_heartbeats_while_ocr_is_pending(monkeypatch, tmp_path):
    import routers.grade as grade

    _client, Session = _setup(monkeypatch, tmp_path, education_stage="senior")
    _template(tmp_path)
    _Boundary(monkeypatch)
    heard = asyncio.Event()
    _hold_until(monkeypatch, grade, "recognize_paper_with_cache", heard, wrap=_block_ocr)

    async def scenario():
        return await _consume(await _direct_recognize(Session, 1), heard, message=OCR_WAIT)

    events = _run(scenario)
    beats = _heartbeats(events, OCR_WAIT)
    assert beats, events
    assert {key: beats[0][key] for key in ("message", "current", "total", "heartbeat")} == {
        "message": OCR_WAIT, "current": 0, "total": 1, "heartbeat": True,
    }
    assert "batch_id" not in beats[0]
    recognized = _first(events, lambda event, payload: event == "progress"
                        and str(payload.get("message", "")).startswith("已识别"))
    assert _first(events, lambda event, payload: payload is beats[0]) < recognized
    assert events[-1][0] == "done"


def test_resume_heartbeats_while_ocr_is_pending(monkeypatch, tmp_path):
    import routers.grade as grade

    _setup(monkeypatch, tmp_path)
    _checkpoint(tmp_path, jobs=("succeeded", "queued"))
    _Boundary(monkeypatch)
    heard = asyncio.Event()
    _hold_until(monkeypatch, grade, "recognize_paper_with_cache", heard, wrap=_block_ocr)

    async def scenario():
        response = await grade.batch_grade_resume_recognition(
            "resume-characterization", User(id=1, name="老师", role="teacher", current_class_id=1),
        )
        return await _consume(response, heard, message=RESUME_OCR_WAIT)

    events = _run(scenario)
    beats = _heartbeats(events, RESUME_OCR_WAIT)
    assert beats, events
    assert beats[0]["batch_id"] == "resume-characterization"
    assert beats[0]["current"] == 1 and beats[0]["total"] == 2
    recognized = _first(events, lambda event, payload: event == "progress"
                        and str(payload.get("message", "")).startswith("已识别"))
    assert _first(events, lambda event, payload: payload is beats[0]) < recognized
    assert events[-1][0] == "done"


def test_grading_drain_heartbeats_while_the_last_paper_is_graded(monkeypatch, tmp_path):
    from services import grader

    _client, Session = _setup(monkeypatch, tmp_path, education_stage="senior")
    _template(tmp_path)
    _Boundary(monkeypatch)
    heard = asyncio.Event()
    _hold_until(monkeypatch, grader, "judge_paper", heard, wrap=_block_grading)

    async def scenario():
        return await _consume(await _direct_recognize(Session, 1), heard, message=GRADING_WAIT)

    events = _run(scenario)
    beats = _heartbeats(events, GRADING_WAIT)
    assert beats, events
    recognized = _first(events, lambda event, payload: event == "progress"
                        and str(payload.get("message", "")).startswith("已识别"))
    result = _first(events, lambda event, payload: event == "result")
    beat = _first(events, lambda event, payload: payload is beats[0])
    assert recognized < beat < result
    assert events[-1][0] == "done"
