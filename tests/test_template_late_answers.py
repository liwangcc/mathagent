"""A solver request that outlasts its page no longer holds the page; its answers follow.

Measured on a real exam page (2024 college entrance exam, page 4, 2026-11-12):
the other requests of the page finished in 85 to 362 s, the solver request of
the last question took 575 s. The page is now delivered without that answer,
the request keeps running, and the answer is bound when it arrives exactly as
it would have been on time.
"""
import asyncio
import copy
import json

import pytest
from PIL import Image

import config
from services import flash_grading
from services import template_directory_pipeline as pipeline
from services import template_late_answers as late_answers
from services import template_max_owned as owned
from services import template_one_three as contract
from services.teaching_context import make_teaching_context, use_teaching_context
from tests.test_template_flash_page_numbering import SOLVE_PAGE_BY_POSITION, coarse_wire
from tests.test_template_one_three import v4_payloads

SOLVE = contract.SOLVE_PHASE
LOCATE = "template-directory-v1"
# Four questions in two shards: the first owns Q1 and Q4, the second Q2 and Q3.
SECOND, ITS_QUESTIONS = SOLVE + "#2", ["2", "3"]
PARTS = ("questions", "question_anchors", "template_answer_regions", "template_handwriting_regions",
         "requires_teacher_confirmation")


class Models:
    """Scripted models behind ``pipeline._request``; chosen requests wait until released, then answer or fail."""

    def __init__(self, hierarchy_wire=SOLVE_PAGE_BY_POSITION):
        self.wire = hierarchy_wire
        _, self.branches = v4_payloads(tuple(len(item.get("slots") or [1]) for item in hierarchy_wire["questions"]))
        self.gates, self.errors, self.started, self.cancelled = {}, {}, [], []

    def hold(self, *keys):
        self.gates.update({key: asyncio.Event() for key in keys})

    def release(self, key, error=None):
        if error is not None:
            self.errors[key] = error
        self.gates[key].set()

    async def request(self, images, prompt, phase, validator, **options):
        key = options.get("stage_key") or phase
        self.started.append(key)
        try:
            if key in self.gates:
                # Bounded: a test that never releases the request fails instead of hanging.
                await asyncio.wait_for(self.gates[key].wait(), timeout=20)
        except asyncio.CancelledError:
            self.cancelled.append(key)
            raise
        if key in self.errors:
            raise self.errors[key]
        if phase == "template-hierarchy-v1":
            return validator(copy.deepcopy(self.wire))
        if phase == "template-coarse-locator-v1":
            return validator(coarse_wire([str(index) for index in range(1, len(self.wire["questions"]) + 1)]))
        payload = copy.deepcopy(self.branches[phase])
        if phase == LOCATE and "#" in key:
            # A locator tile answers for its own questions only.
            payload["questions"] = [located for located in payload["questions"]
                                    if located["question_number"] in options["expected_ids"]]
        return validator(contract.parse_json(json.dumps(payload, ensure_ascii=False)))


def setup_page(tmp_path, monkeypatch, models, *, shards=2, wait_s=0.0):
    monkeypatch.setattr(config, "OCR_TEMPLATE_SOLVER_SHARDS", shards)
    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_FALLBACK_HARD_IN_FLIGHT", 4)
    monkeypatch.setattr(config, "OCR_TEMPLATE_LOCATOR_TILE_QUESTIONS", 0)
    monkeypatch.setattr(late_answers, "DELIVERY_WAIT_S", wait_s)
    monkeypatch.setattr(pipeline, "_request", models.request)
    source = tmp_path / "paper.png"
    Image.new("RGB", (600, 800), "white").save(source)
    return str(source)


async def recognize(source, *, accept=True):
    """The page result and the offer the page was recognized under (None when late answers are not accepted)."""
    with use_teaching_context(make_teaching_context("senior")):
        if not accept:
            return await pipeline.recognize_directory_template(source, template_mode="blank", layout_result={}), None
        with late_answers.accepting() as offer:
            return await pipeline.recognize_directory_template(source, template_mode="blank", layout_result={}), offer


async def collect(late) -> list:
    return [update async for update in late.updates()]


def run(scenario, *, timeout=30):
    return asyncio.run(asyncio.wait_for(scenario, timeout))


def on_time(tmp_path, monkeypatch, *, shards=2, hierarchy_wire=SOLVE_PAGE_BY_POSITION, models=None) -> dict:
    """The same page with every answer in time."""
    source = setup_page(tmp_path, monkeypatch, models or Models(hierarchy_wire), shards=shards)
    result, _ = run(recognize(source, accept=False))
    assert "error" not in result
    return result


def test_a_slow_solver_request_no_longer_holds_the_page_and_its_answers_follow(tmp_path, monkeypatch):
    complete = on_time(tmp_path, monkeypatch)
    models = Models()
    source = setup_page(tmp_path, monkeypatch, models)

    async def scenario():
        models.hold(SECOND)
        delivered, offer = await recognize(source)
        late = offer.late
        snapshot, pending = copy.deepcopy(delivered), list(late.pending)
        models.release(SECOND)
        return snapshot, pending, await collect(late), late.pending

    delivered, pending, updates, left = run(scenario())

    assert "error" not in delivered and pending == ["2", "3"] and delivered["answers_pending"] == ["2", "3"]
    for question, complete_question in zip(delivered["questions"], complete["questions"]):
        if question["question_number"] in ITS_QUESTIONS:
            # Located and transcribed like any question; only the answer is missing, and it says so.
            assert question["answer_pending"] is True and question["standard_answer"] == ""
            assert question["answer_slots"] == [""] * len(complete_question["answer_slots"])
            assert question["answer_bboxes"] == complete_question["answer_bboxes"]
            assert question["question_text"] == complete_question["question_text"]
            assert late_answers.PENDING_REASON in question["directory_issues"] and question["needs_review"] is True
            # The blank covers exactly the slots that were located: the page does not disagree with itself.
            assert not any("清单不一致" in reason for reason in question["directory_issues"])
        else:
            assert question == complete_question
    assert delivered["question_anchors"] == complete["question_anchors"]
    assert delivered["template_answer_regions"] == complete["template_answer_regions"]
    assert delivered["requires_teacher_confirmation"] == ["2", "3"]
    # One update when the request ends: the page as it would have been with every answer in time.
    (rebuilt, arrived, failed), = updates
    assert arrived == ["2", "3"] and failed == [] and left == []
    assert {part: rebuilt[part] for part in PARTS} == {part: complete[part] for part in PARTS}
    assert rebuilt["answers_pending"] == [] and rebuilt["answers_failed"] == {}
    assert not any("answer_pending" in question or "answer_failed" in question for question in rebuilt["questions"])


def test_a_page_whose_answers_are_all_in_time_is_delivered_as_before(tmp_path, monkeypatch):
    complete = on_time(tmp_path, monkeypatch)
    source = setup_page(tmp_path, monkeypatch, Models())

    result, offer = run(recognize(source))

    assert offer.late is None and "answers_pending" not in result and "answers_failed" not in result
    assert {part: result[part] for part in PARTS} == {part: complete[part] for part in PARTS}


@pytest.mark.parametrize("accept, wait_s", [
    pytest.param(False, 0.0, id="late_answers_not_accepted"),
    pytest.param(True, 60.0, id="within_the_delivery_wait"),
])
def test_the_page_waits_for_a_running_solver_request(tmp_path, monkeypatch, accept, wait_s):
    complete = on_time(tmp_path, monkeypatch)
    models = Models()
    source = setup_page(tmp_path, monkeypatch, models, wait_s=wait_s)

    async def scenario():
        models.hold(SECOND)
        recognition = asyncio.create_task(recognize(source, accept=accept))
        await asyncio.sleep(0.3)
        waiting = not recognition.done()
        models.release(SECOND)
        return waiting, await recognition

    waiting, (result, offer) = run(scenario())

    assert waiting is True
    assert "answers_pending" not in result and (offer is None or offer.late is None)
    assert {part: result[part] for part in PARTS} == {part: complete[part] for part in PARTS}


def test_the_page_is_not_delivered_before_its_locator_is_done(tmp_path, monkeypatch):
    models = Models()
    source = setup_page(tmp_path, monkeypatch, models, wait_s=0.0)

    async def scenario():
        models.hold(SECOND, LOCATE)
        recognition = asyncio.create_task(recognize(source))
        await asyncio.sleep(0.3)
        held_by_locator = not recognition.done()   # the wait has passed; the page has no boxes yet
        models.release(LOCATE)
        delivered, offer = await recognition
        offer.late.cancel()
        return held_by_locator, delivered

    held_by_locator, delivered = run(scenario())

    assert held_by_locator is True and delivered["answers_pending"] == ["2", "3"]


def test_the_delivery_wait_counts_from_the_pages_start(monkeypatch):
    monkeypatch.setattr(late_answers, "DELIVERY_WAIT_S", 50.0)

    async def scenario(page_age_s):
        """Whether a page that started ``page_age_s`` ago still waits for its running shard once the locator is done."""
        loop = asyncio.get_running_loop()
        gate = asyncio.Event()

        async def located():
            return "boxes"

        async def solving():
            await asyncio.wait_for(gate.wait(), 10)

        required, shard = asyncio.create_task(located()), asyncio.create_task(solving())
        settling = asyncio.create_task(late_answers.settle(
            (required,), [shard], started=loop.time() - page_age_s, late=True))
        await asyncio.sleep(0.3)
        waits = not settling.done()
        gate.set()
        return waits, await settling, shard

    waits, results, _ = run(scenario(page_age_s=0.0))
    assert waits is True and results == ["boxes"]
    # The solver has had its time long ago: nothing more is waited for.
    waits, results, _ = run(scenario(page_age_s=100.0))
    assert waits is False and results == ["boxes"]


def test_the_page_gives_its_solver_the_delivery_wait_before_leaving_it_behind(tmp_path, monkeypatch):
    models = Models()
    source = setup_page(tmp_path, monkeypatch, models, wait_s=0.5)

    async def scenario():
        loop = asyncio.get_running_loop()
        models.hold(SECOND)
        started = loop.time()
        delivered, offer = await recognize(source)
        elapsed = loop.time() - started
        offer.late.cancel()
        return elapsed, delivered

    elapsed, delivered = run(scenario())

    assert 0.5 <= elapsed < 5 and delivered["answers_pending"] == ["2", "3"]


@pytest.mark.parametrize("error, reason", [
    pytest.param(TimeoutError("template model budget exhausted"), late_answers.TIMED_OUT_REASON, id="timed_out"),
    pytest.param(RuntimeError("strict_contract_failure"), late_answers.FAILED_REASON, id="failed"),
])
def test_a_late_request_that_fails_only_fails_its_own_questions(tmp_path, monkeypatch, error, reason):
    complete = on_time(tmp_path, monkeypatch)
    models = Models()
    source = setup_page(tmp_path, monkeypatch, models)

    async def scenario():
        models.hold(SECOND)
        _, offer = await recognize(source)
        models.release(SECOND, error=error)
        return await collect(offer.late), offer.late.pending

    ((rebuilt, arrived, failed),), left = run(scenario())

    assert arrived == [] and failed == ["2", "3"] and left == []
    assert rebuilt["answers_pending"] == [] and rebuilt["answers_failed"] == {"2": reason, "3": reason}
    for question, complete_question in zip(rebuilt["questions"], complete["questions"]):
        if question["question_number"] in ITS_QUESTIONS:
            assert question["answer_failed"] == reason and "answer_pending" not in question
            assert question["standard_answer"] == "" and reason in question["directory_issues"]
            assert late_answers.PENDING_REASON not in question["directory_issues"]
        else:
            assert question == complete_question


def test_a_request_that_fails_before_the_page_is_delivered_still_fails_the_page(tmp_path, monkeypatch):
    models = Models()
    source = setup_page(tmp_path, monkeypatch, models)

    async def scenario():
        models.hold(LOCATE)
        models.errors[SECOND] = RuntimeError("strict_contract_failure")
        return await recognize(source)

    result, offer = run(scenario())

    assert "模板定位/解题阶段失败" in result["error"] and result["questions"] == []
    assert offer.late is None and LOCATE in models.cancelled


def test_each_late_request_updates_the_page_when_it_ends(tmp_path, monkeypatch):
    complete = on_time(tmp_path, monkeypatch, shards=3)
    models = Models()
    source = setup_page(tmp_path, monkeypatch, models, shards=3)
    plan = complete["template_ocr_diagnostics"]["solver_shard_plan"]
    assert plan == [["Q1", "Q2"], ["Q3"], ["Q4"]]

    async def scenario():
        models.hold(SOLVE + "#2", SOLVE + "#3")
        delivered, offer = await recognize(source)
        pending = list(offer.late.pending)
        updates = offer.late.updates()
        models.release(SOLVE + "#3")
        first = copy.deepcopy(await asyncio.wait_for(anext(updates), 10))
        models.release(SOLVE + "#2", error=RuntimeError("boom"))
        second = await asyncio.wait_for(anext(updates), 10)
        with pytest.raises(StopAsyncIteration):
            await anext(updates)
        return pending, first, second

    pending, (first, arrived, failed), (second, arrived_then, failed_then) = run(scenario())

    assert pending == ["3", "4"]
    assert (arrived, failed) == (["4"], []) and first["answers_pending"] == ["3"]
    assert first["questions"][3] == complete["questions"][3] and first["questions"][2]["answer_pending"] is True
    assert (arrived_then, failed_then) == ([], ["3"]) and second["answers_pending"] == []
    assert second["questions"][3] == complete["questions"][3]
    assert second["questions"][2]["answer_failed"] == late_answers.FAILED_REASON
    assert second["answers_failed"] == {"3": late_answers.FAILED_REASON}


def test_the_single_solver_request_may_be_late_for_the_whole_page(tmp_path, monkeypatch):
    complete = on_time(tmp_path, monkeypatch, shards=1)
    models = Models()
    source = setup_page(tmp_path, monkeypatch, models, shards=1)

    async def scenario():
        models.hold(SOLVE)
        delivered, offer = await recognize(source)
        snapshot = copy.deepcopy(delivered)
        models.release(SOLVE)
        return snapshot, await collect(offer.late)

    delivered, ((rebuilt, arrived, failed),) = run(scenario())

    assert delivered["answers_pending"] == ["1", "2", "3", "4"]
    assert all(question["answer_pending"] is True for question in delivered["questions"])
    assert arrived == ["1", "2", "3", "4"] and failed == []
    assert {part: rebuilt[part] for part in PARTS} == {part: complete[part] for part in PARTS}


@pytest.mark.parametrize("shards", [1, 2])
@pytest.mark.parametrize("tiled", [False, True])
def test_solver_requests_still_queue_behind_the_locators(tmp_path, monkeypatch, shards, tiled):
    models = Models()
    source = setup_page(tmp_path, monkeypatch, models, shards=shards)
    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_FALLBACK_HARD_IN_FLIGHT", 8)
    monkeypatch.setattr(config, "OCR_TEMPLATE_LOCATOR_TILE_QUESTIONS", 2 if tiled else 0)

    result, _ = run(recognize(source))

    assert "error" not in result, result
    locator = [key for key in models.started if key.startswith(LOCATE)]
    solver = [key for key in models.started if key.startswith(SOLVE)]
    assert len(solver) == shards and solver == sorted(solver) and (len(locator) > 1) is tiled
    # When permits are short the page needs its boxes first: every locator request is queued before any solver request.
    assert max(models.started.index(key) for key in locator) < models.started.index(solver[0])


def test_cancelling_late_answers_stops_the_requests_still_running(tmp_path, monkeypatch):
    models = Models()
    source = setup_page(tmp_path, monkeypatch, models)

    async def scenario():
        models.hold(SECOND)
        _, offer = await recognize(source)
        offer.late.cancel()
        return await collect(offer.late)

    (rebuilt, arrived, failed), = run(scenario())

    assert models.cancelled == [SECOND] and arrived == [] and failed == ["2", "3"]
    assert rebuilt["answers_failed"] == {"2": late_answers.FAILED_REASON, "3": late_answers.FAILED_REASON}


def test_answers_that_cannot_be_bound_to_the_delivered_page_fail_only_their_questions():
    manifest = [{"q_id": "Q1", "slots": [{"slot_id": "1[1]", "cue": "a"}]},
                {"q_id": "Q2", "slots": [{"slot_id": "2[1]", "cue": "b"}]}]
    received = {"questions": [{"question_number": "1", "slot_ids": ["1[1]"]},
                              {"question_number": "2", "slot_ids": ["2[1]", "2[2]"]}]}
    answered = lambda q_id: {"answers": [{"q_id": q_id, "standard_answer": "42", "answer_review_reasons": []}],  # noqa: E731
                             "directory_issues": [], "slot_source": owned.SHARED_SLOT_SOURCE, "max_flags": {}}
    seen = []

    def rebuild(solver):
        answers = {answer["q_id"]: answer for answer in solver["answers"]}
        seen.append({q_id: answer["standard_answer"] for q_id, answer in answers.items()})
        if answers["Q2"]["standard_answer"]:
            raise ValueError("定位与解题作答位清单不一致")
        return {"questions": [{"question_number": q_id[1:], "standard_answer": answer["standard_answer"],
                               "slots": [item["slot_id"] for item in answer.get("answer_items", [])],
                               "reasons": list(answer["answer_review_reasons"])} for q_id, answer in answers.items()]}

    async def scenario():
        gate = asyncio.Event()

        async def first():
            return answered("Q1")

        async def second():
            await asyncio.wait_for(gate.wait(), 10)
            return answered("Q2")

        shards = [asyncio.create_task(first()), asyncio.create_task(second())]
        await shards[0]
        late = late_answers.LateAnswers(shards, [["Q1"], ["Q2"]], manifest, received, rebuild)
        gate.set()
        return await collect(late)

    (rebuilt, arrived, failed), = run(scenario())

    assert arrived == [] and failed == ["2"]
    assert seen == [{"Q1": "42", "Q2": "42"}, {"Q1": "42", "Q2": ""}]
    first, second = rebuilt["questions"]
    assert first["standard_answer"] == "42" and "answer_failed" not in first
    # The blank answer covers every slot the locator found, so it binds to the page as delivered.
    assert second["slots"] == ["2[1]", "2[2]"] and second["reasons"] == [late_answers.UNBOUND_REASON]
    assert second["answer_failed"] == late_answers.UNBOUND_REASON


PAGE_DEADLINE_S = 3.0


def _real_request_page(tmp_path, monkeypatch):
    """The real ``_request`` under a page deadline of ``PAGE_DEADLINE_S``.

    The second solver shard answers only when released: ``recognized(accept, release=True)``
    releases it once the page deadline has passed, ``release=False`` never does.
    """
    monkeypatch.setattr(config, "OCR_TEMPLATE_SOLVER_SHARDS", 2)
    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_FALLBACK_HARD_IN_FLIGHT", 4)
    monkeypatch.setattr(config, "OCR_TEMPLATE_LOCATOR_TILE_QUESTIONS", 0)
    monkeypatch.setattr(late_answers, "DELIVERY_WAIT_S", 0.0)
    source = tmp_path / "paper.png"
    Image.new("RGB", (600, 800), "white").save(source)
    raw_hierarchy, payloads = v4_payloads((2, 2, 2, 2))

    async def flash(messages, **kwargs):
        prompt = messages[0]["content"][0]["text"]
        payload = (raw_hierarchy if prompt == pipeline._HIERARCHY_PROMPT
                   else coarse_wire(["1", "2", "3", "4"]) if prompt == pipeline._COARSE_LOCATOR_PROMPT
                   else payloads[contract.TRANSCRIBE_PHASE])
        raw = json.dumps(payload, ensure_ascii=False)
        kwargs["response_validator"](raw)
        return {"content": raw, "model": "qwen3.8-flash", "receipt": {}}

    gates = []

    async def max_call(images, prompt, **kwargs):
        if "本请求只负责以下题目：Q2、Q3" in prompt:
            await asyncio.wait_for(gates[-1].wait(), 20)
        raw = json.dumps(payloads[LOCATE if "normalized_1000_v1" in prompt else SOLVE], ensure_ascii=False)
        kwargs["response_validator"](raw)
        return raw

    monkeypatch.setattr(flash_grading, "call_flash", flash)
    monkeypatch.setattr(pipeline, "call_gpt_vision", max_call)

    async def recognized(accept, release=True):
        loop = asyncio.get_running_loop()
        gates.append(asyncio.Event())
        deadline = loop.time() + PAGE_DEADLINE_S
        with use_teaching_context(make_teaching_context("senior")):
            with pipeline.template_import_context(deadline, {"stages": {}}):
                if not accept:
                    return await pipeline.recognize_directory_template(
                        str(source), template_mode="blank", layout_result={}), None
                with late_answers.accepting() as offer:
                    result = await pipeline.recognize_directory_template(
                        str(source), template_mode="blank", layout_result={})
            delivered_before_deadline = loop.time() < deadline
            if release:
                await asyncio.sleep(deadline + 0.3 - loop.time())
                gates[-1].set()
            return result, delivered_before_deadline, [update async for update in offer.late.updates()]

    return recognized


def test_a_late_request_is_not_stopped_by_its_pages_deadline(tmp_path, monkeypatch):
    recognized = _real_request_page(tmp_path, monkeypatch)

    waited, _ = run(recognized(False))
    delivered, before_deadline, ((rebuilt, arrived, failed),) = run(recognized(True))

    # Waiting for it, the page runs out of time. Delivered without it, the request is
    # still alive after that deadline and its answer is taken.
    assert "模板定位/解题阶段失败" in waited["error"]
    assert "error" not in delivered and before_deadline and delivered["answers_pending"] == ["2", "3"]
    assert arrived == ["2", "3"] and failed == [] and rebuilt["questions"][1]["standard_answer"] != ""


def test_a_late_request_has_a_limit_of_its_own(tmp_path, monkeypatch):
    monkeypatch.setattr(late_answers, "SOLVER_LIMIT_S", PAGE_DEADLINE_S + 1.0)
    recognized = _real_request_page(tmp_path, monkeypatch)

    delivered, before_deadline, ((rebuilt, arrived, failed),) = run(recognized(True, release=False))

    assert before_deadline and delivered["answers_pending"] == ["2", "3"] and arrived == [] and failed == ["2", "3"]
    assert rebuilt["answers_failed"] == {"2": late_answers.TIMED_OUT_REASON, "3": late_answers.TIMED_OUT_REASON}
