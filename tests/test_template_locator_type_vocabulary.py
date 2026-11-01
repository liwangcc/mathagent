"""Adapt the locator's drawing label to the existing internal type model."""
import copy

import pytest

from services import template_directory_pipeline as pipeline
from services import template_max_owned as owned
from services import template_one_three as contract


def hierarchy():
    return contract.receive_hierarchy({"contract_version": 3, "questions": [
        {"question_number": "1", "printed_path": ["作图题", "1"]}]})


def payload(*, question_type="drawing", mixed=False, label_anchor=(50, 110)):
    regions = [{"kind": "drawing", "bbox": [100, 100, 300, 300], "answer_key": None}]
    if mixed:
        regions.append({"kind": "answer", "bbox": [400, 100, 500, 130],
                        "answer_key": {"part": "", "anchor": "", "blank_index": 0}})
    return {"coordinate_space": "normalized_1000_v1", "needs_review": [], "questions": [{
        "question_number": "1", "question_number_bbox": [5, 5, 25, 25],
        "question_type": question_type, "choice_mode": "unknown", "regions": regions,
        "label_anchor": list(label_anchor) if label_anchor is not None else None}]}


@pytest.mark.parametrize("mixed", [False, True])
def test_drawing_type_normalizes_at_boundary_and_preserves_graphic_route(mixed):
    raw = payload(mixed=mixed)
    original = copy.deepcopy(raw)
    h = hierarchy()
    located = pipeline._validate_one_three_locator(raw, h)
    q = located["questions"][0]
    assert q["question_type"] == "solve"
    assert q["requires_drawing"] is True
    assert q["answer_kind"] == ("mixed" if mixed else "drawing")
    assert q["region_kinds"] == (["drawing", "answer"] if mixed else ["drawing"])
    assert q["drawing_regions"] == [[100.0, 100.0, 300.0, 300.0]]
    assert q["label_anchor"] == [50, 110]
    items = [{"answer_key": {"part": "", "anchor": "", "blank_index": 0}, "answer": "3"}] if mixed else []
    solved = contract.receive_solver({"answers": [{
        "q_id": "Q1", "standard_answer": "按要求作图；计算结果3" if mixed else "按要求作图",
        "answer_items": items, "solution_steps": [], "visual_facts": [], "uncertain": []}]}, h)
    merged = contract.merge_branches(located, solved, {"questions": [{
        "q_id": "Q1", "question_text": "在图中标点" + ("并计算" if mixed else ""), "uncertain": []}]})
    assert merged["answers"][0]["answer_slots"] == (["", "3"] if mixed else [""])
    assert merged["answers"][0]["answer_status"] == "ready"
    assert raw == original


def test_drawing_alias_does_not_hide_missing_label_anchor():
    with pytest.raises(ValueError, match="作图题号标记坐标无效"):
        pipeline._validate_one_three_locator(payload(label_anchor=None), hierarchy())


@pytest.mark.parametrize("question_type", ["essay", "unknown", ""])
def test_unrecognized_types_are_not_silently_reclassified(question_type):
    with pytest.raises(ValueError, match="Max 定位题型无效"):
        pipeline._validate_one_three_locator(payload(question_type=question_type), hierarchy())


def test_locator_prompt_declares_the_accepted_drawing_word():
    # The prompt must still name `drawing` as an accepted question_type so the
    # model can emit it. 2026-10-01: the JSON example no longer writes the
    # placeholder `choice|fill|solve|drawing` (models copied that pipe-joined
    # string as a literal value and it failed the enum); the vocabulary is now
    # declared in prose and the example shows one concrete valid value.
    prompt = owned.locator_prompt(contract.question_contract(hierarchy()), width=1000, height=1000, columns=[])
    assert "question_type取choice、fill、solve或drawing" in prompt
    assert '"question_type":"choice|fill|solve|drawing"' not in prompt
