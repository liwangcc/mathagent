"""Marked-template pixel gate: teacher geometry re-rendered on rectified pages.

Synthetic A4 portrait pages (W != H) with a printed grid, a printed figure and
printed text on both pages. The test draws student strokes at pixel positions
it computes itself (x*W/1000, y*H/1000), independent of the module under test,
so an axis swap or a wrong scale cannot pass silently.
"""
import asyncio
import json
from unittest.mock import AsyncMock

import cv2
import numpy as np
import pytest

from services import drawing_grading as drawing
from services import drawing_pixel_gate as gate
from services.drawing_reference import normalize_reference

W, H = 1240, 1754
REGION = [100, 300, 900, 700]
QUESTION = {"question_no": "5", "answer_kind": "drawing", "drawing_regions": [REGION]}
PENCIL = (70, 70, 70)


def px(x, y):
    """Normalized template coordinates to page pixels, computed independently."""
    return round(x * W / 1000), round(y * H / 1000)


def reference(strokes):
    return normalize_reference({"version": "drawing-reference-v1", "coordinate_space": "normalized_1000_v1",
                                "comparison_mode": "fixed", "variants": [{"strokes": strokes}],
                                "context_regions": []}, trusted_confirmed=True)


def printed_page():
    page = np.full((H, W, 3), 250, np.uint8)
    left, top = px(REGION[0], REGION[1])
    right, bottom = px(REGION[2], REGION[3])
    for x in range(left, right, 40):
        cv2.line(page, (x, top), (x, bottom), (200, 200, 200), 1)
    for y in range(top, bottom, 40):
        cv2.line(page, (left, y), (right, y), (200, 200, 200), 1)
    cv2.polylines(page, [np.array([px(150, 330), px(260, 330), px(150, 420)], np.int32)], True, (40, 40, 40), 2)
    cv2.putText(page, "A", px(140, 325), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (30, 30, 30), 2)
    return page


LINE = [[300, 500], [700, 600]]
CIRCLE_CENTER, CIRCLE_RADIUS_PX = [500, 520], 150
SHADE = [[600, 360], [850, 360], [850, 460], [600, 460]]


def line_ref():
    return reference([{"kind": "polyline", "points": LINE}])


def circle_ref():
    # Round in pixels: each axis is scaled by its own page dimension.
    radii = [CIRCLE_RADIUS_PX * 1000 / W, CIRCLE_RADIUS_PX * 1000 / H]
    return reference([{"kind": "ellipse", "center": CIRCLE_CENTER, "radii": radii, "fill": False}])


def shade_ref():
    return reference([{"kind": "polygon", "points": SHADE}])


def measure(student, ref, template):
    return gate.evaluate_pixel_gate(student, QUESTION, ref, template_mode="marked",
                                    marked_template=template)["variants"][0]


@pytest.mark.parametrize("case,expected", [("exact", "pass"), ("one_px_registration", "pass"),
                                           ("offset_4mm", "fail"), ("blank", "fail"), ("transposed", "fail")])
def test_teacher_line_is_scored_on_a_marked_template(case, expected):
    template = printed_page()
    student = printed_page()
    if case == "one_px_registration":
        student = np.roll(student, 1, axis=(0, 1))
    if case != "blank":
        a, b = (px(*LINE[0]), px(*LINE[1]))
        if case == "offset_4mm":
            a, b = (a[0], a[1] + 30), (b[0], b[1] + 30)
        elif case == "transposed":
            # The wrong conversion x*H/1000, y*W/1000 lands somewhere else.
            a = (round(LINE[0][0] * H / 1000), round(LINE[0][1] * W / 1000))
            b = (round(LINE[1][0] * H / 1000), round(LINE[1][1] * W / 1000))
        cv2.line(student, a, b, PENCIL, 3)
    item = measure(student, line_ref(), template)
    assert item["status"] == expected, item
    assert item["metrics"]["mode"] == "registered_template_difference"
    # Printed grid, figure and text never count as the student's strokes.
    if case == "blank":
        assert item["metrics"]["new_ink_pixels"] == 0 and item["score"] == 0


@pytest.mark.parametrize("case,expected", [("exact", "pass"), ("hand_wobble", "pass"),
                                           ("radius_plus_20", "fail"), ("quarter_missing", "fail")])
def test_teacher_circle_is_round_in_pixels_and_scored(case, expected):
    template, student = printed_page(), printed_page()
    center = px(*CIRCLE_CENTER)
    radius = round(CIRCLE_RADIUS_PX * (1.2 if case == "radius_plus_20" else 1))
    angles = np.linspace(0, 2 * np.pi, 361)
    if case == "quarter_missing":
        angles = angles[angles < 1.5 * np.pi]
    wobble = 8 * np.sin(angles * 9) if case == "hand_wobble" else 0
    points = np.column_stack((center[0] + (radius + wobble) * np.cos(angles),
                              center[1] + (radius + wobble) * np.sin(angles)))
    cv2.polylines(student, [np.rint(points).astype(np.int32)], False, PENCIL, 3)
    item = measure(student, circle_ref(), template)
    assert item["status"] == expected, item


@pytest.mark.parametrize("case,expected", [("hatched", "pass"), ("other_area", "fail"), ("blank", "fail")])
def test_teacher_shading_is_scored_against_hatching(case, expected):
    template, student = printed_page(), printed_page()
    left, top = px(*SHADE[0])
    right, bottom = px(*SHADE[2])
    if case == "other_area":
        left, right = left - 400, right - 400
    if case != "blank":
        for offset in range(-(bottom - top), right - left, 16):  # 45-degree hatching, 16 px apart
            cv2.line(student, (left + offset, bottom), (left + offset + (bottom - top), top), PENCIL, 2)
        hatch = np.zeros((H, W), bool)
        hatch[top:bottom, left:right] = True
        student[~hatch] = printed_page()[~hatch]
    item = measure(student, shade_ref(), template)
    assert item["status"] == expected, item


def test_pencil_drawn_along_a_light_printed_grid_line_is_still_student_ink():
    template, student = printed_page(), printed_page()
    left, top = px(REGION[0], REGION[1])
    y = top + 40 * 4  # exactly on a printed grid line
    start = (left + 40 * 3, y)
    end = (left + 40 * 12, y)
    cv2.line(student, start, end, PENCIL, 3)
    ref = reference([{"kind": "polyline", "points": [[start[0] * 1000 / W, y * 1000 / H],
                                                     [end[0] * 1000 / W, y * 1000 / H]]}])
    item = measure(student, ref, template)
    assert item["status"] == "pass", item
    assert item["metrics"]["measured_reference_line_percent"] > 90


def test_teacher_marks_on_the_template_read_as_blank_paper():
    # The template page carries the teacher's own pen work and notes. They are
    # never the standard and never count against or for the student.
    template, student = printed_page(), printed_page()
    cv2.putText(template, "key", px(700, 650), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (20, 20, 20), 3)
    cv2.circle(template, px(250, 650), 30, (20, 20, 20), 3)
    cv2.line(student, px(*LINE[0]), px(*LINE[1]), PENCIL, 3)
    assert measure(student, line_ref(), template)["status"] == "pass"
    assert measure(printed_page(), line_ref(), template)["status"] == "fail"


def test_answer_drawn_on_the_template_is_unavailable_not_a_false_zero():
    # Where both pages are dark along the standard the student's strokes cannot
    # be told from the template's, so the model check decides alone.
    template = printed_page()
    cv2.line(template, px(*LINE[0]), px(*LINE[1]), (20, 20, 20), 4)
    student = printed_page()
    cv2.line(student, px(*LINE[0]), px(*LINE[1]), PENCIL, 3)
    item = measure(student, line_ref(), template)
    assert item["status"] == "unavailable" and item["score"] is None
    # Nothing drawn along a template-only answer is still measurable.
    assert measure(printed_page(), line_ref(), template)["status"] == "fail"


def test_standard_traced_on_printed_ink_is_unavailable():
    template, student = printed_page(), printed_page()
    edge = reference([{"kind": "polyline", "points": [[150, 330], [260, 330]]}])  # printed triangle edge
    assert measure(student, edge, template)["status"] == "unavailable"


@pytest.mark.parametrize("shift", [0, 1, 2, 3, 4, 6])
def test_registration_error_never_becomes_a_false_zero_or_a_false_pass(shift):
    # Diagonal shifts up to 3 px (4.2 px) are absorbed; anything larger is
    # unavailable instead of turning printed residue into student strokes.
    template = printed_page()
    drawn = np.roll(printed_page(), shift, axis=(0, 1))
    cv2.line(drawn, px(*LINE[0]), px(*LINE[1]), PENCIL, 3)
    blank = np.roll(printed_page(), shift, axis=(0, 1))
    expected = ("pass", "fail") if shift <= 3 else ("unavailable", "unavailable")
    assert (measure(drawn, line_ref(), template)["status"], measure(blank, line_ref(), template)["status"]) == expected


def test_line_starting_on_a_printed_vertex_keeps_its_stroke():
    template, student = printed_page(), printed_page()
    ref = reference([{"kind": "polyline", "points": [[260, 330], [600, 450]]}])
    cv2.line(student, px(260, 330), px(600, 450), PENCIL, 3)
    assert measure(student, ref, template)["status"] == "pass"
    assert measure(printed_page(), ref, template)["status"] == "fail"


def test_misregistered_page_is_unavailable_not_scored():
    template = printed_page()
    student = np.roll(printed_page(), 9, axis=(0, 1))
    cv2.line(student, px(*LINE[0]), px(*LINE[1]), PENCIL, 3)
    item = measure(student, line_ref(), template)
    assert item["status"] == "unavailable" and "registered" in item["metrics"]["detail"]


@pytest.mark.parametrize("template", [None, np.zeros((H, W - 1, 3), np.uint8)])
def test_missing_or_other_frame_template_is_unavailable(template):
    student = printed_page()
    cv2.line(student, px(*LINE[0]), px(*LINE[1]), PENCIL, 3)
    assert measure(student, line_ref(), template)["status"] == "unavailable"


def test_point_only_standard_keeps_its_existing_marked_path():
    ref = reference([{"kind": "point", "point": [500, 500]}])
    student = printed_page()
    cv2.circle(student, px(500, 500), 7, (0, 0, 0), -1)
    item = gate.evaluate_pixel_gate(student, QUESTION, ref)["variants"][0]
    assert item["metrics"]["mode"] == "marked_point_grid_suppression" and item["status"] == "pass"


@pytest.mark.parametrize("drawn,decision,calls", [(True, "correct", 1), (False, "wrong", 0)])
def test_marked_template_line_runs_the_dual_gate_end_to_end(tmp_path, monkeypatch, drawn, decision, calls):
    from PIL import Image
    student = printed_page()
    if drawn:
        cv2.line(student, px(*LINE[0]), px(*LINE[1]), PENCIL, 3)
    path = tmp_path / "student.png"
    Image.fromarray(student).save(path)
    call = AsyncMock(return_value={"content": json.dumps({"result": "correct", "p_correct": 95, "reason": "一致"}),
                                  "model": "qwen3.8-flash", "parameters": {"reasoning_effort": "high"}})
    monkeypatch.setattr("services.flash_grading.call_flash", call)
    question = {**QUESTION, "drawing_reference": line_ref()}
    result = asyncio.run(drawing.assess_page(
        source_path=str(path), page_type_id="page_1", questions=[question], transcriptions=[],
        evidence_paths=(str(path),), template_mode="marked", marked_template=printed_page(),
        diagnostics={"template_width": W, "template_height": H, "answer_source_complete": True,
                     "evidence_columns": [{"crop_left_px": 0, "crop_right_px": W}]}))["5"]
    assert result["pixel_gate"]["variants"][0]["status"] == ("pass" if drawn else "fail")
    assert result["decision"] == decision and call.await_count == calls


def test_rendered_standard_lands_on_independently_computed_pixels():
    from services.drawing_reference import render_reference_mask
    mask = render_reference_mask(line_ref(), W, H)
    for x, y in LINE:
        column, row = px(x, y)
        assert mask[row, column] != 0  # rows are y, columns are x
    circle = render_reference_mask(circle_ref(), W, H)
    rows, columns = np.nonzero(circle)
    width, height = columns.max() - columns.min(), rows.max() - rows.min()
    assert abs(width - height) <= 2 and abs(width - 2 * CIRCLE_RADIUS_PX) <= 6
    center_x, center_y = px(*CIRCLE_CENTER)
    assert abs((columns.max() + columns.min()) / 2 - center_x) <= 1
    assert abs((rows.max() + rows.min()) / 2 - center_y) <= 1
