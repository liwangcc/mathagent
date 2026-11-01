"""Pure synthetic coarse-gate contracts. No files, configuration, DB or model."""
import copy

import cv2
import numpy as np
import pytest

from services import drawing_pixel_gate as gate
from services.drawing_reference import normalize_reference, render_overlay, render_reference_mask


def reference(strokes=None, variants=None, mode="fixed"):
    if variants is None:
        variants = [{"strokes": strokes or [{"kind": "polyline", "points": [[300, 400], [560, 550], [710, 550]]}]}]
    return normalize_reference({"version": "drawing-reference-v1", "coordinate_space": "normalized_1000_v1",
                                "comparison_mode": mode, "variants": variants, "context_regions": []}, trusted_confirmed=True)


QUESTION = {"drawing_regions": [[180, 305, 880, 865]]}


@pytest.mark.parametrize("stroke", [
    {"kind": "ellipse", "center": [500, 500], "radii": [100, 140], "fill": False},
    {"kind": "ellipse", "center": [500, 500], "radii": [100, 140], "fill": True},
    {"kind": "polygon", "points": [[400, 400], [600, 400], [550, 650], [400, 650]]},
    {"kind": "brush", "points": [[400, 500], [600, 600]], "width": 30},
])
def test_new_shapes_require_correct_student_ink_and_reliable_blank(stroke):
    ref = reference([stroke])
    template = np.full((800, 1200, 3), 255, np.uint8)
    mask = render_reference_mask(ref, 1200, 800)
    student = template.copy()
    student[mask != 0] = 0
    for pixels, expected in [(student, "pass"), (template, "fail"), (np.roll(student, 200, axis=1), "fail")]:
        value = gate.evaluate_pixel_gate(pixels, QUESTION, ref, blank_template=template, template_mode="blank")
        assert value["variants"][0]["status"] == expected, value
    assert gate.evaluate_pixel_gate(student, QUESTION, ref)["variants"][0]["status"] == "unavailable"


@pytest.mark.parametrize("case,expected", [("complete", "pass"), ("missing_line", "fail"), ("extra_area", "fail")])
def test_filled_area_and_outline_preserve_required_and_extra_strokes(case, expected):
    strokes = [
        {"kind": "polygon", "points": [[400, 400], [600, 400], [600, 650], [400, 650]]},
        {"kind": "polyline", "points": [[400, 400], [600, 400], [600, 650], [400, 650], [400, 400]]},
        {"kind": "polyline", "points": [[600, 500], [750, 500]]},
    ]
    ref = reference(strokes)
    template = np.full((800, 1200, 3), 255, np.uint8)
    student = template.copy()
    painted = reference(strokes[:-1] if case == "missing_line" else strokes)
    student[render_reference_mask(painted, 1200, 800) != 0] = 0
    if case == "extra_area":
        cv2.rectangle(student, (240, 550), (960, 640), (0, 0, 0), -1)
    value = gate.evaluate_pixel_gate(student, QUESTION, ref, blank_template=template, template_mode="blank")
    assert value["variants"][0]["status"] == expected, value


def blank(size=1000):
    image = np.full((size, size, 3), 255, np.uint8)
    for x in range(200, 851, 50):
        cv2.line(image, (round(x * size / 1000), round(340 * size / 1000)),
                 (round(x * size / 1000), round(790 * size / 1000)), (210, 210, 210), 1)
    for y in range(340, 791, 50):
        cv2.line(image, (round(200 * size / 1000), round(y * size / 1000)),
                 (round(850 * size / 1000), round(y * size / 1000)), (210, 210, 210), 1)
    cv2.circle(image, (round(.3 * size), round(.4 * size)), max(1, round(.004 * size)), (90, 90, 90), -1)
    cv2.line(image, (round(.22 * size), round(.815 * size)), (round(.37 * size), round(.815 * size)), (100, 100, 100), 2)
    return image


def line_student(template, case="standard"):
    result = template.copy()
    scale = len(result) / 1000
    points = np.array([[300, 400], [560, 550], [710, 550]], float)
    if case == "long":
        points[-1, 0] += 75
    elif case == "missing":
        points = points[:2]
    elif case == "shift":
        points += (3, 3)
    elif case == "blank":
        return result
    cv2.polylines(result, [np.rint(points * scale).astype(np.int32)], False, (0, 0, 0), max(1, round(4 * scale)))
    if case == "extra":
        cv2.line(result, (round(710 * scale), round(550 * scale)),
                 (round(710 * scale), round(770 * scale)), (0, 0, 0), max(1, round(4 * scale)))
    return result


@pytest.mark.parametrize("size", [500, 1000, 2000])
@pytest.mark.parametrize("case,expected", [("standard", "pass"), ("long", "pass"), ("shift", "pass"),
                                           ("missing", "fail"), ("extra", "fail"), ("blank", "fail")])
def test_fixed_lines_scale_and_coarse_controls(size, case, expected):
    template = blank(size)
    value = gate.evaluate_pixel_gate(line_student(template, case), QUESTION, reference(),
                                    blank_template=template, template_mode="blank")
    assert value["version"] == "drawing-pixel-coarse-v1" and value["threshold"] == 80
    item = value["variants"][0]
    assert item["status"] == expected, item
    assert item["metrics"]["line_tolerance_px"] == size * .005
    if case == "long":
        assert 80 < item["score"] < 90
    if case == "missing":
        assert item["metrics"]["segment_coverage_percent"][1] < 10
    if case == "blank":
        assert item["score"] == 0 and item["metrics"]["new_ink_pixels"] == 0


def point(x=500, y=500):
    return {"kind": "point", "point": [x, y]}


@pytest.mark.parametrize("size", [738, 1477, 2216])
def test_point_tolerance_and_existing_radius_scale(size):
    ref = reference([point(500.0, 500)])
    image = np.full((round(size * 2064 / 1477), size, 3), 255, np.uint8)
    mask = render_reference_mask(ref, image.shape[1], image.shape[0])
    image[mask != 0] = 0
    item = gate.evaluate_pixel_gate(image, {"drawing_regions": [[400, 400, 600, 600]]}, ref)["variants"][0]
    assert item["status"] == "pass" and item["score"] == 100
    assert item["metrics"]["point_tolerance_px"] == pytest.approx(3 * size / 1477)
    assert item["metrics"]["limitations"]


def test_multiple_point_strokes_require_every_point_not_union_average():
    ref = reference([point(300, 400), point(700, 600)])
    image = np.full((1000, 1000, 3), 255, np.uint8)
    cv2.circle(image, (300, 400), 7, (0, 0, 0), -1)
    value = gate.evaluate_pixel_gate(image, QUESTION, ref)["variants"][0]
    assert value["status"] == "fail" and value["score"] == 0
    assert value["metrics"]["point_coverage_percent"] == [100, 0]


def test_independent_alternatives_do_not_combine_partial_matches():
    ref = reference(variants=[{"strokes": [point(300, 400), point(400, 400)]},
                              {"strokes": [point(700, 600), point(800, 600)]}])
    image = np.full((1000, 1000, 3), 255, np.uint8)
    for center in ((300, 400), (800, 600)):
        cv2.circle(image, center, 7, (0, 0, 0), -1)
    values = gate.evaluate_pixel_gate(image, QUESTION, ref)["variants"]
    assert [value["variant_index"] for value in values] == [0, 1]
    assert [value["status"] for value in values] == ["fail", "fail"]
    cv2.circle(image, (400, 400), 7, (0, 0, 0), -1)
    values = gate.evaluate_pixel_gate(image, QUESTION, ref)["variants"]
    assert [value["status"] for value in values] == ["pass", "fail"]


def test_marked_grid_lines_are_not_point_ink_but_printed_dots_limit_is_explicit():
    image = np.full((1000, 1000, 3), 255, np.uint8)
    cv2.line(image, (200, 500), (850, 500), (0, 0, 0), 1)
    cv2.line(image, (500, 350), (500, 790), (0, 0, 0), 1)
    ref = reference([point()])
    assert gate.evaluate_pixel_gate(image, QUESTION, ref)["variants"][0]["status"] == "fail"
    cv2.circle(image, (500, 500), 7, (0, 0, 0), -1)
    item = gate.evaluate_pixel_gate(image, QUESTION, ref)["variants"][0]
    assert item["status"] == "pass"
    assert "printed dots" in item["metrics"]["limitations"][0]


@pytest.mark.parametrize("case", ["standard", "missing", "blank"])
def test_marked_lines_are_unavailable_not_destructively_grid_removed(case):
    item = gate.evaluate_pixel_gate(line_student(blank(), case), QUESTION, reference())["variants"][0]
    assert item["status"] == "unavailable" and item["score"] is None


def test_mixed_strokes_cannot_hide_missing_short_required_stroke():
    ref = reference([{"kind": "polyline", "points": [[300, 400], [700, 400]]},
                     {"kind": "polyline", "points": [[700, 400], [700, 430]]}, point(500, 700)])
    template = blank()
    image = template.copy()
    cv2.line(image, (300, 400), (700, 400), (0, 0, 0), 4)
    cv2.circle(image, (500, 700), 7, (0, 0, 0), -1)
    item = gate.evaluate_pixel_gate(image, QUESTION, ref, blank_template=template, template_mode="blank")["variants"][0]
    assert item["status"] == "fail" and item["metrics"]["segment_coverage_percent"][1] < 30
    cv2.line(image, (700, 400), (700, 430), (0, 0, 0), 4)
    item = gate.evaluate_pixel_gate(image, QUESTION, ref, blank_template=template, template_mode="blank")["variants"][0]
    assert item["status"] == "pass", item


@pytest.mark.parametrize("layer", ["overlay", "solid_blue"])
def test_reference_layer_never_counts_as_student_ink(layer):
    ref = reference([point()])
    image = np.full((1000, 1000, 3), 255, np.uint8)
    if layer == "overlay":
        image = render_overlay(image, ref)
    else:
        image[render_reference_mask(ref, 1000, 1000) != 0] = (0, 160, 240)
    item = gate.evaluate_pixel_gate(image, QUESTION, ref)["variants"][0]
    assert item["status"] == "unavailable" and item["score"] is None


def test_blank_difference_excludes_printed_dot_and_ruler_and_preserves_inputs():
    template = blank()
    ref = reference([point(300, 400)])
    original, saved_ref, saved_question = template.copy(), copy.deepcopy(ref), copy.deepcopy(QUESTION)
    item = gate.evaluate_pixel_gate(template, QUESTION, ref, blank_template=template, template_mode="blank")["variants"][0]
    assert item["status"] == "fail" and item["score"] == 0
    assert np.array_equal(template, original) and ref == saved_ref and QUESTION == saved_question


@pytest.mark.parametrize("canvas", [None, [], np.zeros((20, 20), np.uint8), np.zeros((20, 20, 4), np.uint8),
                                   np.zeros((20, 20, 3), np.float32), np.zeros((0, 20, 3), np.uint8)])
def test_invalid_canvas_unavailable(canvas):
    item = gate.evaluate_pixel_gate(canvas, QUESTION, reference())["variants"][0]
    assert item["status"] == "unavailable" and item["score"] is None


@pytest.mark.parametrize("regions", [None, [], [[0, 0, 1001, 1000]], [[False, 0, 900, 900]],
                                    [[10, 20, 10, 30]], [[0, 0, float("nan"), 1000]]])
def test_invalid_regions_unavailable(regions):
    item = gate.evaluate_pixel_gate(blank(), {"drawing_regions": regions}, reference())["variants"][0]
    assert item["status"] == "unavailable" and item["score"] is None


def test_reference_outside_roi_is_unavailable_only_for_that_variant():
    ref = reference(variants=[{"strokes": [point(500, 500)]}, {"strokes": [point(990, 990)]}])
    image = np.full((1000, 1000, 3), 255, np.uint8)
    cv2.circle(image, (500, 500), 7, (0, 0, 0), -1)
    values = gate.evaluate_pixel_gate(image, QUESTION, ref)["variants"]
    assert [item["status"] for item in values] == ["pass", "unavailable"]


@pytest.mark.parametrize("change", ["stale", "unconfirmed", "manual", "bad_coordinates"])
def test_untrusted_or_unsupported_reference_unavailable(change):
    ref = reference([point()])
    if change == "stale":
        ref["variants"][0]["strokes"][0]["point"][0] = 550
    elif change == "unconfirmed":
        ref["teacher_confirmed"] = False
    elif change == "manual":
        ref = reference(mode="manual", variants=[])
    else:
        ref["variants"][0]["strokes"][0]["point"][0] = True
    item = gate.evaluate_pixel_gate(blank(), QUESTION, ref)["variants"][0]
    assert item["status"] == "unavailable" and item["score"] is None


@pytest.mark.parametrize("background", [None, np.zeros((5, 5, 3), np.uint8), np.zeros((1000, 1000, 3), float)])
def test_missing_or_mismatched_blank_is_not_zero(background):
    item = gate.evaluate_pixel_gate(blank(), QUESTION, reference(), blank_template=background, template_mode="blank")["variants"][0]
    assert item["status"] == "unavailable" and item["score"] is None


def test_per_variant_computation_failure_does_not_erase_other_candidate(monkeypatch):
    ref = reference(variants=[{"strokes": [point(400, 500)]}, {"strokes": [point(600, 500)]}])
    original = gate._measure_variant
    def fail_second(index, *args):
        if index == 1:
            raise RuntimeError("synthetic kernel failure")
        return original(index, *args)
    monkeypatch.setattr(gate, "_measure_variant", fail_second)
    values = gate.evaluate_pixel_gate(blank(), QUESTION, ref)["variants"]
    assert values[0]["status"] == "fail"
    assert values[1]["status"] == "unavailable" and values[1]["score"] is None


def test_normal_max_integer_float_and_multiple_polyline_contracts():
    strokes = [{"kind": "polyline", "points": [[300, 400.0], [560.0, 550]]},
               {"kind": "polyline", "points": [[560, 550.0], [710.0, 550]]}]
    template = blank()
    item = gate.evaluate_pixel_gate(line_student(template), QUESTION, reference(strokes),
                                    blank_template=template, template_mode="blank")["variants"][0]
    assert item["status"] == "pass" and len(item["metrics"]["segment_coverage_percent"]) == 2


def test_unrelated_student_text_and_blue_q_label_outside_region_are_ignored():
    template = blank()
    student = line_student(template)
    expected = gate.evaluate_pixel_gate(student, QUESTION, reference(), blank_template=template, template_mode="blank")
    cv2.rectangle(student, (10, 10), (170, 280), (0, 0, 0), -1)
    cv2.rectangle(student, (170, 300), (179, 320), (0, 160, 240), -1)
    assert gate.evaluate_pixel_gate(student, QUESTION, reference(), blank_template=template, template_mode="blank") == expected


def test_context_and_subpixel_geometry_fail_closed_as_unavailable():
    template = blank()
    ref = reference()
    ref["context_regions"] = [[1, 1, 20, 20]]
    ref = normalize_reference(ref, trusted_confirmed=True)
    assert gate.evaluate_pixel_gate(template, QUESTION, ref)["variants"][0]["status"] == "unavailable"
    ref = reference([{"kind": "polyline", "points": [[500, 500], [500.001, 500]]}])
    item = gate.evaluate_pixel_gate(template, QUESTION, ref, blank_template=template, template_mode="blank")["variants"][0]
    assert item["status"] == "unavailable" and item["score"] is None


def test_distance_sampling_handles_long_valid_sample_arrays():
    distance = np.ones((10, 10), dtype=np.float32)
    samples = np.tile(np.array([[5.0, 5.0]]), (40000, 1))
    values = gate._sample_distance(distance, samples)
    assert len(values) == 40000 and np.all(values == 1)


@pytest.mark.parametrize("coverage,expected", [("hatched", "pass"), ("half", "fail")])
def test_spaced_hatching_is_not_an_automatic_area_fail(coverage, expected):
    # Line tolerance is 4px here; hatching 18px apart leaves 9px gaps that a
    # student's correct shading routinely has. A pixel fail is an automatic zero.
    ref = reference([{"kind": "polygon", "points": [[400, 400], [600, 400], [600, 650], [400, 650]]}])
    template = np.full((800, 1200, 3), 255, np.uint8)
    mask = render_reference_mask(ref, 1200, 800) != 0
    rows = np.zeros(mask.shape, bool)
    for y in range(0, 800, 18):
        rows[y:y + 2] = True
    if coverage == "half":
        rows[:, 600:] = False
    student = template.copy()
    student[mask & rows] = 0
    value = gate.evaluate_pixel_gate(student, QUESTION, ref, blank_template=template, template_mode="blank")
    assert value["variants"][0]["status"] == expected, value
    assert value["variants"][0]["metrics"]["area_tolerance_px"] == 12
