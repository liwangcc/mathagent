import asyncio
import ast
import threading
from pathlib import Path

import pytest


def _template_router_segment(name: str) -> str:
    from routers import grade_template
    source = Path("routers/grade_template.py").read_text(encoding="utf-8")
    node = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.name == name
    )
    segment = ast.get_source_segment(source, node)
    assert segment is not None
    return segment


def test_template_recognition_stream_has_bounded_heartbeats_and_checkpoints():
    assert "_template_import_deadline_seconds(len(files))" in _template_router_segment(
        "batch_grade_template_recognize"
    )
    # The import stream and the per-page phases it runs.
    events = "\n".join(_template_router_segment(name) for name in (
        "_template_import_events",
        "_template_page_events",
        "_correct_template_page_events",
        "_template_layout_events",
        "_template_model_events",
        "_save_recognized_template_page",
        "_freeze_template_page_events",
        "_finish_template_import",
    ))
    # Every slow step is a tracked task polled against the import deadline,
    # and whatever still runs when the import ends early is settled.
    assert events.count("steps.start(asyncio.create_task(") >= 5
    assert events.count("while not await steps.poll():") >= 5
    assert "await steps.settle()" in events
    steps = _template_router_segment("_TemplateImportSteps")
    assert "_poll_template_component(" in steps
    assert "await _settle_template_component(" in steps
    assert '"heartbeat": True' in _template_router_segment("_template_heartbeat")
    assert '"heartbeat": True' in events
    assert "sess.add_page(idx, recognized.questions, page_role=page.role" in events
    assert "sess.page_artifacts[page_label] = artifact" in _template_router_segment(
        "_bind_template_import_artifact"
    )
    # Checkpoints are written off the event loop.
    failure = _template_router_segment("_fail_template_import")
    assert "sess.save_meta()" not in events
    assert "sess.save_meta()" not in failure
    assert events.count("await asyncio.to_thread(sess.save_meta)") >= 6
    assert "await asyncio.to_thread(sess.save_meta)" in failure


@pytest.mark.parametrize(
    ("function_name", "minimum_offloaded_saves"),
    [
        ("recognize_source_stream", 4),
        ("process_student_upload_stream", 5),
        ("recognize_students_stream", 5),
        ("build_grading_draft_stream", 4),
    ],
)
def test_answer_sheet_stream_checkpoints_do_not_block_the_event_loop(
    function_name,
    minimum_offloaded_saves,
):
    source = Path("routers/answer_sheet.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == function_name
    )
    segment = ast.get_source_segment(source, function)
    assert segment is not None
    assert "locked.save()" not in segment
    assert (
        segment.count("await asyncio.to_thread(locked.save)")
        >= minimum_offloaded_saves
    )


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
