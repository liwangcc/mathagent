"""Offline early classification, identity, durability and real report handoff."""
import asyncio
import copy
import json
from pathlib import Path

import pytest

from services import template_knowledge as knowledge
from services.template_session import TemplateSession
from services.template_page_numbering import globalize_page_numbers
from services.teaching_context import make_teaching_context, use_teaching_context
from services.quick_grader import QuickBatch
from services.batch_report import build_batch_report_payload


def question(number="1", text="求函数定义域", **extra):
    return {"question_number": number, "question_text": text, "question_type": "solve",
            "standard_answer": "x>0", "max_score": 5, **extra}


def session(tmp_path, *, context=None, name="knowledge"):
    sess = TemplateSession(name, str(tmp_path), teacher_id=7, teaching_context=context)
    sess.category = "daily"
    sess.status = "ocr_done"
    return sess


def classified(items):
    return [{"id": item["id"], "status": "suggested", "chapter": "函数",
             "topic": item["question"], "confidence": "high", "classification_version": "test"}
            for item in reversed(items)]


def knowledge_tasks(root):
    """The classification tasks of the ledgers under ``root`` (this test's)."""
    prefix = str(Path(root).resolve())
    return {task for (path, _identity), task in knowledge._tasks.items() if path.startswith(prefix)}


async def wait_idle(root):
    """Wait until this test's classification tasks have finished, ledger writes included."""
    tasks = knowledge_tasks(root)
    if tasks:
        await asyncio.gather(*tasks)


async def first_settled(event, *tasks):
    """Wait for ``event``, or until one of ``tasks``, which would set it, has ended.

    Callers assert ``event.is_set()`` next: a task that ended first means the
    event can no longer come, and the assertion says so instead of a hang.
    """
    waiter = asyncio.ensure_future(event.wait())
    try:
        await asyncio.wait({waiter, *tasks}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()


class ReconcileClock:
    """The reconcile deadline's clock; it moves only when the test advances it."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.mark.asyncio
async def test_transcription_starts_classifier_before_max_and_does_not_delay_readiness(tmp_path, monkeypatch):
    from PIL import Image
    from services import template_directory_pipeline as pipeline, template_one_three as contract
    from services.template_recognition import recognize_template_pipeline
    from tests.test_template_one_three import v4_payloads

    raw_hierarchy, payloads = v4_payloads((1, 1))
    source = tmp_path / "synthetic.png"
    Image.new("RGB", (300, 400), "white").save(source)
    max_started, max_release, classify_started, classify_release = (asyncio.Event() for _ in range(4))
    transcribed = asyncio.Event()
    calls = []

    async def request(images, prompt, phase, validator, **kwargs):
        if phase == "template-hierarchy-v1":
            return validator(raw_hierarchy)
        if phase == "template-coarse-locator-v1":
            return validator({"coordinate_space": "normalized_1000_v1", "questions": []})
        if phase in {"template-directory-v1", contract.SOLVE_PHASE}:
            max_started.set()
            await max_release.wait()
        result = validator(copy.deepcopy(payloads[phase]))
        if phase == contract.TRANSCRIBE_PHASE:
            transcribed.set()
        return result

    async def classifier(items):
        calls.append(copy.deepcopy(items))
        classify_started.set()
        await classify_release.wait()
        return classified(items)

    monkeypatch.setattr(pipeline, "_request", request)
    sess = session(tmp_path, context=make_teaching_context("primary"))
    sess.save_meta()
    with use_teaching_context(sess.teaching_context):
        operation = asyncio.create_task(recognize_template_pipeline(
            str(source), layout_result={}, on_text_ready=lambda text: knowledge.start_page_classification(
                sess, 0, text, classifier=classifier)))
    await first_settled(max_started, operation)
    assert max_started.is_set(), operation
    await first_settled(transcribed, operation)
    assert transcribed.is_set(), operation
    # The text-ready callback runs as the transcription returns, in the same step.
    classification = knowledge_tasks(tmp_path)
    assert classification
    await first_settled(classify_started, *classification)
    assert classify_started.is_set(), classification
    assert not max_release.is_set() and not operation.done()
    max_release.set()
    result = await operation
    assert "error" not in result
    assert not classify_release.is_set()  # readiness never joins classification
    merged = globalize_page_numbers(result, 0, page_index=0)
    sess.add_page(0, merged["questions"])
    knowledge.bind_page_knowledge(sess, 0, merged["questions"])
    sess.status = "confirmed"
    sess.confirm_current_contract(7)
    sess.save_meta()
    original = copy.deepcopy(sess.pages)
    frozen = sess.content_hash()
    waiter = asyncio.create_task(knowledge.ensure_session_knowledge(sess, classifier=classifier))
    classify_release.set()
    assert (await waiter)["status"] == "ready"
    # Simulate the remaining recognition stages overwriting a stale meta object.
    sess.knowledge_classifications = {}
    sess.save_meta()
    loaded = TemplateSession.load_meta(sess.template_id, str(tmp_path))
    assert len(calls) == 1
    assert loaded.pages == original
    assert loaded.content_hash() == frozen == loaded.confirmed_contract_hash
    assert loaded.build_answer_map()["1"]["topic"] == "印刷题干1"
    assert loaded.build_answer_map()["2"]["topic"] == "印刷题干2"
    assert loaded.recognition_contract_error() == ""


@pytest.mark.asyncio
async def test_page_local_mapping_reordered_results_and_choice_text_are_derived_only(tmp_path):
    sess = session(tmp_path)
    calls = []

    async def classifier(items):
        calls.append(items)
        return classified(items)

    for index, text in enumerate(("第一页", "第二页")):
        knowledge.start_page_classification(sess, index, {"questions": [
            {"q_id": "Q2", "question_text": text + "选择题"},
            {"q_id": "Q1", "question_text": text + "填空题"},
        ]}, classifier=classifier)
        merged = globalize_page_numbers({"questions": [
            question("1", text + "填空题"),
            question("2", "", question_type="choice", choice_mode="single", standard_answer="A"),
        ]}, index * 2, page_index=index)
        sess.add_page(index, merged["questions"])
        knowledge.bind_page_knowledge(sess, index, list(reversed(merged["questions"])))
    sess.save_meta()
    assert (await knowledge.ensure_session_knowledge(sess, classifier=classifier))["status"] == "ready"
    loaded = TemplateSession.load_meta(sess.template_id, str(tmp_path))
    answers = loaded.build_answer_map()
    assert [answers[str(i)]["topic"] for i in range(1, 5)] == [
        "第一页填空题", "第一页选择题", "第二页填空题", "第二页选择题"]
    assert answers["2"]["question_text"] == answers["4"]["question_text"] == ""
    assert len(calls) == 2
    assert set(loaded.knowledge_classifications) == {"0:1", "0:2", "1:3", "1:4"}


@pytest.mark.asyncio
async def test_late_pre_edit_result_cannot_label_new_text_or_replace_teacher_fields(tmp_path):
    sess = session(tmp_path)
    old_started, old_release = asyncio.Event(), asyncio.Event()
    calls = []

    async def classifier(items):
        calls.append(items)
        if items[0]["question"] == "旧题":
            old_started.set()
            await old_release.wait()
        return classified(items)

    knowledge.start_page_classification(sess, 0, {"questions": [{"q_id": "Q1", "question_text": "旧题"}]}, classifier=classifier)
    raw = globalize_page_numbers({"questions": [question(text="旧题")]}, 0, page_index=0)
    sess.add_page(0, raw["questions"])
    knowledge.bind_page_knowledge(sess, 0, raw["questions"])
    old = knowledge_tasks(tmp_path)
    await first_settled(old_started, *old)
    assert old_started.is_set(), old
    sess.pages[0]["questions"][0]["question_text"] = "教师新题"
    sess.pages[0]["questions"][0]["chapter"] = "教师章节"
    sess.save_meta()
    assert (await knowledge.ensure_session_knowledge(sess, classifier=classifier))["status"] == "ready"
    old_release.set()
    await wait_idle(tmp_path)
    loaded = TemplateSession.load_meta(sess.template_id, str(tmp_path))
    answer = loaded.build_answer_map()["1"]
    assert answer["chapter"] == "教师章节"
    assert answer["topic"] == "教师新题"
    assert len(calls) == 2 and calls[0][0]["id"] != calls[1][0]["id"]
    # Teacher topic as well as chapter is authoritative, even after a reload.
    loaded.pages[0]["questions"][0]["topic"] = "教师知识点"
    loaded.save_meta()
    assert loaded.build_answer_map()["1"]["topic"] == "教师知识点"


@pytest.mark.asyncio
async def test_changed_context_cannot_reuse_ready_ledger(tmp_path):
    sess = session(tmp_path, context=make_teaching_context("primary"))
    sess.add_page(0, [question()])
    calls = []

    async def classifier(items):
        calls.append(items)
        return classified(items)

    await knowledge.ensure_session_knowledge(sess, classifier=classifier)
    assert sess.build_answer_map()["1"]["topic"] == "求函数定义域"
    sess.teaching_context = make_teaching_context("junior")
    assert sess.build_answer_map()["1"]["topic"] != "求函数定义域"
    await knowledge.ensure_session_knowledge(sess, classifier=classifier)
    assert len(calls) == 2 and calls[0][0]["id"] != calls[1][0]["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "missing", "duplicate", "unknown"])
async def test_failures_are_bounded_durable_and_retryable(tmp_path, monkeypatch, failure):
    sess = session(tmp_path)
    sess.add_page(0, [question("1"), question("2", "求单调区间")])
    sess.save_meta()
    calls = []

    async def failing(items):
        calls.append(items)
        if failure == "exception":
            raise RuntimeError("synthetic provider failure")
        rows = classified(items)
        if failure == "missing":
            return rows[:1]
        if failure == "duplicate":
            return [rows[0], rows[0]]
        rows[0]["id"] = "foreign-question"
        return rows

    assert (await knowledge.ensure_session_knowledge(sess, classifier=failing))["status"] == "failed"
    assert len(calls) == knowledge.MAX_ATTEMPTS
    loaded = TemplateSession.load_meta(sess.template_id, str(tmp_path))
    assert knowledge.knowledge_state(loaded)["status"] == "failed"
    assert not loaded.knowledge_classifications
    await knowledge.ensure_session_knowledge(loaded, classifier=failing)
    assert len(calls) == knowledge.MAX_ATTEMPTS  # durable cooldown
    knowledge._mutate(sess, lambda data: [entry.update(retry_after=0) for entry in data["entries"].values()])

    async def recovered(items):
        return classified(items)

    assert (await knowledge.ensure_session_knowledge(loaded, classifier=recovered))["status"] == "ready"
    assert loaded.build_answer_map()["1"]["topic"] == "求函数定义域"


@pytest.mark.asyncio
async def test_pending_waiter_cancellation_and_foreign_lease_do_not_duplicate_calls(tmp_path):
    sess = session(tmp_path)
    sess.add_page(0, [question()])
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def classifier(items):
        calls.append(items)
        started.set()
        await release.wait()
        return classified(items)

    first = asyncio.create_task(knowledge.ensure_session_knowledge(sess, classifier=classifier))
    await first_settled(started, first)
    assert started.is_set(), first
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    # Simulate a second worker without access to this process's task registry.
    owned = dict(knowledge._tasks)
    knowledge._tasks.clear()
    try:
        assert (await knowledge.ensure_session_knowledge(sess, classifier=classifier, timeout=0.05))["status"] == "pending"
        assert len(calls) == 1
    finally:
        knowledge._tasks.update(owned)
    release.set()
    assert (await knowledge.ensure_session_knowledge(sess, classifier=classifier))["status"] == "ready"
    assert len(calls) == 1


async def make_graded_batch(tmp_path, monkeypatch, sess):
    import routers.grade as grade
    monkeypatch.setattr(grade, "UPLOAD_DIR", str(tmp_path))
    sess.status = "confirmed"
    sess.confirm_current_contract(7)
    sess.save_meta()
    batch = QuickBatch("knowledge-batch", str(tmp_path), teaching_context=sess.teaching_context)
    batch.teacher_id = 7
    batch.template_id = sess.template_id
    batch.source_mode = "template"
    batch.category = "daily"
    batch.answer_map = sess.build_answer_map()
    batch.grading_contract_hash = "synthetic-frozen-grading-contract"
    batch.grading_contract = {"confirmed": copy.deepcopy(batch.answer_map)}
    grade._pin_batch_rubric(batch, sess)
    result = {"score": 0, "max_score": 5, "total": 1, "correct_count": 0, "partial_count": 0,
              "wrong_count": 1, "needs_review_count": 0, "details": [{
                  "question_no": "1[2]", "parent_question_no": "1", "question_type": "solve",
                  "question": "合成题", "result": "wrong", "score": 0, "max_score": 5,
              }]}

    async def grade_model(**kwargs):
        return copy.deepcopy(result)

    monkeypatch.setattr(grade, "grade_paper_unified", grade_model)
    _, review, row = await grade._grade_paper_core(
        batch, {"questions": []}, "synthetic-paper", {"name": "合成样本", "student_no": "SYNTHETIC"}, 7,
        detail_level="detailed", grading_category="daily", grading_exam_name="", grading_mode="precise")
    batch.review_results = [review]
    batch.results = [row]
    batch.status = "review_ready"
    batch.save_meta()
    return batch


@pytest.mark.asyncio
async def test_late_classification_updates_real_grading_review_report_without_score_contract_changes(tmp_path, monkeypatch):
    import routers.grade as grade
    from routers import grade_template
    sess = session(tmp_path)
    sess.add_page(0, [question()])
    started, release = asyncio.Event(), asyncio.Event()
    calls, report_inputs = [], []

    async def classifier(items):
        calls.append(items)
        started.set()
        await release.wait()
        return classified(items)

    async def narrative(stats, **kwargs):
        report_inputs.append(copy.deepcopy(stats))
        return "合成教学报告", ""

    monkeypatch.setattr(grade, "_template_knowledge_classifier", classifier)
    monkeypatch.setattr(grade_template, "_template_knowledge_classifier", classifier)
    monkeypatch.setattr(grade, "generate_batch_report", narrative)
    # The report may wait for the labels as long as it takes: its deadline never comes.
    monkeypatch.setattr(knowledge, "_reconcile_clock", ReconcileClock())
    pending = asyncio.create_task(knowledge.ensure_session_knowledge(sess, classifier=classifier))
    await first_settled(started, pending)
    assert started.is_set(), pending
    batch = await make_graded_batch(tmp_path, monkeypatch, sess)
    original = copy.deepcopy(batch.to_dict())
    old_payload = build_batch_report_payload(batch)
    assert old_payload["stats"]["unclassified_knowledge_count"] == 1
    reconciling = asyncio.Event()
    reconcile = knowledge.ensure_session_knowledge

    async def report_reconcile(*args, **kwargs):
        reconciling.set()
        return await reconcile(*args, **kwargs)

    monkeypatch.setattr(knowledge, "ensure_session_knowledge", report_reconcile)
    await grade._start_batch_report_generation(batch, 7)
    report_task = grade._batch_report_tasks[batch.batch_id]
    await first_settled(reconciling, report_task)
    assert reconciling.is_set(), report_task
    # The report is waiting for the pending classification, not narrating stale labels.
    assert not report_inputs and not report_task.done()
    release.set()
    assert (await pending)["status"] == "ready"
    await report_task
    loaded = QuickBatch.load_meta(batch.batch_id, str(tmp_path))
    assert len(calls) == len(report_inputs) == 1
    assert loaded.report_status == "ready"
    assert loaded.report_stats["unclassified_knowledge_count"] == 0
    assert loaded.report_stats["knowledge_points"][0]["topic"] == "求函数定义域"
    assert loaded.review_results[0]["knowledge_items"][0]["question_no"] == "1"
    assert loaded.review_results[0]["knowledge_items"][0]["topic"] == "求函数定义域"
    assert loaded.report_input_hash != old_payload["input_hash"]
    assert loaded.report_input_hash == build_batch_report_payload(loaded)["input_hash"]
    for key in ("grading_contract_hash", "grading_contract", "grading_rubric_hash", "template_contract_hash", "results"):
        assert loaded.to_dict()[key] == original[key]
    assert grade._batch_rubric_hash(loaded) == batch.grading_rubric_hash
    assert loaded.review_results[0]["result"] == original["review_results"][0]["result"]
    assert TemplateSession.load_meta(sess.template_id, str(tmp_path)).content_hash() == sess.confirmed_contract_hash


@pytest.mark.asyncio
async def test_report_pending_and_failure_never_finalize_stale_labels_and_retry_recovers(tmp_path, monkeypatch):
    import routers.grade as grade
    from routers import grade_template
    sess = session(tmp_path)
    sess.add_page(0, [question()])
    batch = await make_graded_batch(tmp_path, monkeypatch, sess)
    started, release = asyncio.Event(), asyncio.Event()
    provider_calls, narrative_calls = [], []
    clock = ReconcileClock()
    monkeypatch.setattr(knowledge, "_reconcile_clock", clock)
    monkeypatch.setattr(knowledge, "RECONCILE_TIMEOUT", 0.1)

    async def failing(items):
        provider_calls.append(items)
        started.set()
        await release.wait()
        raise RuntimeError("offline")

    async def narrative(stats, **kwargs):
        narrative_calls.append(stats)
        return "报告", ""

    monkeypatch.setattr(grade, "_template_knowledge_classifier", failing)
    monkeypatch.setattr(grade_template, "_template_knowledge_classifier", failing)
    monkeypatch.setattr(grade, "generate_batch_report", narrative)
    await grade._start_batch_report_generation(batch, 7)
    task = grade._batch_report_tasks[batch.batch_id]
    await first_settled(started, task)
    assert started.is_set(), task
    # The report started this classification, so its wait is already timed:
    # let that wait run out while the provider is still working.
    clock.advance(knowledge.RECONCILE_TIMEOUT)
    await task
    pending = QuickBatch.load_meta(batch.batch_id, str(tmp_path))
    assert pending.report_status == "queued" and "知识点" in pending.report_error
    assert not narrative_calls and len(provider_calls) == 1
    release.set()
    await wait_idle(tmp_path)
    await grade._ensure_batch_report_generation(pending, 7)
    await grade._batch_report_tasks[batch.batch_id]
    failed = QuickBatch.load_meta(batch.batch_id, str(tmp_path))
    assert failed.report_status == "failed" and "知识点" in failed.report_error
    assert not narrative_calls and len(provider_calls) == knowledge.MAX_ATTEMPTS
    knowledge._mutate(sess, lambda data: [entry.update(retry_after=0) for entry in data["entries"].values()])

    async def recovered(items):
        return classified(items)

    monkeypatch.setattr(grade, "_template_knowledge_classifier", recovered)
    monkeypatch.setattr(grade_template, "_template_knowledge_classifier", recovered)
    monkeypatch.setattr(knowledge, "RECONCILE_TIMEOUT", 2)
    await grade._ensure_batch_report_generation(failed, 7)
    await grade._batch_report_tasks[batch.batch_id]
    final = QuickBatch.load_meta(batch.batch_id, str(tmp_path))
    assert final.report_status == "ready" and len(narrative_calls) == 1
    assert final.report_stats["unclassified_knowledge_count"] == 0
    assert final.results == batch.results


@pytest.mark.asyncio
async def test_report_rechecks_latest_labels_before_committing_cached_narrative(tmp_path, monkeypatch):
    import routers.grade as grade
    sess = session(tmp_path)
    sess.add_page(0, [question(chapter="教师章节", topic="教师知识点")])
    batch = await make_graded_batch(tmp_path, monkeypatch, sess)
    old_hash = build_batch_report_payload(batch)["input_hash"]
    # A mismatched frozen version must never copy a newer question's labels.
    sess.pages[0]["questions"][0]["question_text"] = "另一个新问题"
    sess.save_meta()
    grade._finish_batch_report_sync(batch.batch_id, old_hash, "过期报告", "")
    loaded = QuickBatch.load_meta(batch.batch_id, str(tmp_path))
    assert loaded.report_status == "failed" and loaded.report_text == ""
    assert loaded.results == batch.results


@pytest.mark.asyncio
async def test_real_template_route_wires_text_ready_and_global_binding(tmp_path, monkeypatch):
    import httpx
    import routers.grade as grade
    from routers import grade_template
    from services import template_directory_pipeline as pipeline, template_one_three as contract
    from tests.test_template_one_three import v4_payloads
    from tests.test_template_session import _make_app_and_session, _client, _patch_template_layout, _template_png_bytes, _parse_sse

    _, factory = _make_app_and_session()
    monkeypatch.setattr(grade, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade, "_check_limits_or_raise", lambda *_args: None)
    _patch_template_layout(monkeypatch, grade)

    async def available(*args, **kwargs):
        return {"status": "available", "message": "synthetic readiness"}

    async def hardware_ready(*args, **kwargs):
        return {"status": "ready", "message": "synthetic hardware"}

    monkeypatch.setattr(grade, "batch_grade_availability", available)
    monkeypatch.setattr(grade, "_batch_grade_service_availability", available)
    monkeypatch.setattr(grade_template, "_batch_grade_service_availability", available)
    monkeypatch.setattr(grade_template, "get_column_layout_readiness", available)
    monkeypatch.setattr(grade, "get_public_hardware_readiness", hardware_ready)
    monkeypatch.setattr(grade_template, "get_public_hardware_readiness", hardware_ready)
    raw, branches = v4_payloads((1,))
    classifier_started, classifier_release, max_release = (asyncio.Event() for _ in range(3))
    transcribed = asyncio.Event()
    calls = []

    async def request(images, prompt, phase, validator, **kwargs):
        if phase == "template-hierarchy-v1":
            return validator(raw)
        if phase == "template-coarse-locator-v1":
            raise ValueError("synthetic coarse hint unavailable")
        if phase in {contract.SOLVE_PHASE, "template-directory-v1"}:
            await max_release.wait()
        result = validator(copy.deepcopy(branches[phase]))
        if phase == contract.TRANSCRIBE_PHASE:
            transcribed.set()
        return result

    async def classifier(items):
        calls.append(items)
        classifier_started.set()
        await classifier_release.wait()
        return classified(items)

    monkeypatch.setattr(pipeline, "_request", request)
    monkeypatch.setattr(grade, "_template_knowledge_classifier", classifier)
    monkeypatch.setattr(grade_template, "_template_knowledge_classifier", classifier)
    app = _client(factory).app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        importing = asyncio.create_task(client.post(
            "/api/batch-grade/template/recognize", data={"template_mode": "marked", "category": "daily"},
            files=[("files", ("synthetic.png", _template_png_bytes(), "image/png"))]))
        await first_settled(transcribed, importing)
        assert transcribed.is_set(), importing
        # The text-ready callback runs as the transcription returns, in the same step.
        classification = knowledge_tasks(tmp_path)
        assert classification
        await first_settled(classifier_started, *classification)
        assert classifier_started.is_set(), classification
        assert not importing.done()
        max_release.set()
        response = await importing
        events = _parse_sse(response.text)
        assert response.status_code == 200
        assert not [payload for event, payload in events if event == "error"], events
        template_id = next(payload["template_id"] for event, payload in events if payload.get("template_id"))
        sess = TemplateSession.load_meta(template_id, str(tmp_path))
        before = copy.deepcopy(sess.pages)
        assert sess.status == "ocr_done" and not classifier_release.is_set()
        classifier_release.set()
        await wait_idle(tmp_path)
        loaded = TemplateSession.load_meta(template_id, str(tmp_path))
        assert loaded.pages == before
        assert loaded.build_answer_map()["1"]["topic"] == "印刷题干1"
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_restart_recovers_expired_lease_without_touching_contract(tmp_path):
    sess = session(tmp_path)
    sess.add_page(0, [question()])
    sess.save_meta()
    identities = knowledge._prepare_session(sess)
    knowledge._mutate(sess, lambda data: data["entries"][identities[0]].update(
        status="running", owner="departed-worker", lease_until=0))
    restored = TemplateSession.load_meta(sess.template_id, str(tmp_path))
    calls = []

    async def classifier(items):
        calls.append(items)
        return classified(items)

    assert (await knowledge.ensure_session_knowledge(restored, classifier=classifier))["status"] == "ready"
    assert len(calls) == 1 and restored.pages == sess.pages


@pytest.mark.asyncio
async def test_unavailable_choice_text_is_visible_without_phantom_pending_job(tmp_path, monkeypatch):
    import routers.grade as grade
    from routers import grade_template
    sess = session(tmp_path)
    sess.add_page(0, [question(text="", question_type="choice", choice_mode="single", standard_answer="A")])
    batch = await make_graded_batch(tmp_path, monkeypatch, sess)

    async def forbidden(items):
        pytest.fail("empty choice text must not call the classifier")

    async def narrative(stats, **kwargs):
        return "统计报告", ""

    monkeypatch.setattr(grade, "_template_knowledge_classifier", forbidden)
    monkeypatch.setattr(grade_template, "_template_knowledge_classifier", forbidden)
    monkeypatch.setattr(grade, "generate_batch_report", narrative)
    await grade._start_batch_report_generation(batch, 7)
    await grade._batch_report_tasks[batch.batch_id]
    loaded = QuickBatch.load_meta(batch.batch_id, str(tmp_path))
    assert loaded.report_status == "ready"
    assert "缺少可分类文字" in loaded.report_error
    assert loaded.report_stats["unclassified_knowledge_count"] == 1
    assert loaded.results == batch.results


@pytest.mark.asyncio
async def test_cached_report_refreshes_late_metadata_and_preserves_concurrent_teacher_decision(tmp_path, monkeypatch):
    import routers.grade as grade
    sess = session(tmp_path)
    sess.add_page(0, [question()])
    batch = await make_graded_batch(tmp_path, monkeypatch, sess)
    batch.report_status = "ready"
    batch.report_input_hash = build_batch_report_payload(batch)["input_hash"]
    batch.report_text = "旧未分类报告"
    batch.save_meta()
    request_snapshot = QuickBatch.load_meta(batch.batch_id, str(tmp_path))

    async def classifier(items):
        return classified(items)

    async def narrative(stats, **kwargs):
        return "新报告", ""

    await knowledge.ensure_session_knowledge(sess, classifier=classifier)
    # A teacher changed the verdict after the report request's initial load.
    latest = QuickBatch.load_meta(batch.batch_id, str(tmp_path))
    latest.review_revision = 9
    latest.review_results[0]["result"]["details"][0]["result"] = "correct"
    latest.review_results[0]["result"]["details"][0]["score"] = 5
    latest.review_results[0]["result"]["score"] = 5
    latest.review_results[0]["manual_corrections"] = [{"synthetic_teacher_decision": True}]
    latest.save_meta()
    monkeypatch.setattr(grade, "generate_batch_report", narrative)
    await grade._ensure_batch_report_generation(request_snapshot, 7)
    await grade._batch_report_tasks[batch.batch_id]
    loaded = QuickBatch.load_meta(batch.batch_id, str(tmp_path))
    assert loaded.report_status == "ready" and loaded.report_text == "新报告"
    assert loaded.review_revision == 9
    assert loaded.review_results[0]["result"] == latest.review_results[0]["result"]
    assert loaded.review_results[0]["manual_corrections"] == latest.review_results[0]["manual_corrections"]
    assert loaded.report_stats["correct_count"] == 1
    assert loaded.report_stats["knowledge_points"][0]["topic"] == "求函数定义域"


@pytest.mark.asyncio
async def test_provider_configuration_failure_has_one_attempt_and_corrupt_sidecar_does_not_fail_grading(tmp_path, monkeypatch):
    from services.analyzer import KnowledgeClassifierConfigurationError
    sess = session(tmp_path)
    sess.add_page(0, [question()])
    sess.save_meta()
    calls = []

    async def unconfigured(items):
        calls.append(items)
        raise KnowledgeClassifierConfigurationError("synthetic missing route")

    assert (await knowledge.ensure_session_knowledge(sess, classifier=unconfigured))["status"] == "failed"
    assert len(calls) == 1
    knowledge._path(sess).write_text("{corrupt synthetic ledger", encoding="utf-8")
    loaded = TemplateSession.load_meta(sess.template_id, str(tmp_path))
    assert loaded.pages == sess.pages and loaded.build_answer_map()["1"]["standard_answer"] == "x>0"
    batch = await make_graded_batch(tmp_path, monkeypatch, loaded)
    import routers.grade as grade
    assert grade._refresh_batch_knowledge(batch, 7)["status"] == "failed"
    assert batch.review_results[0]["result"]["score"] == 0


@pytest.mark.asyncio
async def test_canonical_classifier_keeps_strict_catalog_gate_and_text_only_payload(tmp_path, monkeypatch):
    from services import analyzer
    from services.math_curriculum import get_math_catalog
    sess = session(tmp_path, context=make_teaching_context("primary"))
    sess.add_page(0, [question("1"), question("2", "计算两个数的和")])
    submitted = []
    calls = []
    outside = True
    catalog = get_math_catalog("primary")
    chapter, topic = catalog[0]["chapter"], catalog[0]["topics"][0]

    async def text_transport(messages, **kwargs):
        calls.append(messages)
        assert all(isinstance(message["content"], str) for message in messages)
        return json.dumps({"results": [{"id": row["id"], "knowledge_points": [{
            "chapter": chapter, "topic": "目录外标签" if outside and row["question_no"] == "1" else topic,
        }]} for row in submitted]}, ensure_ascii=False)

    async def canonical(items):
        submitted[:] = items
        return await analyzer.classify_questions_knowledge(items, per_question=True)

    monkeypatch.setattr(analyzer, "_call_knowledge_classifier_text", text_transport)
    assert (await knowledge.ensure_session_knowledge(sess, classifier=canonical))["status"] == "failed"
    assert len(calls) == knowledge.MAX_ATTEMPTS
    # The off-catalog label is never stored, and it no longer discards the
    # valid label of question 2; only question 1 is retried, alone.
    assert set(knowledge.load_classifications(sess)) == {sess.knowledge_classification_key(0, "2")}
    assert sess.build_answer_map()["2"]["topic"] == topic
    assert sess.build_answer_map()["1"]["topic"] != "目录外标签"
    outside = False
    knowledge._mutate(sess, lambda data: [entry.update(retry_after=0) for entry in data["entries"].values()])
    assert (await knowledge.ensure_session_knowledge(sess, classifier=canonical))["status"] == "ready"
    assert len(calls) == knowledge.MAX_ATTEMPTS + 1
    assert [row["question_no"] for row in submitted] == ["1"]
    assert all(row["topic"] == topic and row["knowledge_point_id"]
               for row in knowledge.load_classifications(sess).values())


@pytest.mark.asyncio
async def test_replacement_identity_reclassifies_even_when_text_matches(tmp_path):
    sess = session(tmp_path)
    sess.add_page(0, [question(printed_path=["旧章节", "1."])])
    calls = []

    async def classifier(items):
        calls.append(items)
        return classified(items)

    await knowledge.ensure_session_knowledge(sess, classifier=classifier)
    sess.pages[0]["questions"][0]["printed_path"] = ["替换后的章节", "1."]
    assert sess.build_answer_map()["1"]["topic"] != "求函数定义域"
    await knowledge.ensure_session_knowledge(sess, classifier=classifier)
    assert len(calls) == 2 and calls[0][0]["id"] != calls[1][0]["id"]


def test_sidecar_atomic_updates_survive_concurrent_snapshot_writes(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    sess = session(tmp_path)
    sess.add_page(0, [question()])
    sess.save_meta()

    def update(index):
        knowledge._mutate(sess, lambda data: data["sources"].update({str(index): {"id": str(index)}}))
        # Recognition's independently loaded metadata has no sidecar labels.
        stale = TemplateSession.load_meta(sess.template_id, str(tmp_path))
        stale.knowledge_classifications = {}
        stale.save_meta()

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(update, range(12)))
    assert set(knowledge._read(sess)["sources"]) == {str(i) for i in range(12)}
    assert TemplateSession.load_meta(sess.template_id, str(tmp_path)).pages == sess.pages


@pytest.mark.asyncio
async def test_text_ready_observer_failure_cannot_fail_template(tmp_path, monkeypatch):
    from PIL import Image
    from services import template_directory_pipeline as pipeline
    from tests.test_template_one_three import v4_payloads
    source = tmp_path / "synthetic.png"
    Image.new("RGB", (300, 400), "white").save(source)
    raw, branches = v4_payloads((1,))

    async def request(images, prompt, phase, validator, **kwargs):
        if phase == "template-hierarchy-v1":
            return validator(raw)
        if phase == "template-coarse-locator-v1":
            raise ValueError("no coarse hint")
        return validator(copy.deepcopy(branches[phase]))

    def broken_observer(text):
        raise OSError("synthetic sidecar is temporarily unavailable")

    monkeypatch.setattr(pipeline, "_request", request)
    with use_teaching_context(make_teaching_context("primary")):
        result = await pipeline.recognize_directory_template(
            str(source), template_mode="marked", layout_result={}, on_text_ready=broken_observer)
    assert "error" not in result and len(result["questions"]) == 1


def test_ledger_reads_and_path_lookups_never_fail_a_concurrent_replace(tmp_path):
    """On Windows a reader holding knowledge.json open, or resolving its path,
    fails a concurrent os.replace (WinError 5); they share the ledger's I/O lock."""
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    sess = session(tmp_path)
    sess.add_page(0, [question()])
    sess.save_meta()
    knowledge._mutate(sess, lambda data: None)
    stop = threading.Event()
    failures = []

    def until_stopped(access):
        while not stop.is_set():
            try:
                access(sess)
            except OSError as exc:
                failures.append(exc)
            time.sleep(0)  # let the writers run

    others = [threading.Thread(target=until_stopped, args=(access,))
              for access in (knowledge._read, knowledge._read, knowledge._ledger_key)]
    for thread in others:
        thread.start()
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda index: knowledge._mutate(
                sess, lambda data: data["sources"].update({str(index): {"id": str(index)}})), range(48)))
    finally:
        stop.set()
        for thread in others:
            thread.join()
    assert failures == []
    assert set(knowledge._read(sess)["sources"]) == {str(index) for index in range(48)}
