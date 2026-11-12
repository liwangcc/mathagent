"""A solve question whose answer area is only a strip goes to the teacher.

Measured on a real exam page (2021 college entrance paper, page 4, questions
17-20, 2026-11-11): the page leaves no room to answer, and the locator
returned answer boxes 45-72 px high beside number boxes 28-35 px high. Nothing
marked them. On real worksheets in the same measurements the answer areas of
solve questions were 3.2 to 6.3 number-box heights.
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

# (number box height, answer area heights) in px, as measured.
EXAM_PAGE_STRIPS = [(28, [45]), (35, [66]), (35, [60]), (30, [72])]
# The same two heights in number-box lines, read off real worksheets.
WORKSHEET_AREAS = [(14, [3.7]), (16, [3.2]), (14, [4.9, 4.9]), (11, [6.3, 6.3]), (13, [5.2, 5.2, 5.2])]


def page(questions, *, kind="solve"):
    """Flash directory and Max answers for questions given as (line height, [area height, ...])."""
    raw, branches = v4_payloads(tuple(len(areas) for _, areas in questions))
    for located, (line, areas) in zip(branches["template-directory-v1"]["questions"], questions):
        located["question_type"] = kind
        located["question_number_bbox"] = [50, 100, 80, 100 + line]
        for region, height in zip(located["regions"], areas):
            region["bbox"] = [100, 200, 900, 200 + height]
    return raw, branches


def reconciled(raw, branches):
    manifest = contract.question_contract(contract.receive_hierarchy(copy.deepcopy(raw), version=4))
    located, solved = owned.reconcile_slot_candidates(
        owned.receive_locator_candidates(branches["template-directory-v1"], manifest),
        owned.receive_solver_candidates(branches[contract.SOLVE_PHASE], manifest), manifest)
    return located, owned.bind_answers(located, solved)


def marked(answers):
    return [any("作答区不足三行高" in reason for reason in answer["answer_review_reasons"]) for answer in answers["answers"]]


def test_the_strips_measured_on_the_exam_page_all_go_to_the_teacher():
    _, answers = reconciled(*page(EXAM_PAGE_STRIPS))

    assert marked(answers) == [True, True, True, True]
    assert [answer["answer_status"] for answer in answers["answers"]] == ["needs_review"] * 4
    assert answers["answers"][1]["answer_review_reasons"] == [
        "第 2 题是解答题，但作答区不足三行高，可能没有框到完整的作答空白，请核对或重画"]


def test_the_answer_areas_measured_on_real_worksheets_are_left_alone():
    located, answers = reconciled(*page([(line, [round(line * lines) for lines in areas]) for line, areas in WORKSHEET_AREAS]))

    assert marked(answers) == [False] * len(WORKSHEET_AREAS)
    assert [answer["answer_status"] for answer in answers["answers"]] == ["ready"] * len(WORKSHEET_AREAS)
    assert located["needs_review"] == []


@pytest.mark.parametrize("kind", ["fill", "choice"])
def test_a_blank_to_fill_or_a_choice_is_never_judged_by_its_height(kind):
    _, answers = reconciled(*page([(30, [20]), (30, [45])], kind=kind))

    assert marked(answers) == [False, False]


def test_one_area_with_room_to_work_is_enough():
    _, answers = reconciled(*page([(30, [40, 200]), (30, [40, 60])]))

    assert marked(answers) == [False, True]


@pytest.mark.parametrize("height, low", [(83, True), (84, False), (85, False)])
def test_the_line_is_drawn_at_two_point_eight_number_box_heights(height, low):
    _, answers = reconciled(*page([(30, [height])]))

    assert marked(answers) == [low]


def test_a_question_the_locator_could_not_place_is_not_judged():
    raw, branches = page([(30, [40]), (30, [40])])
    branches["template-directory-v1"]["questions"][0] = {"question_number": "1", "status": "refused", "reason": "看不清"}

    _, answers = reconciled(raw, branches)

    assert marked(answers) == [False, True]


def test_only_the_strip_questions_of_a_recognized_page_await_the_teacher(tmp_path, monkeypatch):
    source = tmp_path / "paper.png"
    Image.new("RGB", (600, 800), "white").save(source)
    raw, branches = page([(30, [40]), (30, [200]), (30, [60])])

    async def request(images, prompt, phase, validator, **kwargs):
        if phase == "template-hierarchy-v1":
            return validator(copy.deepcopy(raw))
        if phase == "template-coarse-locator-v1":
            raise RuntimeError("no coarse boxes in this test")
        return validator(contract.parse_json(json.dumps(branches[phase], ensure_ascii=False)))

    monkeypatch.setattr(pipeline, "_request", request)
    with use_teaching_context(make_teaching_context("senior")):
        result = asyncio.run(pipeline.recognize_directory_template(str(source), template_mode="blank", layout_result={}))

    assert "error" not in result
    assert result["requires_teacher_confirmation"] == ["1", "3"]
    assert [question["answer_status"] for question in result["questions"]] == ["needs_review", "ready", "needs_review"]
    # The boxes themselves are delivered as located; only the mark is added.
    assert [len(question["answer_bboxes"]) for question in result["questions"]] == [1, 1, 1]
