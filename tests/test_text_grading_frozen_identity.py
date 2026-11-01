"""Frozen template slot IDs are model IDs, not legacy display/scoring aliases."""
import asyncio
import copy
import json

import pytest

from services import flash_grading as flash
from services import grader
from services.grading_contract import AnswerSpec
from services.scoring_engine import build_grading_units, llm_payload_for_unit
from services.template_slots import bind_student_slots, slot_transcription_prompt


def frozen_spec(parent, ids, qtype="solve"):
    spec = AnswerSpec(
        question_no=parent, question_text="按要求完成各作答位。", question_type=qtype,
        standard_answer="；".join(f"参考{i}" for i in range(len(ids))),
        answer_slots=[f"参考{i}" for i in range(len(ids))], slot_ids=ids,
        region_kinds=["answer"] * len(ids), blank_count=len(ids),
        subquestion_count=len(ids), max_score=3 * len(ids),
        subquestion_scores=[3] * len(ids) if qtype == "solve" else [],
    )
    return spec


def ocr_row(parent, ids):
    return {"question_no": parent, "student_answer": "display text is not the source",
            "student_slots": list(reversed([
                {"slot_id": slot, "student_answer": f"作答{i}\n步骤;同一槽", "answer_state": "visible"}
                for i, slot in enumerate(ids)
            ]))}


@pytest.mark.parametrize("parent,ids,legacy_keys", [
    ("5", ["5[1]"], ["5"]),
    ("5", ["5[5]"], ["5(5)"]),
    ("2", ["2[2]", "2[5]"], ["2(2)", "2(5)"]),
    ("5.1", ["5.1[1]"], ["5.1"]),
])
def test_model_identity_is_frozen_slot_even_for_single_or_sparse_solve(parent, ids, legacy_keys):
    entry = frozen_spec(parent, ids).to_answer_map_entry()
    units = build_grading_units([ocr_row(parent, ids)], {parent: entry}, "exam")
    assert [u["key"] for u in units] == legacy_keys
    for unit, slot in zip(units, ids):
        payload = llm_payload_for_unit(unit)
        assert payload["question_no"] == payload["slot_id"] == slot
        assert f"只判断作答位 {slot}；" in payload["question_text"]
        assert payload["student_answer"] == unit["student_answer"]
        assert payload["standard_answer"] == unit["standard_answer"]


@pytest.mark.parametrize("qtype", ["fill", "solve"])
@pytest.mark.parametrize("ids", [["5[1]"], ["5[2]", "5[5]"]])
@pytest.mark.parametrize("second_page", [False, True])
def test_frozen_template_ocr_persistence_wire_schema_and_score_mapping(monkeypatch, tmp_path, qtype, ids, second_page):
    if second_page:
        from services.template_page_numbering import globalize_page_numbers
        local_ids = ["1" + slot[1:] for slot in ids]
        page = {"questions": [frozen_spec("1", local_ids, qtype).model_dump()]}
        merged = globalize_page_numbers(page, 4, page_index=1)
        spec = AnswerSpec.model_validate(merged["questions"][0])
        assert spec.question_no == "5" and spec.slot_ids == ids
        assert spec.page_local_number == "1"
        with pytest.raises(ValueError, match="不能重复偏移"):
            globalize_page_numbers(merged, 4, page_index=1)
    else:
        spec = frozen_spec("5", ids, qtype)
    frozen_file = tmp_path / "confirmed-contract.json"
    frozen_file.write_text(json.dumps(spec.model_dump(), ensure_ascii=False), encoding="utf-8")
    reopened = AnswerSpec.model_validate_json(frozen_file.read_text(encoding="utf-8"))
    answer_map = {"5": reopened.to_answer_map_entry()}
    prompt = slot_transcription_prompt([reopened.model_dump()])
    assert all(slot in prompt for slot in ids)
    bound = bind_student_slots(ocr_row("5", ids), "5", answer_map["5"])
    stored_file = tmp_path / "transcription.json"
    stored_file.write_text(json.dumps(bound, ensure_ascii=False), encoding="utf-8")
    stored = json.loads(stored_file.read_text(encoding="utf-8"))
    snapshot = copy.deepcopy(answer_map)
    units = build_grading_units([stored], answer_map, "exam")
    expected = {slot: ("correct" if i == 0 else "wrong") for i, slot in enumerate(ids)}
    captured = []

    class ImmediateQueue:
        async def submit(self, coro_factory, **kwargs):
            return await coro_factory()

    async def transport(messages, **kwargs):
        refs = json.loads(messages[0]["content"].split("教师确认资料：", 1)[1])
        answers = json.loads(messages[1]["content"])["answers"]
        assert len(refs) == 1 and refs[0]["question_no"] == "5"
        assert refs[0]["question_text"] == answer_map["5"]["question_text"]
        wire_ids = list(refs[0]["answers"])
        captured.extend(wire_ids)
        assert wire_ids == ids
        assert [q["question_no"] for q in answers] == ids
        assert [q["student_answer"] for q in answers] == [u["student_answer"] for u in units]
        assert kwargs["response_schema"]["properties"]["results"]["required"] == ids
        content = json.dumps({"results": {
            slot: {"result": expected[slot], "explanation": "", "hint": "", "model_answer": "", "standard_answer": ""}
            for slot in reversed(ids)
        }})
        kwargs["response_validator"](content)
        return {"content": content, "receipt": {}, "model": flash.FLASH_MODEL, "parameters": {}, "usage": {}}

    monkeypatch.setattr(grader, "get_grading_provider", lambda: "qwen3.8-flash")
    monkeypatch.setattr(grader, "get_grading_queue", ImmediateQueue)
    monkeypatch.setattr(grader, "judge_paper", flash.judge_text_paper)
    monkeypatch.setattr(flash, "call_flash", transport)
    result = asyncio.run(grader.grade_paper_unified({"questions": [stored]}, answer_map, "synthetic", 1, category="exam"))
    assert captured == ids
    assert result["needs_review_count"] == 0
    assert result["score"] == 3
    assert answer_map == snapshot
    details = result["details"][0]["blank_results"] if qtype == "fill" else result["details"]
    assert [d["result"] for d in details] == [expected[slot] for slot in ids]
    assert [d["slot_id"] for d in details] == ids
    if qtype == "solve":
        assert [d["question_no"] for d in details] == [u["key"] for u in units]


@pytest.mark.parametrize("fault", ["legacy_alias", "missing", "unknown", "duplicate"])
def test_bad_model_ids_cannot_be_guessed_back_to_scoring_keys(monkeypatch, fault):
    ids = ["5[1]"]
    entry = frozen_spec("5", ids).to_answer_map_entry()

    class ImmediateQueue:
        async def submit(self, coro_factory, **kwargs):
            return await coro_factory()

    async def transport(messages, **kwargs):
        row = {"result": "correct", "explanation": "", "hint": "", "model_answer": "", "standard_answer": ""}
        if fault == "duplicate":
            item = json.dumps(row)
            content = '{"results":{"5[1]":' + item + ',"5[1]":' + item + '}}'
        else:
            key = "5" if fault == "legacy_alias" else "5[2]"
            content = json.dumps({"results": {} if fault == "missing" else {key: row}})
        kwargs["response_validator"](content)
        raise AssertionError("invalid model identity must be rejected")

    monkeypatch.setattr(grader, "get_grading_provider", lambda: "qwen3.8-flash")
    monkeypatch.setattr(grader, "get_grading_queue", ImmediateQueue)
    monkeypatch.setattr(grader, "judge_paper", flash.judge_text_paper)
    monkeypatch.setattr(flash, "call_flash", transport)
    with pytest.raises(grader.GradingModelUnavailable):
        asyncio.run(grader.grade_paper_unified({"questions": [ocr_row("5", ids)]}, {"5": entry}, "synthetic", 1))


def test_legacy_uncontracted_question_keeps_its_exact_id_without_invented_slot():
    unit = {"key": "5.1", "question_text": "题干", "student_answer": "作答", "standard_answer": "参考"}
    payload = llm_payload_for_unit(unit)
    assert payload["question_no"] == "5.1"
    assert "slot_id" not in payload


@pytest.mark.parametrize("bad_id", ["5", "5.1", "6[1]", "5[2]", None])
def test_slot_not_in_frozen_contract_is_rejected_before_dispatch(bad_id):
    unit = build_grading_units([ocr_row("5", ["5[1]"])], {"5": frozen_spec("5", ["5[1]"]).to_answer_map_entry()}, "daily")[0]
    unit["slot_id"] = bad_id
    with pytest.raises(ValueError):
        llm_payload_for_unit(unit)


def test_text_model_cannot_abstain_but_system_failure_sentinel_is_retained():
    assert "needs_review" not in flash._TEXT_RULES
    assert "needs_review" not in flash.build_text_response_schema(["5[1]"])["properties"]["results"]["properties"]["5[1]"]["properties"]["result"]["enum"]
    from services.analyzer import _fallback_paper_result
    failed = _fallback_paper_result(["5[1]"], "system failure")
    assert failed["5[1]"]["result"] == "needs_review"
    assert failed.__task_queue_cacheable__ is False


def test_two_score_keys_cannot_share_one_model_identity(monkeypatch):
    entry = frozen_spec("5", ["5[1]"]).to_answer_map_entry()
    row = ocr_row("5", ["5[1]"])
    units = build_grading_units([row], {"5": entry}, "daily")
    duplicate = copy.deepcopy(units[0])
    duplicate["key"] = "5(1)"
    monkeypatch.setattr(grader, "build_grading_units", lambda *args: [units[0], duplicate])

    def forbidden():
        raise AssertionError("identity collision must be rejected before queue dispatch")

    monkeypatch.setattr(grader, "get_grading_queue", forbidden)
    with pytest.raises(grader.GradingModelUnavailable, match="不能唯一对应"):
        asyncio.run(grader.grade_paper_unified({"questions": [row]}, {"5": entry}, "synthetic", 1))
