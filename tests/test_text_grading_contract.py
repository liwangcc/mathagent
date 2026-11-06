"""Approved text-grading rules; preserve upstream OCR and frozen-slot scope."""
import asyncio
import copy
import json

import pytest

from services import analyzer, flash_grading as flash, grader
from services.text_grading_rules import TEXT_GRADING_RULES
from services.grading_contract import AnswerSpec
from services.scoring_engine import build_grading_units, llm_payload_for_unit


def question():
    return {
        "question_no": "2[8]", "question_text": "保留完整公共题干。",
        "student_answer": "6", "standard_answer": "6",
        "solution_steps": ["共享参考步骤一", "共享参考步骤二"],
    }


def verdict(result="correct"):
    return {"result": result, "explanation": "model explanation", "hint": "",
            "model_answer": "", "standard_answer": ""}


def test_text_routes_share_one_semantic_rule_body():
    assert TEXT_GRADING_RULES == flash._TEXT_RULES
    rules = flash._TEXT_RULES
    assert "同一所求答案的多步解答" in rules
    assert "学生写出的最终结果正确" in rules
    assert "只有中间极少量孤立的数字或符号偏差" in rules
    assert "needs_review" not in rules
    assert "最多" not in rules
    assert "明确错误判wrong" not in rules


@pytest.mark.parametrize("detail", ["simple", "detailed"])
def test_all_model_prompt_forms_have_three_results_and_preserve_reference_data(detail):
    q = question()
    before = copy.deepcopy(q)
    messages = flash.build_text_messages([q], detail)
    prompts = [messages[0]["content"]]
    assert all("needs_review" not in p for p in prompts)
    assert all("correct" in p and "partial" in p and "wrong" in p for p in prompts)
    refs = json.loads(messages[0]["content"].split("教师确认资料：", 1)[1])
    assert refs[0]["question_text"] == q["question_text"]
    assert refs[0]["solution_steps"] == q["solution_steps"]
    assert json.loads(messages[1]["content"])["answers"] == [
        {"question_no": "2[8]", "student_answer": "6"}]
    assert q == before


@pytest.mark.parametrize("detail", ["simple", "detailed"])
def test_flash_schema_does_not_offer_model_abstention(detail):
    schema = flash.build_text_response_schema(["2[8]"], detail)
    values = schema["properties"]["results"]["properties"]["2[8]"]["properties"]["result"]["enum"]
    assert values == ["correct", "partial", "wrong"]


@pytest.mark.parametrize("route", ["flash", "legacy"])
@pytest.mark.parametrize("value", ["needs_review", "unknown", None])
def test_model_receivers_reject_non_grading_states(route, value):
    item = verdict(value)
    with pytest.raises(analyzer.StructuredOutputError):
        if route == "flash":
            flash.parse_text_judgment(json.dumps({"results": {"2[8]": item}}), ["2[8]"])
        else:
            analyzer._validate_paper_judgment(json.dumps({
                "results": [{"question_no": "2[8]", **item}]}), ["2[8]"])


@pytest.mark.parametrize("value", ["needs_review", None, [], "unknown"])
def test_normalizer_cannot_turn_invalid_model_results_into_student_verdicts(value):
    with pytest.raises(analyzer.StructuredOutputError):
        analyzer._normalize_judged(verdict(value))


@pytest.mark.parametrize("route", ["flash", "legacy"])
@pytest.mark.parametrize("value", ["correct", "partial", "wrong"])
def test_valid_model_verdicts_are_not_regraded(route, value):
    item = verdict(value)
    if route == "flash":
        result = flash.parse_text_judgment(json.dumps({"results": {"2[8]": item}}), ["2[8]"])
    else:
        result = analyzer._validate_paper_judgment(json.dumps({
            "results": [{"question_no": "2[8]", **item}]}), ["2[8]"])
    assert result["2[8]"]["result"] == value


@pytest.mark.parametrize("method", [None, "", "   "])
def test_missing_method_stays_unspecified_from_answer_spec_to_both_prompts(method):
    spec = AnswerSpec(question_no="2", question_type="solve", question_text="直接填写结果。",
                      standard_answer="6", grading_method=method)
    entry = spec.to_answer_map_entry()
    assert spec.grading_method == ""
    snapshot = copy.deepcopy(entry)
    unit = build_grading_units([{"question_no": "2", "student_answer": "6"}],
                               {"2": entry}, "daily")[0]
    payload = llm_payload_for_unit(unit)
    assert not payload.get("grading_method")
    refs = json.loads(flash.build_text_messages([payload])[0]["content"].split("教师确认资料：", 1)[1])
    assert not refs[0].get("grading_method")
    assert entry == snapshot


def test_raw_answer_map_does_not_synthesize_a_process_requirement():
    entry = {"question_type": "solve", "question_text": "直接填写结果。", "standard_answer": "6"}
    unit = build_grading_units([{"question_no": "2", "student_answer": "6"}], {"2": entry}, "daily")[0]
    assert not unit.get("grading_method")
    assert not llm_payload_for_unit(unit).get("grading_method")
    assert "grading_method" not in entry


@pytest.mark.parametrize("method", ["process", "equivalent", "exact"])
def test_explicit_confirmed_method_survives_roundtrip_and_wire(method):
    spec = AnswerSpec(question_no="2", question_type="solve", question_text="按题意作答。",
                      standard_answer="6", grading_method=method)
    reopened = AnswerSpec.model_validate_json(spec.model_dump_json())
    entry = reopened.to_answer_map_entry()
    snapshot = copy.deepcopy(entry)
    unit = build_grading_units([{"question_no": "2", "student_answer": "6"}], {"2": entry}, "daily")[0]
    payload = llm_payload_for_unit(unit)
    refs = json.loads(flash.build_text_messages([payload])[0]["content"].split("教师确认资料：", 1)[1])
    assert payload["grading_method"] == refs[0]["grading_method"] == method
    assert entry == snapshot


def test_one_model_abstention_rejects_entire_batch_without_partial_success(monkeypatch):
    questions = [question(), {**question(), "question_no": "4[1]"}]
    ids = [q["question_no"] for q in questions]
    items = {ids[0]: verdict("needs_review"), ids[1]: verdict("correct")}
    calls = []

    async def transport(messages, **kwargs):
        calls.append(messages)
        return {"content": json.dumps({"results": items}), "receipt": {},
                "model": flash.FLASH_MODEL, "parameters": {}, "usage": {}}

    monkeypatch.setattr(flash, "call_flash", transport)
    result = asyncio.run(analyzer.judge_paper(questions))
    assert len(calls) == 1
    assert getattr(result, "__task_queue_cacheable__", True) is False
    assert all(item["result"] != "correct" for item in result.values())
    assert all(item["explanation"] != "model explanation" for item in result.values())


def test_single_answer_model_abstention_is_rejected_not_accepted(monkeypatch):
    async def transport(messages, **kwargs):
        return {"content": json.dumps({"results": {"1": verdict("needs_review")}}), "receipt": {},
                "model": flash.FLASH_MODEL, "parameters": {}, "usage": {}}
    monkeypatch.setattr(flash, "call_flash", transport)
    result = asyncio.run(analyzer.judge_answer("题干", "转录", "参考"))
    assert result["explanation"] != "model explanation"


class ImmediateQueue:
    async def submit(self, coro_factory, **kwargs):
        return await coro_factory()


def test_item_three_keeps_full_references_sparse_dispatch_and_blank_handling(monkeypatch):
    ids = [f"2[{i}]" for i in range(1, 9)]
    refs = [str(i) for i in range(1, 9)]
    steps = [f"共享参考步骤{i}" for i in range(1, 9)]
    spec = AnswerSpec(question_no="2", question_type="solve", question_text="完整八项题干不得裁剪。",
                      blank_count=8, slot_ids=ids, answer_slots=refs, standard_answer="；".join(refs),
                      solution_steps=steps, region_kinds=["answer"] * 8)
    entry = spec.to_answer_map_entry()
    snapshot = copy.deepcopy(entry)
    slots = [{"slot_id": qn, "student_answer": "8" if qn == "2[8]" else "",
              "answer_state": "visible" if qn == "2[8]" else "blank"} for qn in ids]
    captured = []
    async def judge(questions, **kwargs):
        captured.extend(copy.deepcopy(questions))
        return {q["question_no"]: verdict() for q in questions}
    monkeypatch.setattr(grader, "get_grading_queue", ImmediateQueue)
    monkeypatch.setattr(grader, "judge_paper", judge)
    result = asyncio.run(grader.grade_paper_unified(
        {"questions": [{"question_no": "2", "student_slots": slots}]}, {"2": entry}, "synthetic", 1))
    assert [q["question_no"] for q in captured] == ["2[8]"]
    assert captured[0]["question_text"].startswith(spec.question_text)
    assert captured[0]["solution_steps"] == steps
    assert captured[0]["standard_answer"] == "8"
    assert [d["slot_id"] for d in result["details"]] == ids
    assert [d["result"] for d in result["details"]] == ["wrong"] * 7 + ["correct"]
    assert entry == snapshot


def test_upstream_unreadable_still_bypasses_text_model(monkeypatch):
    entry = AnswerSpec(question_no="2", question_type="solve", standard_answer="6",
                       blank_count=1, slot_ids=["2[8]"], answer_slots=["6"]).to_answer_map_entry()
    async def forbidden(*args, **kwargs):
        raise AssertionError("OCR unreadable input must not reach the grading model")
    monkeypatch.setattr(grader, "judge_paper", forbidden)
    result = asyncio.run(grader.grade_paper_unified({"questions": [{"question_no": "2", "student_slots": [
        {"slot_id": "2[8]", "student_answer": "[无法识别]", "answer_state": "unreadable"}]}]},
        {"2": entry}, "synthetic", 1))
    assert result["needs_review_count"] == 1
    assert result["details"][0]["error_type"] == "ocr_unreadable"


def test_schema_and_mixed_question_scope_follow_the_same_contract():
    assert analyzer._JudgedQuestionOutput.model_json_schema()["properties"]["result"]["enum"] == [
        "correct", "partial", "wrong"]
    q = {**question(), "scope_hint": "text_only"}
    flash_refs = json.loads(flash.build_text_messages([q])[0]["content"].split("教师确认资料：", 1)[1])
    assert flash_refs[0]["scope_hint"] == "text_only"


@pytest.mark.parametrize("method", ["", "process"])
def test_method_survives_real_template_save_confirm_reopen_and_dispatch_payload(monkeypatch, tmp_path, method):
    from unittest.mock import AsyncMock
    import routers.grade as grade
    from routers import grade_pipeline
    from routers import grade_template
    from services.template_session import TemplateSession
    from tests.test_slot_http_e2e import seed
    from tests.test_template_session import _make_app_and_session, _client

    _, factory = _make_app_and_session()
    monkeypatch.setattr(grade_pipeline, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_template, "_classify_confirmed_template_knowledge", AsyncMock())
    session, boxes, anchors = seed(tmp_path)
    client = _client(factory, monkeypatch, raise_server_exceptions=True)
    q = copy.deepcopy(session.pages[0]["questions"][0])
    q.update(question_type="solve", grading_method=method)
    payload = {
        "page_index": 0, "columns": [{"column_index": 1, "x1_1000": 0, "x2_1000": 1000}],
        "question_anchors": anchors,
        "template_answer_regions": [{"question_number": "2", "regions": boxes,
                                      "slot_ids": q["slot_ids"], "region_kinds": q["region_kinds"]}],
        "template_handwriting_regions": [{"question_number": "2", "regions": boxes}],
        "question_schema": [{**q, "question_number": "2", "drawing_regions": [],
                             "label_anchor": None, "answer_kind": "non_graphic"}],
    }
    saved = client.put(f"/api/batch-grade/template/{session.template_id}/layout-overrides", json=payload)
    assert saved.status_code == 200, saved.text
    reopened = TemplateSession.load_meta(session.template_id, str(tmp_path))
    # Layout writes carry geometry; the answer-confirmation request owns rubric edits.
    reopened.pages[0]["questions"][0]["grading_method"] = method
    confirmed = client.post("/api/batch-grade/template/confirm", json={
        "template_id": session.template_id, "category": "daily", "pages": reopened.pages})
    assert confirmed.status_code == 200, confirmed.text
    frozen = TemplateSession.load_meta(session.template_id, str(tmp_path))
    answer_map = frozen.build_answer_map()
    assert answer_map["2"]["grading_method"] == method
    assert answer_map["2"]["slot_ids"] == q["slot_ids"]
    raw = {"question_no": "2", "student_slots": [
        {"slot_id": slot_id, "answer_state": "visible", "student_answer": answer}
        for slot_id, answer in zip(q["slot_ids"], q["answer_slots"])]}
    units = build_grading_units([raw], answer_map, "daily")
    assert all(llm_payload_for_unit(unit).get("grading_method", "") == method for unit in units)
