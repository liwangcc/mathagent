"""Flash numbers a page's questions its own way on real exam pages; the page must still be recognized.

Shapes below are the ones measured on real papers (2021 and 2024 college
entrance exam pages, 2026-11-11): a page of solve problems 17-20 that Flash
numbered once by position and once by printed number, and a page that opens
with the rest of question 17 from the previous page. Before this fix three of
four such answers failed the whole page and the fourth lost its coarse boxes.
"""
import asyncio
import copy
import json

import pytest
from PIL import Image

from services import template_directory_pipeline as pipeline
from services import template_max_owned as owned
from services import template_one_three as contract
from services.teaching_context import make_teaching_context, use_teaching_context
from tests.test_template_one_three import v4_payloads

PARTS = ["(1)", "(2)"]


def row(number, path, cues=PARTS, *, slots_as=None):
    """One hierarchy row; its slots are numbered under ``slots_as`` (default: the row's own number)."""
    name = number if slots_as is None else slots_as
    return {"question_number": number, "printed_path": list(path),
            "slots": [{"slot_id": f"{name}[{index}]", "cue": cue} for index, cue in enumerate(cues, 1)]}


def page(*rows, version=4):
    rows = [copy.deepcopy(item) for item in rows]
    if version == 3:
        for item in rows:
            item.pop("slots")
    return {"contract_version": version, "questions": rows}


SOLVE_PAGE_BY_POSITION = page(*(row(str(index), [f"{printed}."]) for index, printed in enumerate(range(17, 21), 1)))
SOLVE_PAGE_BY_PRINTED_NUMBER = page(*(row(str(printed), [f"{printed}."]) for printed in range(17, 21)))
CONTINUED_PAGE = page(row("1", []), row("2", ["18."], PARTS + ["(3)"]), row("3", ["19."], PARTS + ["(3)"]))


def test_a_page_numbered_by_printed_number_is_the_same_page_numbered_by_position():
    wire = copy.deepcopy(SOLVE_PAGE_BY_PRINTED_NUMBER)
    hierarchy = contract.receive_hierarchy(wire, version=4)

    assert hierarchy == contract.receive_hierarchy(copy.deepcopy(SOLVE_PAGE_BY_POSITION), version=4)
    assert hierarchy["leaf_ids"] == ["1", "2", "3", "4"]
    assert [node["slot_ids"] for node in hierarchy["nodes"]] == [[f"{index}[1]", f"{index}[2]"] for index in range(1, 5)]
    # The printed number survives where it was read: in the printed path.
    assert [node["source_path"] for node in hierarchy["nodes"]] == [["17."], ["18."], ["19."], ["20."]]
    assert wire == SOLVE_PAGE_BY_PRINTED_NUMBER


@pytest.mark.parametrize("first", [
    pytest.param(row("1", []), id="numbered_by_position"),
    pytest.param(row("1", [""]), id="empty_label_component"),
    pytest.param(row("17", []), id="numbered_by_the_number_it_continues"),
    pytest.param(row("17", [], slots_as="1"), id="printed_number_with_positional_slots"),
])
def test_a_page_may_open_with_the_unlabelled_rest_of_the_previous_pages_question(first):
    hierarchy = contract.receive_hierarchy(page(first, *CONTINUED_PAGE["questions"][1:]), version=4)

    assert hierarchy == contract.receive_hierarchy(copy.deepcopy(CONTINUED_PAGE), version=4)
    assert hierarchy["leaf_ids"] == ["1", "2", "3"]
    # Its printed label stays unknown in the hierarchy; only the models are told what the row is.
    assert hierarchy["nodes"][0]["source_path"] == [] and hierarchy["nodes"][0]["raw_label"] == ""
    assert [item["printed_path"] for item in contract.question_contract(hierarchy)] == [
        [contract.CONTINUED_FROM_PREVIOUS_PAGE], ["18."], ["19."]]
    assert contract.question_contract(hierarchy)[0]["slots"] == [
        {"slot_id": "1[1]", "cue": "(1)"}, {"slot_id": "1[2]", "cue": "(2)"}]


@pytest.mark.parametrize("wire, message", [
    pytest.param(page(row("1", ["1."]), row("2", ["2."]), row("4", ["3."])), "连续编号", id="gap_in_page_numbering"),
    pytest.param(page(row("17", ["三、解答题"])), "连续编号", id="number_the_path_does_not_show"),
    pytest.param(page(row("17", ["117."])), "连续编号", id="number_inside_a_longer_number"),
    pytest.param(page(row("1", ["17."]), row("17", ["18."])), "连续编号", id="number_of_another_row"),
    pytest.param(page(row("17(1)", ["17.", "(1)"])), "连续编号", id="not_a_plain_number"),
    pytest.param(page(row("", ["17."])), "连续编号", id="empty_number"),
    pytest.param(page(row(1, ["1."], slots_as="1")), "连续编号", id="number_not_text"),
    pytest.param(page(row("17", ["17."], slots_as="18")), "作答位编号", id="slots_under_another_number"),
    pytest.param(page({"question_number": "17", "printed_path": ["17."], "slots": [
        {"slot_id": "1[1]", "cue": "(1)"}, {"slot_id": "17[2]", "cue": "(2)"}]}), "作答位编号", id="slots_under_both_names"),
    pytest.param(page(row("1", ["17."]), row("2", [])), "标记无效", id="later_question_without_any_label"),
    pytest.param(page(row("1", [" "])), "标记无效", id="blank_label_component"),
    pytest.param(page(row("17", ["17."]), version=3), "连续编号", id="historical_v3_stays_positional"),
    pytest.param(page(row("1", []), version=3), "标记无效", id="historical_v3_needs_a_label"),
])
def test_numbering_the_page_itself_does_not_back_is_still_rejected(wire, message):
    with pytest.raises(ValueError, match=message):
        contract.receive_hierarchy(wire)


def coarse_wire(numbers):
    return {"coordinate_space": "normalized_1000_v1", "questions": [
        {"question_number": number, "bbox": [0, index * 200, 1000, (index + 1) * 200]}
        for index, number in enumerate(numbers)]}


@pytest.mark.parametrize("numbers", [["1", "2", "3", "4"], ["17", "18", "19", "20"]])
def test_coarse_rows_are_numbered_by_position_whatever_flash_called_them(numbers):
    labels = ["stale"]
    coarse = pipeline._receive_coarse_locator(coarse_wire(numbers), width=600, height=800, labels=labels)

    assert coarse == coarse_wire(["1", "2", "3", "4"])
    assert labels == numbers


@pytest.mark.parametrize("numbers", [["17", "17"], ["17", "十八"], ["1", ""], [1, 2], ["17", "18.5"]])
def test_coarse_rows_without_distinct_plain_numbers_are_rejected(numbers):
    with pytest.raises(ValueError, match="Flash 粗定位题号无效"):
        pipeline._receive_coarse_locator(coarse_wire(numbers), width=600, height=800)


@pytest.mark.parametrize("wire, labels, follows", [
    (SOLVE_PAGE_BY_POSITION, ["1", "2", "3", "4"], True),
    (SOLVE_PAGE_BY_POSITION, ["17", "18", "19", "20"], True),
    (SOLVE_PAGE_BY_PRINTED_NUMBER, ["17", "18", "19", "20"], True),
    (SOLVE_PAGE_BY_POSITION, ["1", "18", "3", "20"], True),
    (SOLVE_PAGE_BY_POSITION, ["1", "2", "3"], False),              # one question fewer
    (SOLVE_PAGE_BY_POSITION, ["18", "19", "20", "21"], False),     # the same count, shifted by one question
    (SOLVE_PAGE_BY_POSITION, ["17", "18", "20", "19"], False),
    (CONTINUED_PAGE, ["1", "2", "3"], True),
    (CONTINUED_PAGE, ["17", "18", "19"], True),                    # the first question shows no label to compare
    (CONTINUED_PAGE, ["18", "19", "20"], False),                   # the opening fragment was not boxed
])
def test_coarse_rows_must_be_the_hierarchys_questions_in_the_same_order(wire, labels, follows):
    hierarchy = contract.receive_hierarchy(copy.deepcopy(wire), version=4)

    assert contract.coarse_follows_hierarchy(labels, hierarchy) is follows


def max_answers(hierarchy_wire):
    """The directory Max is given for this page and its canonical locator and solver answers."""
    _, branches = v4_payloads(tuple(len(item["slots"]) for item in hierarchy_wire["questions"]))
    manifest = contract.question_contract(contract.receive_hierarchy(copy.deepcopy(hierarchy_wire), version=4))
    return manifest, branches["template-directory-v1"], branches[contract.SOLVE_PHASE]


def recognize(tmp_path, monkeypatch, hierarchy_wire, coarse_numbers, *, number_boxes=None):
    """The real pipeline on a blank page; Flash answers as scripted, Max answers the canonical directory.

    ``number_boxes`` replaces the printed-number box Max returns for those question numbers.
    """
    source = tmp_path / "paper.png"
    Image.new("RGB", (600, 800), "white").save(source)
    _, branches = v4_payloads(tuple(len(item["slots"]) for item in hierarchy_wire["questions"]))
    for located in branches["template-directory-v1"]["questions"]:
        if located["question_number"] in (number_boxes or {}):
            located["question_number_bbox"] = number_boxes[located["question_number"]]
    prompts = {}

    async def request(images, prompt, phase, validator, **kwargs):
        prompts[phase] = prompt
        if phase == "template-hierarchy-v1":
            return validator(copy.deepcopy(hierarchy_wire))
        if phase == "template-coarse-locator-v1":
            return validator(coarse_wire(coarse_numbers))
        return validator(contract.parse_json(json.dumps(branches[phase], ensure_ascii=False)))

    monkeypatch.setattr(pipeline, "_request", request)
    with use_teaching_context(make_teaching_context("senior")):
        result = asyncio.run(pipeline.recognize_directory_template(
            str(source), template_mode="blank", layout_result={}))
    return result, prompts


@pytest.mark.parametrize("hierarchy_wire, coarse_numbers", [
    pytest.param(SOLVE_PAGE_BY_POSITION, ["17", "18", "19", "20"], id="hierarchy_by_position_coarse_by_printed_number"),
    pytest.param(SOLVE_PAGE_BY_PRINTED_NUMBER, ["1", "2", "3", "4"], id="hierarchy_by_printed_number"),
    pytest.param(SOLVE_PAGE_BY_PRINTED_NUMBER, ["17", "18", "19", "20"], id="both_by_printed_number"),
])
def test_a_solve_page_numbered_by_printed_number_is_recognized_with_its_coarse_boxes(
        tmp_path, monkeypatch, hierarchy_wire, coarse_numbers):
    result, prompts = recognize(tmp_path, monkeypatch, hierarchy_wire, coarse_numbers)

    assert "error" not in result
    assert [question["question_number"] for question in result["questions"]] == ["1", "2", "3", "4"]
    assert [question["printed_path"] for question in result["questions"]] == [["17."], ["18."], ["19."], ["20."]]
    assert [question["slot_ids"] for question in result["questions"]] == [[f"{n}[1]", f"{n}[2]"] for n in range(1, 5)]
    diagnostics = result["template_ocr_diagnostics"]
    assert diagnostics["coarse_locator_available"] is True and diagnostics["coarse_crop_count"] == 4
    assert "coarse_locator_error_info" not in diagnostics
    # Max is asked about positions 1-4 and is handed the coarse boxes under the same numbers.
    hint = json.loads(prompts["template-directory-v1"].rsplit("权威坐标：", 1)[1])
    assert hint == coarse_wire(["1", "2", "3", "4"])


def test_coarse_boxes_of_other_questions_than_the_hierarchys_are_dropped_not_misassigned(tmp_path, monkeypatch):
    result, prompts = recognize(tmp_path, monkeypatch, SOLVE_PAGE_BY_POSITION, ["18", "19", "20", "21"])

    assert "error" not in result and len(result["questions"]) == 4
    diagnostics = result["template_ocr_diagnostics"]
    assert diagnostics["coarse_locator_available"] is False and diagnostics["coarse_crop_count"] == 0
    assert "coarse_locator_error_info" in diagnostics
    assert "粗定位提示" not in prompts["template-directory-v1"]


def test_a_page_that_opens_mid_question_is_recognized_and_its_first_label_stays_unknown(tmp_path, monkeypatch):
    result, prompts = recognize(tmp_path, monkeypatch, CONTINUED_PAGE, ["1", "2", "3"])

    assert "error" not in result
    assert [question["question_number"] for question in result["questions"]] == ["1", "2", "3"]
    # No made-up printed label reaches the template: page routing compares printed labels across pages.
    assert [question["printed_path"] for question in result["questions"]] == [[], ["18."], ["19."]]
    assert result["questions"][0]["printed_label"] == ""
    assert result["template_ocr_diagnostics"]["coarse_locator_available"] is True
    told = '{"q_id":"Q1","printed_path":["' + contract.CONTINUED_FROM_PREVIOUS_PAGE + '"]'
    assert all(told in prompts[phase] for phase in ("template-directory-v1", contract.SOLVE_PHASE, contract.TRANSCRIBE_PHASE))


def test_both_flash_prompts_number_by_position_and_count_an_opening_fragment():
    for prompt in (pipeline._HIERARCHY_PROMPT, pipeline._COARSE_LOCATOR_PROMPT):
        assert "本页内的顺序号" in prompt and "与印刷题号无关" in prompt
        assert "页面开头若是上一页某题的后续内容" in prompt and "作为第1题" in prompt
    assert "printed_path为空数组[]" in pipeline._HIERARCHY_PROMPT
    # The example shows a printed number that differs from the position.
    assert '"question_number":"1","printed_path":["基础练习","5."]' in pipeline._HIERARCHY_PROMPT


def test_the_question_carried_over_from_the_previous_page_needs_no_printed_number_box():
    manifest, located, _ = max_answers(CONTINUED_PAGE)
    printed_box = located["questions"][1]["question_number_bbox"]
    located["questions"][0]["question_number_bbox"] = None

    received = owned.receive_locator_candidates(located, manifest)

    assert [question["question_number_bbox"] is None for question in received["questions"]] == [True, False, False]
    assert received["questions"][1]["question_number_bbox"] == [float(value) for value in printed_box]
    # Its answer areas are located like any other question's.
    assert all(len(box) == 4 for box in received["questions"][0]["regions"])


def test_a_box_max_still_draws_for_the_carried_over_question_is_kept():
    manifest, located, _ = max_answers(CONTINUED_PAGE)

    received = owned.receive_locator_candidates(copy.deepcopy(located), manifest)

    assert received["questions"][0]["question_number_bbox"] == [
        float(value) for value in located["questions"][0]["question_number_bbox"]]


@pytest.mark.parametrize("hierarchy_wire, position", [
    pytest.param(CONTINUED_PAGE, 1, id="a_later_question_of_a_continued_page"),
    pytest.param(SOLVE_PAGE_BY_POSITION, 0, id="the_first_question_of_a_page_that_prints_its_number"),
])
def test_a_question_whose_number_is_printed_on_the_page_still_needs_its_box(hierarchy_wire, position):
    manifest, located, _ = max_answers(hierarchy_wire)
    located["questions"][position]["question_number_bbox"] = None

    with pytest.raises(ValueError, match="Max 作答框坐标无效"):
        owned.receive_locator_candidates(located, manifest)


def test_the_carried_over_question_goes_to_the_teacher_whatever_the_solver_says():
    manifest, _, solved = max_answers(CONTINUED_PAGE)
    assert all(answer["uncertain"] == [] for answer in solved["answers"])

    confident = owned.receive_solver_candidates(copy.deepcopy(solved), manifest)
    solved["answers"][0]["uncertain"] = ["结果依赖未显示的条件"]
    hedged = owned.receive_solver_candidates(solved, manifest)

    assert [answer["answer_review_reasons"] for answer in confident["answers"]] == [
        [owned.CONTINUED_REVIEW_REASON], [], []]
    assert hedged["answers"][0]["answer_review_reasons"] == [owned.CONTINUED_REVIEW_REASON, "结果依赖未显示的条件"]


def test_only_the_carried_over_row_of_the_directory_carries_the_handling_note():
    continued, _, _ = max_answers(CONTINUED_PAGE)
    printed, _, _ = max_answers(SOLVE_PAGE_BY_POSITION)

    assert continued[0]["note"] == owned.CONTINUED_NOTE and owned.continues_previous_page(continued[0])
    assert all(set(row) == {"q_id", "printed_path", "slots"} for row in continued[1:] + printed)
    assert not any(owned.continues_previous_page(row) for row in continued[1:] + printed)


def test_a_continued_page_is_recognized_without_a_number_box_and_only_its_first_question_awaits_the_teacher(
        tmp_path, monkeypatch):
    result, prompts = recognize(tmp_path, monkeypatch, CONTINUED_PAGE, ["1", "2", "3"], number_boxes={"1": None})

    assert "error" not in result
    first, *rest = result["questions"]
    assert first["answer_status"] == "needs_review" and owned.CONTINUED_REVIEW_REASON in first["directory_issues"]
    assert [question["answer_status"] for question in rest] == ["ready", "ready"]
    assert result["requires_teacher_confirmation"] == ["1"]
    assert ["question_number_bbox" in anchor for anchor in result["question_anchors"]] == [False, True, True]
    assert all(box for box in first["answer_bboxes"])
    # Locator, solver and transcription all read the same row, note included.
    assert all(owned.CONTINUED_NOTE in prompts[phase]
               for phase in ("template-directory-v1", contract.SOLVE_PHASE, contract.TRANSCRIBE_PHASE))


def test_a_page_without_a_carried_over_question_is_told_nothing_about_one(tmp_path, monkeypatch):
    result, prompts = recognize(tmp_path, monkeypatch, SOLVE_PAGE_BY_POSITION, ["1", "2", "3", "4"])

    assert "error" not in result and result["requires_teacher_confirmation"] == []
    assert not any("接上页" in prompts[phase]
                   for phase in ("template-directory-v1", contract.SOLVE_PHASE, contract.TRANSCRIBE_PHASE))


def test_a_question_of_an_older_hierarchy_that_carries_no_printed_path_is_not_taken_for_a_carried_over_one():
    hierarchy = contract.receive_hierarchy(copy.deepcopy(CONTINUED_PAGE), version=4)
    assert contract.continued_question(hierarchy) == "1"
    pathless = copy.deepcopy(hierarchy)
    for node in pathless["nodes"]:
        node.pop("source_path")
    historical = contract.receive_hierarchy(page(row("1", ["17."]), row("2", ["18."]), version=3), version=3)
    for node in historical["nodes"]:
        node["source_path"] = []

    printed = contract.receive_hierarchy(copy.deepcopy(SOLVE_PAGE_BY_POSITION), version=4)
    later = copy.deepcopy(printed)
    later["nodes"][1]["source_path"] = []

    for other in (pathless, historical, printed, later):
        assert contract.continued_question(other) is None
        rows = contract.question_contract(other)
        assert not any("note" in row or owned.continues_previous_page(row) for row in rows)
    assert [row["printed_path"] for row in contract.question_contract(pathless)] == [[], [], []]
