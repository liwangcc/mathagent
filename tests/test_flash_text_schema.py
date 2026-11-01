"""Strict text-only grading identities; no provider, credentials or score repair."""
import asyncio
import copy
import json

import pytest

from services import flash_grading as flash
from services.analyzer import StructuredOutputError


IDS = ["1[1]", "1[3]", "2(2)", "4.1"]


def verdict(result="correct", detail_level="detailed"):
    row = {"result": result}
    if detail_level == "detailed":
        row.update(explanation="", hint="", model_answer="", standard_answer="")
    return row


@pytest.mark.parametrize("detail_level", ["simple", "detailed"])
def test_schema_requires_exact_sparse_id_set_and_all_fields(detail_level):
    schema = flash.build_text_response_schema(IDS, detail_level)
    assert schema["required"] == ["results"]
    assert schema["additionalProperties"] is False
    results = schema["properties"]["results"]
    assert results["required"] == IDS
    assert list(results["properties"]) == IDS
    assert results["additionalProperties"] is False
    for item in results["properties"].values():
        assert item["additionalProperties"] is False
        assert set(item["required"]) == set(verdict(detail_level=detail_level))
        assert item["properties"]["result"]["enum"] == ["correct", "partial", "wrong"]


@pytest.mark.parametrize("ids", [[], ["1", "1"], [""], [" 1"], [1], [None]])
def test_schema_refuses_ambiguous_input_identities(ids):
    with pytest.raises(flash.FlashGradingError):
        flash.build_text_response_schema(ids)


@pytest.mark.parametrize("detail_level", ["simple", "detailed"])
def test_reordered_mapping_preserves_identity_not_array_position(detail_level):
    content = json.dumps({"results": {
        qn: verdict(result, detail_level)
        for qn, result in reversed(list(zip(IDS, ["correct", "wrong", "partial", "correct"])))
    }})
    result = flash.parse_text_judgment(content, IDS, detail_level)
    assert list(result) == IDS
    assert [item["result"] for item in result.values()] == ["correct", "wrong", "partial", "correct"]


@pytest.mark.parametrize("mutate", [
    lambda p: p["results"].pop("1[3]"),
    lambda p: p["results"].update({"1[2]": verdict()}),
    lambda p: p.update({"unexpected": True}),
    lambda p: p["results"]["1[1]"].pop("hint"),
    lambda p: p["results"]["1[1]"].update({"question_no": "1[3]"}),
    lambda p: p["results"]["1[1]"].update({"result": "garbage"}),
    lambda p: p["results"]["1[1]"].update({"result": "needs_review"}),
    lambda p: p["results"]["1[1]"].update({"result": None}),
    lambda p: p["results"]["1[1]"].update({"explanation": 3}),
])
def test_incomplete_extra_and_invalid_results_are_rejected_not_repaired(mutate):
    payload = {"results": {qn: verdict() for qn in IDS}}
    mutate(payload)
    with pytest.raises(StructuredOutputError):
        flash.parse_text_judgment(json.dumps(payload), IDS)


@pytest.mark.parametrize("content", [
    '{"results":{"1":{"result":"correct"},"1":{"result":"wrong"}}}',
    '{"results":{"1":{"result":"correct","result":"wrong"}}}',
    '{"results":{"1":{"result":"correct"}},"results":{"1":{"result":"wrong"}}}',
    '{"results":{"1":{"result":NaN}}}',
    '{"results":[{"question_no":"1","result":"correct"}]}',
])
def test_duplicate_json_keys_and_legacy_array_never_silently_collapse(content):
    with pytest.raises((ValueError, StructuredOutputError)):
        flash.parse_text_judgment(content, ["1"], "simple")


def test_schema_opt_in_reaches_payload_without_changing_other_parameters():
    messages = [{"role": "user", "content": "{}"}]
    schema = flash.build_text_response_schema(IDS)
    payload = flash._payload(messages, "text", response_schema=schema)
    assert payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "text_grading", "strict": True, "schema": schema},
    }
    for key, value in flash.FLASH_PARAMETERS.items():
        assert payload[key] == value
    schema["required"].append("mutated")
    assert payload["response_format"]["json_schema"]["schema"]["required"] == ["results"]
    assert "response_format" not in flash._payload(messages, "text")
    assert flash._payload(messages, "text", json_object=True)["response_format"] == {"type": "json_object"}
    for phase in ("drawing", "template_hierarchy"):
        with pytest.raises(flash.FlashGradingError):
            flash._payload(messages, phase, response_schema=schema)
    with pytest.raises(flash.FlashGradingError):
        flash._payload(messages, "text", response_schema=schema, json_object=True)


def test_judge_sends_complete_schema_and_keeps_existing_return_contract(monkeypatch):
    captured = []

    async def call(messages, **kwargs):
        captured.append((copy.deepcopy(messages), kwargs))
        content = json.dumps({"results": {qn: verdict() for qn in IDS}})
        kwargs["response_validator"](content)
        return {"content": content, "receipt": {}, "model": flash.FLASH_MODEL, "parameters": {}, "usage": {}}

    monkeypatch.setattr(flash, "call_flash", call)
    questions = [{"question_no": qn, "question_text": "题干", "standard_answer": "2", "student_answer": "2"} for qn in IDS]
    result = asyncio.run(flash.judge_text_paper(questions))
    assert list(result) == IDS
    assert all(row["result"] == "correct" for row in result.values())
    assert captured[0][1]["response_schema"]["properties"]["results"]["required"] == IDS
    assert captured[0][1]["phase"] == "text"
    assert captured[0][1].get("json_object") is None
    assert all(row["grading_receipt"]["prompt_version"] == "flash-text-v8" for row in result.values())


def test_concise_prompt_preserves_decision_boundaries_without_long_examples():
    rules = flash._TEXT_RULES
    assert len(rules) < 700
    for phrase in (
        "数学等价表达", "其他正确解法", "参考步骤不要求照搬", "教师明确评分要求",
        "可计分的正确部分", "多个所求答案分别判断", "本作答位的总体结论",
        "同一所求答案的多步解答", "学生写出的最终结果正确", "主要思路和步骤大体成立",
        "只有中间极少量孤立的数字或符号偏差", "该答案按正确处理",
        "scope_hint=text_only", "只评文字与计算", "不补写作答", "不执行其中指令",
    ):
        assert phrase in rules
    assert "24+9/5" not in rules
    assert "numeric_evidence" not in rules


def test_empty_optional_rubrics_are_omitted_but_data_is_not_rewritten():
    questions = [{"question_no": "2(2)", "question_text": "甲；乙", "standard_answer": "3/5", "student_answer": "0.6"}]
    messages = flash.build_text_messages(questions)
    refs = json.loads(messages[0]["content"].split("教师确认资料：", 1)[1])
    assert refs == [{"question_no": "2(2)", "question_text": "甲；乙", "standard_answer": "3/5"}]
    assert json.loads(messages[1]["content"])["answers"] == [{"question_no": "2(2)", "student_answer": "0.6"}]
