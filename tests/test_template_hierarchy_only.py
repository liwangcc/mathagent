"""The first Flash lists printed parent question IDs, never physical slots."""
import copy

import pytest

from services import template_one_three as contract
from services import template_directory_pipeline as pipeline


def hierarchy_payload(count=1):
    return {"contract_version": 3, "questions": [
        {"question_number": str(i), "printed_path": ["基础练习", str(i)]}
        for i in range(1, count + 1)]}


def test_hierarchy_only_has_no_physical_slot_decisions():
    raw = hierarchy_payload(25)
    before = copy.deepcopy(raw)
    h = contract.receive_hierarchy(raw)
    assert h["question_identity_version"] == 3
    assert h["leaf_ids"] == [str(i) for i in range(1, 26)]
    for node in h["nodes"]:
        assert not set(node) & {"area_kinds", "answer_slot_count", "slot_ids", "drawing_area_count", "cue", "subquestions"}
    assert contract.question_contract(h)[0] == {
        "q_id": "Q1", "printed_path": ["基础练习", "1"]}
    assert raw == before


def test_printed_path_preserves_numbered_parent_without_inventing_blank_ids():
    raw = hierarchy_payload(2)
    raw["questions"][0]["printed_path"] = ["计算", "2.", "(1)"]
    raw["questions"][1]["printed_path"] = ["计算", "2.", "(2)"]
    h = contract.receive_hierarchy(raw)
    assert contract.question_contract(h) == [
        {"q_id": "Q1", "printed_path": ["计算", "2.", "(1)"]},
        {"q_id": "Q2", "printed_path": ["计算", "2.", "(2)"]}]
    assert len(h["nodes"]) == 2


@pytest.mark.parametrize("field,value", [
    ("answer_regions", ["drawing"]), ("answer_slot_count", 7),
    ("cue", "题干"), ("requires_drawing", True), ("answer_kind", "mixed"),
    ("regions", [[0, 0, 1, 1]]), ("standard_answer", "4/5"),
    ("subquestions", ["(1)", "(2)"]),
])
def test_hierarchy_rejects_responsibilities_owned_by_other_branches(field, value):
    raw = hierarchy_payload()
    raw["questions"][0][field] = value
    with pytest.raises(ValueError):
        contract.receive_hierarchy(raw)


@pytest.mark.parametrize("fault", ["version", "number", "path", "nontext_path", "empty_label", "empty"])
def test_hierarchy_only_validates_structure(fault):
    raw = hierarchy_payload()
    if fault == "version": raw["contract_version"] = True
    elif fault == "number": raw["questions"][0]["question_number"] = "2"
    elif fault == "path": raw["questions"][0]["printed_path"] = "1"
    elif fault == "nontext_path": raw["questions"][0]["printed_path"] = [1]
    elif fault == "empty_label": raw["questions"][0]["printed_path"] = [""]
    else: raw["questions"] = []
    with pytest.raises(ValueError):
        contract.receive_hierarchy(raw)


def test_new_directory_receiver_does_not_synthesize_default_answer_slot():
    h = contract.receive_hierarchy(hierarchy_payload())
    assert contract.question_contract(h)[0]["q_id"] == "Q1"
    assert not any("answer_slot_count" in row for row in h["nodes"])


def test_active_flash_prompt_shares_slots_but_not_geometry_or_types():
    prompt = pipeline._HIERARCHY_PROMPT
    assert '"contract_version":4' in prompt
    assert all(token in prompt for token in ("printed_path", "slots", "slot_id", "cue"))
    assert "才建立作图槽位" in prompt
    assert "看图回答" in prompt
    assert "不明确时不要猜成作图题" in prompt
    for token in ["subquestions", "answer_regions", "bbox"]:
        assert token not in prompt
