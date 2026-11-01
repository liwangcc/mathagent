"""Slice 4: local pixel recognizer for filled answer-sheet bubbles.

Every image here is synthetic and generated in-test (white page, printed
bubble outlines and letters, pencil-like fills with a fixed-seed grain).  No
student data, no real papers, no model or network call.  Grid geometry is the
production shape from ``resolve_bubble_grid_pixels`` / ``resolve_bubble_regions``.
"""
import ast
from pathlib import Path

import cv2
import numpy as np
import pytest

import services.answer_sheet_bubble_recognizer as recognizer
from services.answer_sheet_bubble_recognizer import (
    BRIDGE_RELATIVE,
    EMPTY_RELATIVE,
    MARKED_RELATIVE,
    NOISE_FLOOR,
    REAL_MARK,
    choice_row_answer,
    classify_row,
    recognize_bubble_page,
    recognize_bubble_sheet,
)
from services.answer_sheet_layout_contract import resolve_bubble_grid_pixels
from services.answer_sheet_template_registry import AnswerSheetTemplateRegistry
from tests.test_answer_sheet_bubble_grids import _bound_batch, _published, _specs


WIDTH, HEIGHT = 800, 1200
PAGE = "page_1"
ANSWERS = {"1": "C", "2": "A", "3": "B", "4": "AC", "5(1)": "B", "5(2)": "D"}
EPS = 1e-9


def _frozen_row(question_no, bbox, *, sub="", options="ABCD", allow_multiple=False):
    x1, y1, x2, y2 = bbox
    width = (x2 - x1) // len(options)
    cells = [
        {"option": option, "bbox": [x1 + i * width, y1, x1 + (i + 1) * width, y2],
         "cell_size": [width, y2 - y1]}
        for i, option in enumerate(options)
    ]
    return {
        "question_no": question_no, "subquestion_no": sub, "page_id": PAGE,
        "region_id": f"answer_{question_no}", "allow_multiple": allow_multiple,
        "grid_order": "left_to_right", "grid_bbox": [x1, y1, x1 + width * len(options), y2],
        "cells": cells, "teacher_confirmed": True,
    }


def _resolved(page_size=(WIDTH, HEIGHT)) -> dict:
    """Rows 1-3 single choice, 4 multiple choice, 5(1)/5(2) small questions of Q5."""
    rows = [
        _frozen_row("1", [100, 100, 500, 150]),
        _frozen_row("2", [100, 200, 500, 250]),
        _frozen_row("3", [100, 300, 500, 350]),
        _frozen_row("4", [100, 400, 500, 450], allow_multiple=True),
        _frozen_row("5", [100, 500, 500, 550], sub="1"),
        _frozen_row("5", [100, 560, 500, 610], sub="2"),
    ]
    return resolve_bubble_grid_pixels(rows, {PAGE: page_size})


ROWS = _resolved()


def _box(key, option, rows=ROWS):
    return next(cell["pixel_box"] for cell in rows[key]["cells"] if cell["option"] == option)


def _sheet(rows=ROWS, *, printed=True, paper=255, size=(WIDTH, HEIGHT)) -> np.ndarray:
    """A blank registered page with printed bubble outlines and option letters."""
    image = np.full((size[1], size[0], 3), paper, dtype=np.uint8)
    if not printed:
        return image
    for row in rows.values():
        for cell in row["cells"]:
            x1, y1, x2, y2 = cell["pixel_box"]
            w, h = x2 - x1, y2 - y1
            cv2.rectangle(image, (x1 + w // 12, y1 + h // 12), (x2 - 1 - w // 12, y2 - 1 - h // 12),
                          (110, 110, 110), 1)
            (tw, th), _ = cv2.getTextSize(cell["option"], cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            cv2.putText(image, cell["option"], ((x1 + x2 - tw) // 2, (y1 + y2 + th) // 2),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (90, 90, 90), 1, cv2.LINE_AA)
    return image


def _fill(image, key, option, *, gray=40, grain=None, dx=0.0, dy=0.0, scale=1.0, seed=7, rows=ROWS):
    """Pencil fill: an ellipse inside the bubble; ink never lightens the print."""
    x1, y1, x2, y2 = _box(key, option, rows)
    w, h = x2 - x1, y2 - y1
    center = (int(round((x1 + x2) / 2 + dx * w)), int(round((y1 + y2) / 2 + dy * h)))
    axes = (int(0.38 * w * scale), int(0.36 * h * scale))
    mask = np.zeros(image.shape[:2], dtype=np.uint8)
    cv2.ellipse(mask, center, axes, 0, 0, 360, 255, -1)
    darkness = np.full(image.shape[:2], 255 - gray, dtype=np.float64)
    if grain is not None:
        darkness *= np.random.default_rng(seed).uniform(grain, 1.0, image.shape[:2])
    level = np.clip(np.rint(255 - darkness), 0, 255).astype(np.uint8)
    inside = mask > 0
    image[inside] = np.minimum(image[inside], level[inside][:, None])
    return image


def _paint(image, box, gray):
    """Uniform gray over a whole pixel box (exact ink for boundary straddles)."""
    x1, y1, x2, y2 = box
    image[y1:y2, x1:x2] = gray
    return image


def _run(image, rows=ROWS, answers=ANSWERS):
    return recognize_bubble_page(image, list(rows.values()), answers)


def _outcomes(results):
    return {key: result["outcome"] for key, result in results.items()}


def _states(result):
    return {cell["option"]: cell["state"] for cell in result["cells"]}


# --- the five-class decision table (spec §2.3, answer = C) --------------------

@pytest.mark.parametrize("marks, outcome, label", [
    ({"C": {}}, "correct", "对"),
    ({"A": {}}, "wrong", "错"),
    ({}, "blank", "空答"),
    ({"C": {}, "D": {}}, "multiple", "多涂"),
    # Partly erased A next to C: residue ~45% of the student's own fill.
    ({"A": {"gray": 150, "grain": 0.75}, "C": {}}, "needs_review", "needs_review"),
])
def test_decision_table_for_answer_c(marks, outcome, label):
    image = _sheet()
    for option, style in marks.items():
        _fill(image, "1", option, **style)
    result = _run(image)["1"]
    assert (result["outcome"], result["label"]) == (outcome, label)
    assert result["needs_review"] is (outcome == "needs_review")
    assert result["answer_options"] == ["C"]


def test_decided_outcomes_carry_the_cell_diff_as_evidence():
    image = _fill(_sheet(), "1", "A")
    result = _run(image)["1"]
    assert result["marked_options"] == ["A"]
    assert result["missing_options"] == ["C"] and result["extra_options"] == ["A"]
    assert [cell["option"] for cell in result["cells"]] == ["A", "B", "C", "D"]
    assert all(cell["pixel_box"] == _box("1", cell["option"]) for cell in result["cells"])
    assert _states(result) == {"A": "marked", "B": "empty", "C": "empty", "D": "empty"}
    assert result["recognizer_version"] == recognizer.RECOGNIZER_VERSION


def test_every_class_on_one_page_is_independent_per_row():
    image = _sheet()
    _fill(image, "1", "C")                      # 对
    _fill(image, "2", "B")                      # 错 (answer A)
    _fill(image, "3", "B"); _fill(image, "3", "D")  # 多涂
    _fill(image, "4", "A"); _fill(image, "4", "C")  # 对 (legal multiple choice)
    _fill(image, "5(1)", "B")                   # 对; 5(2) left blank: 空答
    assert _outcomes(_run(image)) == {
        "1": "correct", "2": "wrong", "3": "multiple", "4": "correct",
        "5(1)": "correct", "5(2)": "blank",
    }


# --- light ink / heavy ink / erasure / off-center -----------------------------

def test_light_but_real_mark_on_the_answer_is_correct_not_review():
    image = _fill(_sheet(), "1", "C", gray=200, grain=0.6)
    result = _run(image)["1"]
    assert result["outcome"] == "correct" and result["needs_review"] is False
    light = next(cell for cell in result["cells"] if cell["option"] == "C")
    # Genuinely light: well under half of a normal pencil fill's darkness.
    assert REAL_MARK <= light["excess"] < 0.2


def test_light_but_real_mark_on_another_option_is_wrong():
    image = _fill(_sheet(), "1", "B", gray=200, grain=0.6)
    assert _run(image)["1"]["outcome"] == "wrong"


def test_faint_mark_is_never_read_as_blank():
    image = _fill(_sheet(), "1", "C", gray=232)
    result = _run(image)["1"]
    assert result["outcome"] == "needs_review" and result["reason"] == "faint_marks"
    assert result["uncertain_options"] == ["C"]


def test_heavy_ink_bleeding_into_the_neighbour_is_not_multiple():
    image = _fill(_sheet(), "1", "C", gray=5, scale=1.4)
    result = _run(image)["1"]
    assert result["outcome"] == "correct"
    assert _states(result) == {"A": "empty", "B": "empty", "C": "marked", "D": "empty"}


def test_heavy_answer_next_to_light_student_elsewhere_uses_each_rows_own_ink():
    image = _sheet()
    _fill(image, "1", "C", gray=5)
    _fill(image, "2", "A", gray=205, grain=0.6)
    assert _outcomes(_run(image))["1"] == "correct"
    assert _outcomes(_run(image))["2"] == "correct"


def test_cleanly_erased_attempt_keeps_the_final_choice():
    image = _sheet()
    _fill(image, "1", "A", gray=238, grain=0.3)   # erased residue
    _fill(image, "1", "C")
    result = _run(image)["1"]
    assert result["outcome"] == "correct"
    assert _states(result)["A"] == "empty"


def test_incompletely_erased_attempt_goes_to_the_teacher():
    image = _sheet()
    _fill(image, "1", "A", gray=150, grain=0.75)
    _fill(image, "1", "C")
    result = _run(image)["1"]
    assert result["outcome"] == "needs_review" and result["reason"] == "uncertain_marks"
    assert result["marked_options"] == ["C"] and result["uncertain_options"] == ["A"]
    assert result["missing_options"] == [] and result["extra_options"] == []


@pytest.mark.parametrize("dx, dy", [(0.22, 0.0), (-0.22, 0.0), (0.0, 0.2), (0.18, -0.18)])
def test_off_center_fill_still_counts_for_its_own_cell(dx, dy):
    image = _fill(_sheet(), "1", "C", dx=dx, dy=dy)
    result = _run(image)["1"]
    assert result["outcome"] == "correct"
    assert _states(result) == {"A": "empty", "B": "empty", "C": "marked", "D": "empty"}


@pytest.mark.parametrize("shift", [(3, 0), (-3, 0), (0, 3), (2, -2)])
def test_residual_registration_offset_does_not_change_outcomes(shift):
    image = _sheet()
    _fill(image, "1", "C"); _fill(image, "2", "D"); _fill(image, "4", "A"); _fill(image, "4", "C")
    expected = _outcomes(_run(image))
    moved = np.full_like(image, 255)
    dx, dy = shift
    moved[max(dy, 0):HEIGHT + min(dy, 0), max(dx, 0):WIDTH + min(dx, 0)] = \
        image[max(-dy, 0):HEIGHT + min(-dy, 0), max(-dx, 0):WIDTH + min(-dx, 0)]
    assert _outcomes(_run(moved)) == expected


# --- stray ink / uniform ink --------------------------------------------------

def test_one_blot_across_two_cells_is_review_not_multiple():
    image = _sheet()
    (bx1, by1, bx2, by2), (cx1, _, cx2, cy2) = _box("1", "B"), _box("1", "C")
    cv2.ellipse(image, ((bx1 + cx2) // 2, (by1 + by2) // 2), ((cx2 - bx1) // 2 - 6, (by2 - by1) // 2 - 6),
                0, 0, 360, (30, 30, 30), -1)
    result = _run(image)["1"]
    assert result["outcome"] == "needs_review" and result["reason"] == "ink_bridges_cells"


def test_two_separate_fills_are_multiple_not_a_bridge():
    image = _sheet()
    _fill(image, "1", "B"); _fill(image, "1", "C")
    result = _run(image)["1"]
    assert result["outcome"] == "multiple"
    # Fill overshoot reaches the B|C gap a little; far from joining the cells.
    fills = [cell["excess"] for cell in result["cells"] if cell["option"] in "BC"]
    assert result["measures"]["bridge_excess"][1] < 0.25 * BRIDGE_RELATIVE * min(fills)


def test_heavy_stroke_across_the_whole_row_is_review():
    image = _sheet()
    x1, y1, _, y2 = _box("1", "A")
    x2 = _box("1", "D")[2]
    cv2.line(image, (x1 + 2, (y1 + y2) // 2), (x2 - 2, (y1 + y2) // 2), (20, 20, 20), 7)
    result = _run(image)["1"]
    assert result["outcome"] == "needs_review" and result["reason"] == "row_uniformly_inked"


def test_every_option_filled_is_review_not_blank():
    image = _sheet()
    for option in "ABCD":
        _fill(image, "1", option)
    result = _run(image)["1"]
    assert result["outcome"] == "needs_review" and result["reason"] == "row_uniformly_inked"


def test_light_shadow_over_a_row_does_not_hide_a_clear_fill():
    image = _sheet()
    _fill(image, "1", "C")
    x1, y1, _, y2 = _box("1", "A")
    band = image[y1:y2, x1:_box("1", "D")[2]].astype(np.int16) - 8
    image[y1:y2, x1:_box("1", "D")[2]] = np.clip(band, 0, 255).astype(np.uint8)
    result = _run(image)["1"]
    assert result["outcome"] == "correct"
    assert 0 < result["measures"]["row_lift"] < NOISE_FLOOR


# --- small-question granularity -----------------------------------------------

@pytest.mark.parametrize("first, second, expected", [
    ("B", "D", ("correct", "correct")),
    ("B", "B", ("correct", "wrong")),
    ("D", "D", ("wrong", "correct")),
    (None, "D", ("blank", "correct")),
    ("B", None, ("correct", "blank")),
])
def test_small_questions_are_decided_one_row_at_a_time(first, second, expected):
    image = _sheet()
    if first:
        _fill(image, "5(1)", first)
    if second:
        _fill(image, "5(2)", second)
    results = _run(image)
    assert (results["5(1)"]["outcome"], results["5(2)"]["outcome"]) == expected
    assert (results["5(1)"]["answer_options"], results["5(2)"]["answer_options"]) == (["B"], ["D"])


# --- multiple-choice contract (allow_multiple) --------------------------------

@pytest.mark.parametrize("marks, outcome, missing, extra", [
    ("AC", "correct", [], []),
    ("A", "wrong", ["C"], []),
    ("ABC", "wrong", [], ["B"]),
    ("", "blank", ["A", "C"], []),     # answer cells empty, nothing extra
])
def test_multiple_choice_compares_the_whole_fill_state(marks, outcome, missing, extra):
    image = _sheet()
    for option in marks:
        _fill(image, "4", option)
    result = _run(image)["4"]
    assert result["outcome"] == outcome
    assert (result["missing_options"], result["extra_options"]) == (missing, extra)
    assert result["outcome"] != "multiple"


# --- tolerance boundaries on the pure decision --------------------------------

def _decide(inks, answer="C", *, allow_multiple=False, page_floor=0.0, bridges=None):
    return classify_row(inks, options="ABCD", answer_options=answer,
                        allow_multiple=allow_multiple, page_floor=page_floor,
                        bridge_excess=bridges)


@pytest.mark.parametrize("top, outcome, reason", [
    (NOISE_FLOOR - EPS, "blank", "no_marks"),
    (NOISE_FLOOR, "needs_review", "faint_marks"),
    (REAL_MARK - EPS, "needs_review", "faint_marks"),
    (REAL_MARK, "correct", "matches_answer_fill_state"),
])
def test_noise_floor_and_real_mark_boundaries(top, outcome, reason):
    result = _decide([0.0, 0.0, top, 0.0])
    assert (result["outcome"], result["reason"]) == (outcome, reason)


@pytest.mark.parametrize("second, outcome", [
    (EMPTY_RELATIVE * 0.5, "correct"),          # exactly the empty share: residue
    (EMPTY_RELATIVE * 0.5 + EPS, "needs_review"),
    (MARKED_RELATIVE * 0.5 - EPS, "needs_review"),
    (MARKED_RELATIVE * 0.5, "multiple"),        # exactly the marked share: a second fill
])
def test_relative_boundaries_against_the_students_own_mark(second, outcome):
    assert _decide([0.0, 0.0, 0.5, second])["outcome"] == outcome


def test_relative_empty_share_needs_the_noise_floor_too():
    # 0.049 is 49% of a faint 0.1 top mark, but below the noise floor: empty.
    assert _decide([0.0, 0.0, 0.1, NOISE_FLOOR - EPS])["outcome"] == "correct"


@pytest.mark.parametrize("lift, fill, outcome, reason", [
    (REAL_MARK - EPS, 0.6, "correct", "matches_answer_fill_state"),
    (REAL_MARK, 0.6, "needs_review", "row_uniformly_inked"),
    (NOISE_FLOOR - EPS, None, "blank", "no_marks"),
    (NOISE_FLOOR, None, "needs_review", "row_faint_uniform_ink"),
])
def test_row_lift_boundaries(lift, fill, outcome, reason):
    inks = [lift, lift, fill if fill is not None else lift, lift]
    result = _decide(inks)
    assert (result["outcome"], result["reason"]) == (outcome, reason)


@pytest.mark.parametrize("bridge, outcome", [
    (BRIDGE_RELATIVE * 0.5 - EPS, "multiple"),
    (BRIDGE_RELATIVE * 0.5, "needs_review"),
])
def test_bridge_boundary_between_two_marked_cells(bridge, outcome):
    result = _decide([0.0, 0.5, 0.5, 0.0], bridges=[0.0, bridge, 0.0])
    assert result["outcome"] == outcome


def test_bridge_needs_the_noise_floor_and_two_inked_cells():
    # One inked cell: ink in its gaps is overflow, not a join.
    assert _decide([0.0, 0.0, 0.5, 0.0], bridges=[0.0, 0.3, 0.3])["outcome"] == "correct"
    # Two faint cells: the gap reaches half their excess but not the noise
    # floor, so the row is faint (review), not a bridge.
    result = _decide([0.0, 0.08, 0.08, 0.0], "B", bridges=[0.0, NOISE_FLOOR - EPS, 0.0])
    assert result["reason"] == "faint_marks"


def test_uncertainty_that_cannot_change_the_verdict_is_decided():
    # Two clear fills on a single choice: 多涂 whatever the uncertain cell is.
    result = _decide([0.0, 0.5, 0.5, 0.2])
    assert result["outcome"] == "multiple" and result["uncertain_options"] == ["D"]
    # Multiple choice with a chosen wrong option: 错 (extra) either way.
    result = _decide([0.5, 0.5, 0.0, 0.2], "AC", allow_multiple=True)
    assert result["outcome"] == "wrong" and result["extra_options"] == ["B"]
    # ... but a missing-only reading vs an extra reading differs for scoring.
    result = _decide([0.5, 0.2, 0.0, 0.0], "AC", allow_multiple=True)
    assert result["outcome"] == "needs_review" and result["reason"] == "uncertain_marks"


# --- the same boundaries measured from pixels ---------------------------------

@pytest.mark.parametrize("gray, outcome", [
    (243, "blank"),          # 12/255 = 0.047 < noise floor
    (242, "needs_review"),   # 13/255 = 0.051 faint
    (230, "needs_review"),   # 25/255 = 0.098 faint
    (229, "correct"),        # 26/255 = 0.102 real (light) mark
])
def test_pixel_straddle_of_the_absolute_bands(gray, outcome):
    image = _paint(_sheet(printed=False), _box("1", "C"), gray)
    assert _run(image)["1"]["outcome"] == outcome


@pytest.mark.parametrize("gray, outcome", [
    (196, "correct"),        # 59/255 <= 0.3 x 200/255
    (194, "needs_review"),   # 61/255
    (136, "needs_review"),   # 119/255 < 0.6 x 200/255
    (134, "multiple"),       # 121/255
])
def test_pixel_straddle_of_the_relative_bands(gray, outcome):
    # Inner areas only: two separate fills, the C|D gap stays paper.
    image = _sheet(printed=False)
    _paint(image, recognizer.inner_box(_box("1", "C")), 55)
    _paint(image, recognizer.inner_box(_box("1", "D")), gray)
    assert _run(image)["1"]["outcome"] == outcome


@pytest.mark.parametrize("gray, fill, outcome", [
    (230, 0, "correct"),        # lift 0.098: tint, clear fill decided
    (229, 0, "needs_review"),   # lift 0.102: every cell really inked
    (243, None, "blank"),       # lift 0.047
    (242, None, "needs_review"),  # lift 0.051 on an otherwise blank row
])
def test_pixel_straddle_of_the_row_lift(gray, fill, outcome):
    image = _sheet(printed=False)
    for option in "ABCD":
        _paint(image, _box("1", option), gray)
    if fill is not None:
        _paint(image, _box("1", "C"), fill)
    assert _run(image)["1"]["outcome"] == outcome


@pytest.mark.parametrize("gray, expected", [
    (191, "needs_review"),   # 64/255 = 0.251: no print-only cell on the page
    (192, "correct"),        # 63/255 = 0.247: still a plausible print level
])
def test_pixel_straddle_of_the_print_ceiling(gray, expected):
    image = _sheet(printed=False)
    for row in ROWS.values():
        for cell in row["cells"]:
            _paint(image, cell["pixel_box"], gray)
    _paint(image, _box("1", "C"), 0)
    results = _run(image)
    assert results["1"]["outcome"] == expected
    if expected == "needs_review":
        assert {result["reason"] for result in results.values()} == {"page_floor_implausible"}
    else:
        assert results["2"]["outcome"] == "blank"


@pytest.mark.parametrize("paper, outcome", [(95, "needs_review"), (96, "correct")])
def test_pixel_straddle_of_the_paper_level(paper, outcome):
    image = _paint(_sheet(printed=False, paper=paper), _box("1", "C"), 0)
    results = _run(image)
    assert results["1"]["outcome"] == outcome
    assert results["1"]["measures"]["paper_level"] == paper
    if outcome == "needs_review":
        assert {result["reason"] for result in results.values()} == {"page_unreadable"}


def test_inner_area_boundary_makes_tiny_cells_unreadable():
    rows = resolve_bubble_grid_pixels([
        {**_frozen_row("1", [100, 100, 108, 104], options="AB")},     # 4x4 px cells: 16
        {**_frozen_row("2", [100, 200, 106, 205], options="AB")},     # 3x5 px cells: 15
    ], {PAGE: (1000, 1000)})
    image = np.full((1000, 1000, 3), 255, dtype=np.uint8)
    _paint(image, rows["1"]["cells"][0]["pixel_box"], 0)
    results = recognize_bubble_page(image, list(rows.values()), {"1": "A", "2": "A"})
    assert results["1"]["outcome"] == "correct"
    assert (results["2"]["outcome"], results["2"]["reason"]) == ("needs_review", "cell_too_small")


# --- determinism --------------------------------------------------------------

def test_identical_pixels_give_identical_results():
    image = _sheet()
    _fill(image, "1", "C", gray=200, grain=0.6)
    _fill(image, "2", "A", gray=150, grain=0.75); _fill(image, "2", "B")
    _fill(image, "4", "A"); _fill(image, "4", "C")
    before = image.copy()
    first = _run(image)
    assert _run(image) == first
    assert _run(image.copy()) == first
    reversed_rows = recognize_bubble_page(image.copy(), list(reversed(list(ROWS.values()))), ANSWERS)
    assert {key: reversed_rows[key] for key in first} == first
    assert np.array_equal(image, before)  # the student page is never modified


# --- fail-closed contract -------------------------------------------------------

@pytest.mark.parametrize("image", [
    np.zeros((HEIGHT, WIDTH), dtype=np.uint8),
    np.zeros((HEIGHT, WIDTH, 3), dtype=np.float32),
    np.zeros((HEIGHT, WIDTH, 4), dtype=np.uint8),
    [[0]],
])
def test_non_rgb_page_is_rejected(image):
    with pytest.raises(ValueError, match="RGB uint8"):
        recognize_bubble_page(image, list(ROWS.values()), ANSWERS)


def test_unregistered_page_size_is_rejected_not_resampled():
    image = np.full((HEIGHT // 2, WIDTH // 2, 3), 255, dtype=np.uint8)
    with pytest.raises(ValueError, match="配准"):
        recognize_bubble_page(image, list(ROWS.values()), ANSWERS)


def test_tampered_pixel_box_is_rejected():
    row = {**ROWS["1"], "cells": [dict(cell) for cell in ROWS["1"]["cells"]]}
    row["cells"][2]["pixel_box"] = [value + 1 for value in row["cells"][2]["pixel_box"]]
    with pytest.raises(ValueError, match="像素框与模板几何不一致"):
        recognize_bubble_page(_sheet(), [row], {"1": "C"})


@pytest.mark.parametrize("answers, message", [
    ({}, "缺少标准答案涂态"),
    ({"1": "E"}, "不重复的选项"),
    ({"1": "CC"}, "不重复的选项"),
    ({"1": ""}, "不重复的选项"),
    ({"1": "CD"}, "恰好一个选项"),
    ({"1": ["c"]}, "不重复的选项"),
])
def test_answer_fill_state_out_of_contract_is_rejected(answers, message):
    with pytest.raises(ValueError, match=message):
        recognize_bubble_page(_sheet(), [ROWS["1"]], answers)


def test_rows_from_two_pages_or_duplicated_keys_are_rejected():
    other = {**ROWS["2"], "page_id": "page_2"}
    with pytest.raises(ValueError, match="同一页"):
        recognize_bubble_page(_sheet(), [ROWS["1"], other], ANSWERS)
    with pytest.raises(ValueError, match="重复"):
        recognize_bubble_page(_sheet(), [ROWS["1"], ROWS["1"]], ANSWERS)


def test_page_floor_above_the_row_is_rejected():
    with pytest.raises(ValueError, match="页面基准"):
        _decide([0.1, 0.1, 0.5, 0.1], page_floor=0.2)


# --- paper-template answers per grid row ----------------------------------------

def test_whole_question_uses_the_confirmed_standard_answer():
    spec = {"question_no": "1", "question_type": "choice", "choice_mode": "single", "standard_answer": "C"}
    assert choice_row_answer(spec) == ["C"]
    multiple = {**spec, "choice_mode": "multiple", "standard_answer": "CA"}
    assert choice_row_answer(multiple) == ["A", "C"]


def test_small_questions_need_one_confirmed_answer_each():
    spec = {"question_no": "2", "question_type": "choice", "choice_mode": "single",
            "standard_answer": "A", "subquestion_count": 2}
    with pytest.raises(ValueError, match=r"2\(1\)题的小问标准答案未逐小问确认"):
        choice_row_answer(spec, "1")
    confirmed = {**spec, "answer_slots": ["B", "D"]}
    assert (choice_row_answer(confirmed, "1"), choice_row_answer(confirmed, "2")) == (["B"], ["D"])
    with pytest.raises(ValueError, match="小问结构"):
        choice_row_answer(confirmed, "3")
    with pytest.raises(ValueError, match="小问结构"):
        choice_row_answer(confirmed, "")


@pytest.mark.parametrize("spec", [
    {"question_no": "1", "question_type": "choice", "choice_mode": "unknown", "standard_answer": "C"},
    {"question_no": "1", "question_type": "choice", "choice_mode": "single", "standard_answer": "AC"},
    {"question_no": "1", "question_type": "choice", "choice_mode": "multiple", "standard_answer": "C"},
    {"question_no": "1", "question_type": "choice", "choice_mode": "single", "standard_answer": "C?"},
    {"question_no": "1", "question_type": "fill", "standard_answer": "C"},
])
def test_answer_contract_mismatch_fails_closed(spec):
    with pytest.raises(ValueError):
        choice_row_answer(spec)


# --- end to end through the frozen answer-sheet template -----------------------

def _registry_sheet(tmp_path, specs):
    registry = AnswerSheetTemplateRegistry(tmp_path / "registry")
    _source, template = _published(tmp_path, registry)
    batch = _bound_batch(tmp_path, registry, template["template_code"], specs=specs)
    return batch, registry.resolve_bubble_regions(batch)


def test_sheet_recognition_through_the_confirmed_template(tmp_path):
    specs = _specs()
    specs[1] = {**specs[1], "answer_slots": ["B", "D"]}
    batch, resolved = _registry_sheet(tmp_path, specs)
    rows = resolved["questions"]
    image = _sheet(rows)
    for key, option in (("1", "C"), ("2(1)", "B"), ("2(2)", "B")):
        _fill(image, key, option, rows=rows)

    result = recognize_bubble_sheet({"page_1": image}, resolved, batch.answer_spec_snapshot)
    assert result["template_code"] == batch.shared_template_code
    assert result["template_fingerprint"] == batch.shared_template_hash
    assert list(result["questions"]) == ["1", "2(1)", "2(2)"]
    assert {key: item["outcome"] for key, item in result["questions"].items()} == {
        "1": "correct", "2(1)": "correct", "2(2)": "wrong",
    }
    assert result["needs_review"] == []


def test_sheet_recognition_fails_closed_without_small_question_answers(tmp_path):
    batch, resolved = _registry_sheet(tmp_path, _specs())
    with pytest.raises(ValueError, match="answer_slots"):
        recognize_bubble_sheet({"page_1": _sheet(resolved["questions"])}, resolved, batch.answer_spec_snapshot)


def test_sheet_recognition_fails_closed_on_missing_page_or_unbound_geometry(tmp_path):
    specs = _specs()
    specs[1] = {**specs[1], "answer_slots": ["B", "D"]}
    batch, resolved = _registry_sheet(tmp_path, specs)
    with pytest.raises(ValueError, match="page_1"):
        recognize_bubble_sheet({}, resolved, batch.answer_spec_snapshot)
    with pytest.raises(ValueError, match="已发布"):
        recognize_bubble_sheet({"page_1": _sheet(resolved["questions"])},
                               {"questions": resolved["questions"]}, batch.answer_spec_snapshot)
    mismatched = [dict(spec) for spec in specs]
    mismatched[0]["choice_mode"] = "multiple"
    mismatched[0]["standard_answer"] = "AC"
    with pytest.raises(ValueError, match="单选/多选"):
        recognize_bubble_sheet({"page_1": _sheet(resolved["questions"])}, resolved, mismatched)


# --- purity -------------------------------------------------------------------

def test_recognizer_imports_no_model_network_or_io_code():
    tree = ast.parse(Path(recognizer.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    assert imported == {
        "__future__", "itertools", "typing", "numpy",
        "services.answer_sheet_layout_contract", "services.grading_contract",
    }
    source = Path(recognizer.__file__).read_text(encoding="utf-8")
    assert "open(" not in source and "import cv2" not in source
