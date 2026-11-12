"""Standard answers that arrive after their page was delivered reach the draft, and block confirmation until then.

The import and every editing route rewrite the template's ``meta.json`` whole,
so late answers are kept in a ledger beside it and laid over the saved pages
when the draft is read. A template is not confirmed while an answer is still
being generated; an answer that can no longer arrive reads as failed.
"""
import asyncio
import copy
import json
import time
from pathlib import Path

import httpx
import pytest

from routers import grade_availability
from routers import grade_recognition
from routers import grade_template_availability
from services import template_answer_ledger as answer_ledger
from services import template_late_answers as late_answers
from services.grading_contract import AnswerSpec
from services.template_page_numbering import globalize_page_numbers
from services.template_session import TemplateSession
from tests.grade_family import set_grade_global
from tests.test_template_late_answers import (
    ITS_QUESTIONS,
    Models,
    SECOND,
    SOLVE,
    on_time,
    recognize,
    run,
    setup_page,
)
from tests.test_template_session import (  # noqa: F401 - autouse readiness fixture
    _client,
    _directory_confirmation_session,
    _make_app_and_session,
    _parse_sse,
    _patch_template_layout,
    _ready_batch_grade_chain,
    _template_png_bytes,
)

FAR = time.time() + 3600


def question(number, node, *, answer="", slots=("",), **extra) -> dict:
    """A saved question of a draft: ``slots`` are its answer slots in order."""
    return {"question_no": number, "directory_node_id": node, "standard_answer": answer,
            "answer_slots": list(slots), "slot_ids": [f"{number}[{index}]" for index in range(1, len(slots) + 1)],
            **extra}


def draft(*questions, page_index=0) -> list:
    return [{"page_index": page_index, "questions": list(questions)}]


def arrived(number="3", **extra) -> dict:
    """What a late answer brings for a two-slot question that was number ``number`` when delivered."""
    return {"standard_answer": "AD=√3", "answer_slots": ["平行", "√3"], "slot_ids": [f"{number}[1]", f"{number}[2]"],
            "solution_steps": ["由勾股定理得 BC⊥AB"], "answer_variants": [], **extra}


def test_a_draft_without_late_answers_is_read_as_saved(tmp_path):
    pages = draft(question("1", "p1-n001", answer="2", slots=("2",)), question("2", "p1-n002", slots=("", "")))

    read = answer_ledger.overlay(tmp_path, pages)

    assert read == pages and read is not pages and read[0]["questions"][0] is not pages[0]["questions"][0]
    assert answer_ledger.pending(tmp_path, pages) == []
    assert not (tmp_path / "late_answers.json").exists()


def test_a_question_being_solved_is_marked_until_its_answer_arrives(tmp_path):
    pages = draft(question("3", "p1-n003", slots=("", "")), question("4", "p1-n004", answer="5", slots=("5",)),
                  page_index=2)
    saved = copy.deepcopy(pages)
    answer_ledger.begin(tmp_path, ["p1-n003"], deadline_at=FAR)

    waiting = answer_ledger.overlay(tmp_path, pages)
    assert waiting[0]["questions"][0] == {**pages[0]["questions"][0], "answer_pending": True}
    assert waiting[0]["questions"][1] == pages[0]["questions"][1]
    assert answer_ledger.pending(tmp_path, pages) == [(2, "3")]

    answer_ledger.record(tmp_path, {"p1-n003": arrived()}, {})
    filled = answer_ledger.overlay(tmp_path, pages)
    first = filled[0]["questions"][0]
    assert first["standard_answer"] == "AD=√3" and first["answer_slots"] == ["平行", "√3"]
    assert first["solution_steps"] == ["由勾股定理得 BC⊥AB"] and first["slot_ids"] == ["3[1]", "3[2]"]
    assert "answer_pending" not in first and "answer_failed" not in first
    assert answer_ledger.pending(tmp_path, pages) == []
    # Reading never changes what was saved.
    assert pages == saved


@pytest.mark.parametrize("failure, reason", [
    pytest.param({"p1-n003": late_answers.FAILED_REASON}, late_answers.FAILED_REASON, id="the_request_failed"),
    pytest.param({"p1-n003": late_answers.TIMED_OUT_REASON}, late_answers.TIMED_OUT_REASON, id="the_request_timed_out"),
    pytest.param(None, late_answers.TIMED_OUT_REASON, id="nobody_recorded_an_outcome_before_the_deadline"),
])
def test_an_answer_that_will_not_arrive_reads_as_failed_never_as_pending(tmp_path, failure, reason):
    pages = draft(question("3", "p1-n003", slots=("", "")))
    answer_ledger.begin(tmp_path, ["p1-n003"], deadline_at=1000.0)
    if failure:
        answer_ledger.record(tmp_path, {}, failure)

    read = answer_ledger.overlay(tmp_path, pages, now=2000.0)

    assert read[0]["questions"][0] == {**pages[0]["questions"][0], "answer_failed": reason}
    assert answer_ledger.pending(tmp_path, pages, now=2000.0) == []
    # Before the deadline a question without an outcome is still being solved.
    assert answer_ledger.pending(tmp_path, pages, now=999.0) == ([] if failure else [(0, "3")])


def test_an_answer_the_draft_already_has_is_never_replaced(tmp_path):
    typed = draft(question("3", "p1-n003", answer="老师填写的答案", slots=("甲", "")),
                  question("4", "p1-n004", slots=("只填了一个作答位", "")))
    answer_ledger.begin(tmp_path, ["p1-n003", "p1-n004"], deadline_at=FAR)

    assert answer_ledger.overlay(tmp_path, typed) == typed and answer_ledger.pending(tmp_path, typed) == []
    answer_ledger.record(tmp_path, {"p1-n003": arrived(), "p1-n004": arrived("4")}, {})
    assert answer_ledger.overlay(tmp_path, typed) == typed


def test_a_late_answer_follows_its_question_through_renumbering_and_slot_edits(tmp_path):
    answer_ledger.record(tmp_path, {"p1-n003": arrived("3", max_flags=[{"scope": "question", "status": "extra"}],
                                                       solve_visual_context={"facts": ["PA=AC=2"]})}, {})

    def filled(saved_question):
        return answer_ledger.overlay(tmp_path, draft(saved_question))[0]["questions"][0]

    # The teacher inserted a question before it: it is number 4 now, its slots are 4[1] and 4[2].
    renumbered = filled(question("4", "p1-n003", slots=("", "")))
    assert renumbered["answer_slots"] == ["平行", "√3"] and renumbered["slot_ids"] == ["4[1]", "4[2]"]
    assert renumbered["max_flags"] == [{"scope": "question", "status": "extra"}]
    assert renumbered["solve_visual_context"] == {"facts": ["PA=AC=2"]}
    # A slot was added, another removed: each answer stays with its own place in the question.
    assert filled(question("3", "p1-n003", slots=("", "", "")))["answer_slots"] == ["平行", "√3", ""]
    assert filled({**question("3", "p1-n003", slots=("",)), "slot_ids": ["3[2]"]})["answer_slots"] == ["√3"]
    # Without slot ids the answers go by position when the count is the same, and are left out otherwise.
    unkeyed = {key: value for key, value in question("3", "p1-n003", slots=("", "")).items() if key != "slot_ids"}
    assert filled(unkeyed)["answer_slots"] == ["平行", "√3"]
    assert filled({**unkeyed, "answer_slots": ["", "", ""]})["answer_slots"] == ["", "", ""]
    # A question that was deleted, or one that never waited, is none of the ledger's business.
    other = question("5", "p1-n009", slots=("",))
    assert filled(other) == other


def test_what_a_late_answer_does_not_bring_is_not_left_over_from_the_blank(tmp_path):
    blank = question("3", "p1-n003", slots=("", ""), solve_visual_context={"facts": [], "relations": [], "uncertain": []},
                     solution_steps=[], answer_variants=[])
    answer_ledger.record(tmp_path, {"p1-n003": arrived()}, {})

    read = answer_ledger.overlay(tmp_path, draft(blank))[0]["questions"][0]

    assert "solve_visual_context" not in read and "max_flags" not in read
    assert read["solution_steps"] == ["由勾股定理得 BC⊥AB"]


def test_forgetting_a_template_drops_its_ledger(tmp_path):
    pages = draft(question("3", "p1-n003", slots=("", "")))
    answer_ledger.begin(tmp_path, ["p1-n003"], deadline_at=FAR)
    assert answer_ledger.pending(tmp_path, pages) == [(0, "3")]

    answer_ledger.forget(tmp_path)
    answer_ledger.forget(tmp_path)

    assert answer_ledger.overlay(tmp_path, pages) == pages and not (tmp_path / "late_answers.json").exists()


def stored(result, *, offset=10, page_index=1):
    """A page result as the import stores it: its renumbered questions, and the page of the saved draft."""
    page = globalize_page_numbers(copy.deepcopy(result), offset, page_index=page_index)
    return page["questions"], [{"page_index": page_index,
                                "questions": [AnswerSpec.model_validate(item).model_dump() for item in page["questions"]]}]


async def settled(template_dir, pages) -> None:
    """Wait, bounded, until nothing of this draft is being solved any more."""
    async def idle():
        while answer_ledger.pending(template_dir, pages):
            await asyncio.sleep(0.02)
    await asyncio.wait_for(idle(), 10)


def reporting() -> Models:
    """Models whose answer to question 2 reports its first slot as not an answer position."""
    models = Models()
    models.branches[SOLVE]["answers"][1]["answer_items"][0].update(
        status="extra", answer="", reason="该处是印刷示意图，没有新增作答要求")
    return models


def test_a_late_answer_reaches_the_draft_exactly_as_it_would_have_been_saved_on_time(tmp_path, monkeypatch):
    _, complete = stored(on_time(tmp_path, monkeypatch, models=reporting()))
    models = reporting()
    source = setup_page(tmp_path, monkeypatch, models)
    template_dir = tmp_path / "template"
    template_dir.mkdir()

    async def scenario():
        models.hold(SECOND)
        delivered, offer = await recognize(source)
        questions, pages = stored(delivered)
        await answer_ledger.follow_page(template_dir, 1, questions, offer.late)
        while_solving = answer_ledger.overlay(template_dir, pages), answer_ledger.pending(template_dir, pages)
        models.release(SECOND)
        await settled(template_dir, pages)
        return pages, while_solving, answer_ledger.overlay(template_dir, pages)

    pages, (while_solving, waiting), filled = run(scenario())

    assert waiting == [(1, "12"), (1, "13")]
    for read, saved, on_time_question in zip(while_solving[0]["questions"], pages[0]["questions"], complete[0]["questions"]):
        if saved["page_local_number"] in ITS_QUESTIONS:
            assert read == {**saved, "answer_pending": True} and saved["standard_answer"] == ""
        else:
            assert read == saved == on_time_question
    assert filled == complete
    # What the solver reported about a slot arrives under the number the draft knows the slot by.
    (flag,) = filled[0]["questions"][1]["max_flags"]
    assert flag["slot_id"] == "12[1]" and flag["status"] == "extra" and flag["source"] == "solver"


def test_a_late_request_that_fails_leaves_its_questions_marked_failed(tmp_path, monkeypatch):
    models = Models()
    source = setup_page(tmp_path, monkeypatch, models)

    async def scenario():
        models.hold(SECOND)
        delivered, offer = await recognize(source)
        questions, pages = stored(delivered)
        await answer_ledger.follow_page(tmp_path, 1, questions, offer.late)
        models.release(SECOND, error=TimeoutError("budget"))
        await settled(tmp_path, pages)
        return pages, answer_ledger.overlay(tmp_path, pages)

    pages, read = run(scenario())

    for shown, saved in zip(read[0]["questions"], pages[0]["questions"]):
        expected = ({**saved, "answer_failed": late_answers.TIMED_OUT_REASON}
                    if saved["page_local_number"] in ITS_QUESTIONS else saved)
        assert shown == expected


class ScriptedLate:
    """A delivered page's late answers whose follow-up is scripted by the test."""

    pending = ["2", "3"]

    def __init__(self, error=None, steps=()):
        self.error, self.steps, self.gate, self.cancelled = error, list(steps), asyncio.Event(), 0

    async def updates(self):
        await asyncio.wait_for(self.gate.wait(), 10)
        for step in self.steps:
            yield step
        if self.error:
            raise self.error

    def cancel(self):
        self.cancelled += 1


def delivered_questions():
    return [{"question_no": str(10 + local), "page_local_number": str(local), "directory_node_id": f"p2-n00{local}",
             "standard_answer": "", "answer_slots": [""], "slot_ids": [f"{10 + local}[1]"]} for local in (1, 2, 3)]


def test_if_following_breaks_nothing_is_left_pending(tmp_path):
    pages = [{"page_index": 1, "questions": delivered_questions()}]

    async def scenario():
        late = ScriptedLate(RuntimeError("the follow-up broke"))
        await answer_ledger.follow_page(tmp_path, 1, pages[0]["questions"], late)
        waiting = answer_ledger.pending(tmp_path, pages)
        late.gate.set()
        await settled(tmp_path, pages)
        return waiting, late.cancelled

    waiting, cancelled = run(scenario())

    assert waiting == [(1, "12"), (1, "13")] and cancelled == 1
    read = answer_ledger.overlay(tmp_path, pages)[0]["questions"]
    assert [item.get("answer_failed") for item in read] == [None, late_answers.FAILED_REASON, late_answers.FAILED_REASON]


def test_an_answer_the_draft_cannot_hold_fails_its_own_question_only(tmp_path):
    pages = [{"page_index": 1, "questions": delivered_questions()}]

    def rebuilt_question(local, **answer):
        return {"question_number": str(local), "question_no": str(local), "q_id": f"Q{local}",
                "question_text": f"第{local}题", "question_type": "solve", "standard_answer": "", "answer_slots": [""],
                "slot_ids": [f"{local}[1]"], **answer}

    # Question 3 comes back with two answer positions' ids and one answer: no saved question can hold that.
    rebuilt = {"answers_failed": {}, "questions": [
        rebuilt_question(1, standard_answer="1"),
        rebuilt_question(2, standard_answer="x=2", answer_slots=["x=2"]),
        rebuilt_question(3, standard_answer="y=3", answer_slots=["y=3"], slot_ids=["3[1]", "3[2]"])]}

    async def scenario():
        late = ScriptedLate(steps=[(rebuilt, ["2", "3"], [])])
        await answer_ledger.follow_page(tmp_path, 1, pages[0]["questions"], late)
        late.gate.set()
        await settled(tmp_path, pages)
        return late.cancelled

    assert run(scenario()) == 0
    first, second, third = answer_ledger.overlay(tmp_path, pages)[0]["questions"]
    assert first == pages[0]["questions"][0]
    assert second["standard_answer"] == "x=2" and second["answer_slots"] == ["x=2"] and "answer_failed" not in second
    assert third["standard_answer"] == "" and third["answer_failed"] == late_answers.UNBOUND_REASON


def test_forgetting_a_template_stops_following_it_and_leaves_no_ledger_behind(tmp_path):
    pages = [{"page_index": 1, "questions": delivered_questions()}]

    async def scenario():
        late = ScriptedLate()
        await answer_ledger.follow_page(tmp_path, 1, pages[0]["questions"], late)
        answer_ledger.forget(tmp_path)
        for _ in range(20):
            await asyncio.sleep(0.01)
        return late.cancelled

    assert run(scenario()) == 1
    assert not (tmp_path / "late_answers.json").exists() and answer_ledger.overlay(tmp_path, pages) == pages


def gate_draft(monkeypatch, tmp_path):
    """A saved one-question draft whose answer the model did not deliver, and the payload a teacher would post."""
    client, sess, payload = _directory_confirmation_session(monkeypatch, tmp_path, answer="")
    payload["pages"][0]["questions"][0]["standard_answer"] = "老师自己填的答案"
    return client, sess, payload


def test_a_draft_is_not_confirmed_while_an_answer_is_being_generated(monkeypatch, tmp_path):
    client, sess, payload = gate_draft(monkeypatch, tmp_path)
    answer_ledger.begin(sess.template_dir, ["n001"], deadline_at=FAR)
    before = Path(sess.meta_path).read_bytes()

    # Even with an answer typed in: the generated one is on its way.
    blocked = client.post("/api/batch-grade/template/confirm", json=payload)

    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"] == "还有 1 道题的标准答案正在生成（第 1 页第 1 题），生成完成后才能保存模板"
    assert Path(sess.meta_path).read_bytes() == before
    assert TemplateSession.load_meta(sess.template_id, str(tmp_path)).status == "ocr_done"
    state = client.get(f"/api/batch-grade/template/{sess.template_id}/recognition").json()
    assert state["status"] == "ocr_done" and state["answers_pending"] == 1
    assert state["pages"][0]["questions"][0]["answer_pending"] is True

    answer_ledger.record(sess.template_dir, {"n001": {"standard_answer": "x=2", "answer_slots": ["x=2"],
                                                       "solution_steps": ["移项得 x=2"]}}, {})
    state = client.get(f"/api/batch-grade/template/{sess.template_id}/recognition").json()
    shown = state["pages"][0]["questions"][0]
    assert state["answers_pending"] == 0 and shown["standard_answer"] == "x=2" and "answer_pending" not in shown
    # The saved draft itself is untouched: the answer is laid over it when it is read.
    assert Path(sess.meta_path).read_bytes() == before

    payload["pages"][0]["questions"][0]["standard_answer"] = shown["standard_answer"]
    accepted = client.post("/api/batch-grade/template/confirm", json=payload)
    assert accepted.status_code == 200, accepted.text
    confirmed = TemplateSession.load_meta(sess.template_id, str(tmp_path))
    assert confirmed.status == "confirmed" and confirmed.pages[0]["questions"][0]["standard_answer"] == "x=2"
    assert not (Path(sess.template_dir) / "late_answers.json").exists()


@pytest.mark.parametrize("outcome", ["failed", "deadline_passed"])
def test_a_draft_whose_answer_will_not_arrive_is_confirmed_once_the_teacher_gives_one(monkeypatch, tmp_path, outcome):
    client, sess, payload = gate_draft(monkeypatch, tmp_path)
    answer_ledger.begin(sess.template_dir, ["n001"], deadline_at=FAR if outcome == "failed" else time.time() - 1)
    if outcome == "failed":
        answer_ledger.record(sess.template_dir, {}, {"n001": late_answers.FAILED_REASON})

    state = client.get(f"/api/batch-grade/template/{sess.template_id}/recognition").json()
    reason = late_answers.FAILED_REASON if outcome == "failed" else late_answers.TIMED_OUT_REASON
    assert state["answers_pending"] == 0 and state["pages"][0]["questions"][0]["answer_failed"] == reason
    without_answer = copy.deepcopy(payload)
    without_answer["pages"][0]["questions"][0]["standard_answer"] = ""
    assert client.post("/api/batch-grade/template/confirm", json=without_answer).status_code == 422

    accepted = client.post("/api/batch-grade/template/confirm", json=payload)

    assert accepted.status_code == 200, accepted.text
    saved = TemplateSession.load_meta(sess.template_id, str(tmp_path)).pages[0]["questions"][0]
    assert saved["standard_answer"] == "老师自己填的答案" and "answer_failed" not in saved


def test_a_report_that_arrives_with_a_late_answer_blocks_confirmation_like_one_that_was_in_time(monkeypatch, tmp_path):
    client, sess, payload = gate_draft(monkeypatch, tmp_path)
    flag = {"scope": "question", "status": "extra", "reason": "该编号不是独立题目", "source": "solver"}
    answer_ledger.record(sess.template_dir, {"n001": {"standard_answer": "", "answer_slots": [""], "max_flags": [flag]}}, {})

    shown = client.get(f"/api/batch-grade/template/{sess.template_id}/recognition").json()["pages"][0]["questions"][0]
    blocked = client.post("/api/batch-grade/template/confirm", json=payload)

    assert shown["max_flags"] == [flag]
    assert blocked.status_code == 422 and "目录多余" in blocked.json()["detail"], blocked.text
    payload["pages"][0]["questions"][0]["max_flags_kept"] = ["*"]
    assert client.post("/api/batch-grade/template/confirm", json=payload).status_code == 200


def test_a_confirmed_template_is_read_as_confirmed_whatever_a_ledger_says(monkeypatch, tmp_path):
    client, sess, payload = gate_draft(monkeypatch, tmp_path)
    assert client.post("/api/batch-grade/template/confirm", json=payload).status_code == 200
    answer_ledger.begin(sess.template_dir, ["n001"], deadline_at=FAR)

    state = client.get(f"/api/batch-grade/template/{sess.template_id}/recognition").json()

    assert state["status"] == "confirmed" and state["answers_pending"] == 0
    assert state["pages"][0]["questions"][0]["standard_answer"] == "老师自己填的答案"
    assert client.post("/api/batch-grade/template/confirm", json=payload).status_code == 200


async def _import_one_page(monkeypatch, tmp_path, models, scenario):
    """Run ``scenario(client, importing)`` against the real import route and the real page pipeline."""
    import routers.grade as grade

    _, factory = _make_app_and_session()
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda *_args: None)
    _patch_template_layout(monkeypatch, grade)

    async def available(*args, **kwargs):
        return {"status": "available", "message": "synthetic readiness"}

    async def hardware_ready(*args, **kwargs):
        return {"status": "ready", "message": "synthetic hardware"}

    monkeypatch.setattr(grade_availability, "batch_grade_availability", available)
    monkeypatch.setattr(grade_availability, "_batch_grade_service_availability", available)
    monkeypatch.setattr(grade_template_availability, "_batch_grade_service_availability", available)
    monkeypatch.setattr(grade_template_availability, "get_column_layout_readiness", available)
    monkeypatch.setattr(grade_availability, "get_public_hardware_readiness", hardware_ready)
    monkeypatch.setattr(grade_template_availability, "get_public_hardware_readiness", hardware_ready)
    setup_page(tmp_path, monkeypatch, models)
    app = _client(factory, monkeypatch).app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        importing = asyncio.create_task(client.post(
            "/api/batch-grade/template/recognize", data={"template_mode": "blank", "category": "daily"},
            files=[("files", ("page.png", _template_png_bytes(), "image/png"))]))
        return await asyncio.wait_for(scenario(client, importing), 60)


def _done(response) -> dict:
    events = _parse_sse(response.text)
    assert response.status_code == 200 and not [payload for event, payload in events if event == "error"], events
    return next(payload for event, payload in events if event == "done")


@pytest.mark.asyncio
async def test_the_import_delivers_the_page_and_the_answer_follows_into_the_draft(tmp_path, monkeypatch):
    (tmp_path / "in-time").mkdir()
    (tmp_path / "late").mkdir()

    async def in_time(client, importing):
        return _done(await importing)["pages"]

    complete = await _import_one_page(monkeypatch, tmp_path / "in-time", Models(), in_time)
    models = Models()

    async def scenario(client, importing):
        models.hold(SECOND)
        done = _done(await importing)
        template_id = done["template_id"]
        meta = Path(TemplateSession.load_meta(template_id, str(tmp_path / "late")).meta_path)
        saved = meta.read_bytes()
        state = (await client.get(f"/api/batch-grade/template/{template_id}/recognition")).json()
        # A teacher who typed answers into the questions still being solved and saved anyway.
        typed = copy.deepcopy(state["pages"])
        for item in typed[0]["questions"]:
            if item.get("answer_pending"):
                item.update(standard_answer="先填一个", answer_slots=["先填一个"] * len(item["answer_slots"]))
        blocked = await client.post("/api/batch-grade/template/confirm", json={
            "template_id": template_id, "category": "daily", "pages": typed})
        models.release(SECOND)

        async def arrived():
            while True:
                later = (await client.get(f"/api/batch-grade/template/{template_id}/recognition")).json()
                if not later["answers_pending"]:
                    return later
                await asyncio.sleep(0.02)

        return done, state, blocked, await asyncio.wait_for(arrived(), 10), saved, meta.read_bytes()

    done, state, blocked, later, saved, saved_later = await _import_one_page(
        monkeypatch, tmp_path / "late", models, scenario)

    # The import finished without the slow request: its questions are in the draft, marked.
    assert state["status"] == "ocr_done" and state["answers_pending"] == 2 and done["pages"] == state["pages"]
    for shown, on_time_question in zip(state["pages"][0]["questions"], complete[0]["questions"]):
        if shown["question_no"] in ITS_QUESTIONS:
            assert shown["answer_pending"] is True and shown["standard_answer"] == ""
        else:
            assert shown == on_time_question
    assert blocked.status_code == 409 and "正在生成" in blocked.json()["detail"], blocked.text
    # When the request ends, the draft reads exactly as if every answer had been in time.
    assert later["pages"] == complete and saved_later == saved
