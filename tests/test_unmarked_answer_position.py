"""Unmarked single-slot answers: Max hint, persistence and pixel routing.

Synthetic pixels and payloads only; no model calls or business data.
"""
import copy

import numpy as np
import pytest
from PIL import Image, ImageDraw

from services import template_evidence as evidence
from services import template_max_owned as owned
from services.grading_contract import AnswerSpec

MANIFEST = [{"q_id": "Q1", "slots": [{"slot_id": "1[1]", "cue": "选择"}]},
            {"q_id": "Q2", "slots": [{"slot_id": "2[1]", "cue": "a"}, {"slot_id": "2[2]", "cue": "b"}]}]


def row(number, ids, question_type="choice", **extra):
    return {"question_number": number, "question_number_bbox": [50, 100, 70, 120],
            "question_type": question_type, "choice_mode": "single" if question_type == "choice" else "unknown",
            "regions": [{"slot_id": sid, "kind": "answer", "bbox": [300 + 60 * i, 100, 350 + 60 * i, 120]}
                        for i, sid in enumerate(ids)],
            "label_anchor": None, **extra}


def locate(first_extra=None, second_extra=None):
    payload = {"coordinate_space": "normalized_1000_v1", "needs_review": [], "questions": [
        row("1", ["1[1]"], **(first_extra or {})),
        row("2", ["2[1]", "2[2]"], question_type="fill", **(second_extra or {}))]}
    return {q["question_number"]: q for q in owned.receive_locator(payload, MANIFEST)["questions"]}


def test_locator_prompt_asks_for_the_optional_hint_only_in_shared_mode():
    prompt = owned.locator_prompt(MANIFEST, width=1000, height=1400, columns=[])
    assert prompt.count('"answer_position":"unmarked"') == 1
    assert owned.UNMARKED_POSITION_GUIDE in prompt


def test_locator_keeps_unmarked_for_one_text_slot_and_ignores_other_shapes():
    located = locate({"answer_position": "unmarked"}, {"answer_position": "unmarked"})
    assert located["1"]["answer_position"] == "unmarked"
    assert "answer_position" not in located["2"]  # two blanks cannot sit before the number


@pytest.mark.parametrize("value", ["printed", None, "before_number", 1])
def test_other_hint_values_are_today_behaviour_not_a_rejection(value):
    assert "answer_position" not in locate({"answer_position": value})["1"]


def test_unknown_question_fields_are_still_rejected():
    with pytest.raises(ValueError):
        locate({"answer_side": "left"})


def test_answer_spec_carries_the_hint_as_server_owned_metadata():
    spec = AnswerSpec.model_validate({"question_no": "1", "question_type": "choice", "answer_position": "unmarked"})
    assert spec.directory_metadata()["answer_position"] == "unmarked"
    assert spec.model_dump()["answer_position"] == "unmarked"
    plain = AnswerSpec.model_validate({"question_no": "1", "answer_position": "left"})
    assert "answer_position" not in plain.model_dump() and "answer_position" not in plain.directory_metadata()


@pytest.mark.parametrize("question_type,kept", [("choice", True), ("fill", True), ("solve", False)])
def test_schema_keeps_the_hint_for_choice_and_fill(question_type, kept):
    schema = evidence.normalize_question_schema(
        [{"question_number": "3", "question_type": question_type, "answer_position": "unmarked"}])
    assert ("answer_position" in schema[0]) is kept


BLACK, BLUE_INK, RED_INK = (0, 0, 0), (30, 30, 120), (200, 30, 30)


def letter(draw, x, y, color=BLUE_INK):
    """A handwritten "B"-like mark about one printed line high."""
    draw.line([x, y, x, y + 26], fill=color, width=3)
    draw.arc([x - 2, y, x + 16, y + 13], -90, 90, fill=color, width=3)
    draw.arc([x - 2, y + 13, x + 18, y + 26], -90, 90, fill=color, width=3)


def single_row(marks=()):
    """3. <stem>   [frozen box after the stem]; printed text earlier on the row."""
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    for box in ([60, 205, 240, 226], [300, 200, 330, 230], [345, 205, 700, 226]):
        draw.rectangle(box, fill=BLACK)
    for x, y in marks:
        letter(draw, x, y)
    return np.asarray(image).copy()


def choice_page(marks=(), circle=False, tick=False):
    """5. <stem>  /  options row framed by the model, no bracket anywhere."""
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([100, 400, 130, 430], fill=BLACK)  # printed "5."
    draw.rectangle([145, 405, 520, 426], fill=BLACK)  # stem
    for x in (160, 330, 500, 670):                    # four printed options
        draw.rectangle([x, 465, x + 90, 486], fill=BLACK)
    if circle:
        draw.ellipse([318, 452, 432, 498], outline=BLUE_INK, width=3)
    if tick:
        draw.line([40, 412, 50, 426, 78, 398], fill=RED_INK, width=4)
    for x, y in marks:
        letter(draw, x, y)
    return np.asarray(image).copy()


def two_up_page(marks=()):
    """1. <stem>  [box to the gutter]   2. <stem>  [box]  on one row."""
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    for box in ([80, 300, 110, 330], [125, 305, 300, 326], [560, 300, 590, 330], [605, 305, 760, 326]):
        draw.rectangle(box, fill=BLACK)
    for x, y in marks:
        letter(draw, x, y)
    return np.asarray(image).copy()


def snapshot(questions, position="unmarked"):
    regions, schema, anchors = [], [], []
    for number, question_type, number_box, box in questions:
        region = {"question_number": number, "regions": [box], "slot_ids": [f"{number}[1]"],
                  "region_kinds": ["answer"]}
        item = {"question_number": number, "question_type": question_type,
                "choice_mode": "single" if question_type == "choice" else "unknown",
                "slot_ids": [f"{number}[1]"], "region_kinds": ["answer"]}
        if position:
            item["answer_position"] = position
        regions.append(region)
        schema.append(item)
        anchors.append({"question_number": number, "question_number_bbox": number_box})
    return {"page_type_id": "page_1", "template_mode": "blank", "question_numbers": [q[0] for q in questions],
            "question_schema": schema, "question_anchors": anchors,
            "template_answer_regions": regions, "template_handwriting_regions": copy.deepcopy(regions)}


SINGLE = [("3", "choice", [300, 200, 330, 230], [710, 195, 800, 235])]
CHOICE = [("5", "choice", [100, 400, 130, 430], [150, 455, 770, 497])]
TWO_UP = [("1", "fill", [80, 300, 110, 330], [305, 296, 555, 334]),
          ("2", "fill", [560, 300, 590, 330], [765, 296, 900, 334])]


def build(tmp_path, student, template, questions, position="unmarked"):
    result = evidence.build_full_column_difference_evidence_from_aligned(
        student, np.full(student.shape[:2], 255, np.uint8), template, snapshot(questions, position), tmp_path)
    labels = {p["question_number"]: p for c in result.diagnostics["evidence_columns"] for p in c["label_placements"]}
    routing = {r["question_number"]: r for r in result.diagnostics.get("answer_position_routing", [])}
    return result.diagnostics, labels, routing


def test_ink_before_the_number_moves_the_label_left_of_that_ink(tmp_path):
    diagnostics, labels, routing = build(tmp_path, single_row([(272, 202)]), single_row(), SINGLE)
    record, label = routing["3"], labels["3"]
    assert record["decision"] == "before_number" and diagnostics["answer_position_review"] == []
    dx = label["target_anchor_px"][0] - record["label_region_1000"][0]
    assert label["label_bbox"][2] < 272 + dx  # the student's mark is right of its label
    # The gap after earlier printed text on the row is shared at its middle.
    assert record["before_number_px"][0] == 241 + (300 - 241 - 1) // 2
    assert record["before_number_px"][2] == 300 and label["slot_id"] == "3[1]"


@pytest.mark.parametrize("marks,decision", [([(720, 202)], "box"), ([], "box")])
def test_ink_in_the_frozen_box_or_none_keeps_the_box(tmp_path, marks, decision):
    diagnostics, labels, routing = build(tmp_path, single_row(marks), single_row(), SINGLE)
    assert routing["3"]["decision"] == decision and diagnostics["answer_position_review"] == []
    assert labels["3"]["region_bbox_1000"] == SINGLE[0][3]


def test_ink_on_two_sides_goes_to_teacher_review(tmp_path):
    diagnostics, labels, routing = build(tmp_path, single_row([(272, 202), (720, 202)]), single_row(), SINGLE)
    assert routing["3"]["decision"] == "ambiguous" and diagnostics["answer_position_review"] == ["3"]
    assert labels["3"]["region_bbox_1000"] == SINGLE[0][3]


def test_printed_position_contract_is_unchanged(tmp_path):
    diagnostics, labels, routing = build(tmp_path, single_row([(272, 202)]), single_row(), SINGLE, position=None)
    assert "answer_position_routing" not in diagnostics and labels["3"]["region_bbox_1000"] == SINGLE[0][3]


def test_choice_boxed_on_its_options_row_finds_a_letter_after_the_stem(tmp_path):
    diagnostics, labels, routing = build(tmp_path, choice_page([(560, 402)]), choice_page(), CHOICE)
    record = routing["5"]
    assert record["decision"] == "after_stem"
    assert record["after_stem_px"][0] == 521 and record["after_stem_px"][1] < 405
    assert record["ink_px"]["box"] == 0  # printed options inside the exemption are not ink
    dx = labels["5"]["target_anchor_px"][0] - record["label_region_1000"][0]
    assert labels["5"]["label_bbox"][2] < 560 + dx


@pytest.mark.parametrize("kwargs,decision,review", [
    ({"circle": True}, "box", []),
    ({"marks": [(55, 402)]}, "before_number", []),
    ({"tick": True, "marks": [(560, 402)]}, "ambiguous", ["5"]),  # a red tick still counts as ink
])
def test_choice_circle_margin_and_tick(tmp_path, kwargs, decision, review):
    diagnostics, _labels, routing = build(tmp_path, choice_page(**kwargs), choice_page(), CHOICE)
    assert routing["5"]["decision"] == decision and diagnostics["answer_position_review"] == review


def test_two_up_gutter_is_split_between_stem_end_and_next_number(tmp_path):
    # Q1 answered after its stem; Q2 answered just before its number, inside Q1's box.
    diagnostics, labels, routing = build(tmp_path, two_up_page([(320, 302), (520, 302)]), two_up_page(), TWO_UP)
    assert routing["1"]["decision"] == "box" and routing["2"]["decision"] == "before_number"
    assert routing["2"]["before_number_px"][0] == 301 + (560 - 301 - 1) // 2
    assert diagnostics["answer_position_review"] == []
    assert labels["1"]["region_bbox_1000"] == TWO_UP[0][3]


def test_ambiguous_side_reaches_the_fail_closed_unreadable_path(monkeypatch, tmp_path):
    import asyncio
    from types import SimpleNamespace

    from services.accelerator_business import AcceleratorEvidence
    from services.paper_cache import PaperCache
    from services.template_evidence import EvidenceResult

    cache = PaperCache.from_confirmed_template_pages([{"page_index": 0, "questions": [
        {"question_no": "1", "question_text": "", "question_type": "choice", "answer_position": "unmarked"},
        {"question_no": "2", "question_text": "", "question_type": "choice"}]}])
    evidence_path = tmp_path / "evidence-deadbeefdeadbeefdeadbeefdeadbeef.png"
    evidence_path.write_bytes(b"evidence")
    evidence_result = EvidenceResult(
        page_type_id="page_1", evidence_path=str(evidence_path), question_numbers=["1", "2"],
        blank_question_numbers=[], diagnostics={
            "ownership": "confirmed_bbox_neighbor_clamped", "answer_source_complete": True,
            "incomplete_answer_regions": [], "answer_position_review": ["1"],
            "accelerator": {"evidence_authority": "trusted_client_evidence",
                            "server_source_transform_replayed": False}})

    async def fake_accelerator(**_kwargs):
        return AcceleratorEvidence(evidence_result, state="ready")

    cache._template_evidence_matcher = SimpleNamespace(match_and_build=None)
    cache._accelerator_template_artifacts = {"page_1": {"verified": True}}
    cache._accelerator_template_dir = str(tmp_path)
    cache._accelerator_template_id = "template-1"
    captured = {}

    async def fake_vision(*_args, **kwargs):
        captured.update(kwargs)
        return {"questions": [{"question_number": n, "student_answer": "A", "answer_state": "visible"}
                              for n in ("1", "2")]}

    monkeypatch.setattr(cache, "_recognize_answers_only", fake_vision)
    monkeypatch.setattr("services.accelerator_business.try_accelerated_evidence", fake_accelerator)
    result = asyncio.run(cache._recognize_with_local_evidence(
        "paper.png", teacher_id="7", batch_id="batch-1", paper_id="paper-1"))
    assert "error" not in result
    assert captured["program_unreadable_qns"] == {"1"}


MARKED_BEFORE = [("3", "choice", [300, 200, 330, 230], [255, 195, 298, 235])]


@pytest.mark.parametrize("marks,decision", [([(272, 202)], "box"), ([(720, 202)], "after_stem")])
def test_marked_template_box_before_the_number_owns_that_side(tmp_path, marks, decision):
    # The template's handwriting sat before the number, so the frozen box is there.
    diagnostics, _labels, routing = build(tmp_path, single_row(marks), single_row(), MARKED_BEFORE)
    assert routing["3"]["before_number_px"] is None
    assert routing["3"]["decision"] == decision and diagnostics["answer_position_review"] == []


def test_tall_solve_box_label_is_level_with_its_top_and_short_box_stays_centred(tmp_path):
    from services.template_evidence import EVIDENCE_Q_LABEL_FONT_SIZE
    image = np.full((1000, 1000, 3), 255, np.uint8)
    questions = [("4", "fill", [80, 100, 110, 130], [300, 96, 500, 136]),
                 ("7", "solve", [80, 300, 110, 330], [130, 350, 900, 600])]
    result = evidence.build_full_column_difference_evidence_from_aligned(
        image, np.full(image.shape[:2], 255, np.uint8), image, snapshot(questions, position=None), tmp_path)
    column = result.diagnostics["evidence_columns"][0]
    dy = column["label_layout"]["source_offset_px"][1]
    labels = {p["question_number"]: p for p in column["label_placements"]}
    assert 600 - 350 > 3 * EVIDENCE_Q_LABEL_FONT_SIZE > 136 - 96
    assert labels["7"]["label_align"] == "top" and labels["7"]["label_bbox"][1] - dy == 350
    assert "label_align" not in labels["4"]
    assert abs((labels["4"]["label_bbox"][1] + labels["4"]["label_bbox"][3]) / 2 - dy - 116) <= 1
    assert result.diagnostics["qwen37_label_contract"]["version"] == "qwen37-blue-box-adaptive-slot-labels-v7"


@pytest.mark.parametrize("box,expected_top", [
    ([130, 260, 900, 520], 300),  # drawn up into the previous question: start at its own number
    ([130, 320, 900, 600], 320),  # below its number: start at the box top
])
def test_tall_box_label_never_starts_above_its_own_printed_number(tmp_path, box, expected_top):
    image = np.full((1000, 1000, 3), 255, np.uint8)
    questions = [("6", "fill", [80, 100, 110, 130], [300, 96, 500, 136]),
                 ("7", "choice", [80, 300, 110, 330], box)]
    result = evidence.build_full_column_difference_evidence_from_aligned(
        image, np.full(image.shape[:2], 255, np.uint8), image, snapshot(questions, position=None), tmp_path)
    column = result.diagnostics["evidence_columns"][0]
    dy = column["label_layout"]["source_offset_px"][1]
    label = next(p for p in column["label_placements"] if p["question_number"] == "7")
    assert label["label_align"] == "top" and label["label_bbox"][1] - dy == expected_top
