"""Code-native reference geometry and mocked candidate contracts; no network."""
import copy

import cv2
import numpy as np
import pytest

from services.drawing_reference import (
    normalize_reference,
    reference_digest, render_display_mask, render_overlay, render_reference_mask,
    validate_confirmed_reference,
)
from services.grading_contract import AnswerSpec


def reference(mode="fixed"):
    return {"version": "drawing-reference-v1", "coordinate_space": "normalized_1000_v1",
            "comparison_mode": mode, "variants": [{"strokes": [
                {"kind": "polyline", "points": [[100, 300], [400, 500], [650, 500]]},
                {"kind": "point", "point": [800, 200]},
            ]}], "context_regions": [[40, 800, 300, 930]]}


def test_candidate_cannot_confirm_or_supply_reference_hash():
    raw = {**reference(), "teacher_confirmed": True, "reference_hash": "forged"}
    before = copy.deepcopy(raw)
    candidate = normalize_reference(raw)
    assert candidate["teacher_confirmed"] is False
    assert candidate["reference_hash"] == reference_digest(raw) != "forged"
    confirmed = normalize_reference(raw, trusted_confirmed=True)
    assert confirmed["teacher_confirmed"] is True
    assert confirmed["reference_hash"] == candidate["reference_hash"]
    assert raw == before


def test_hash_normalizes_numbers_and_binds_geometry_mode_and_context():
    raw = reference()
    digest = reference_digest(raw)
    assert reference_digest(normalize_reference(raw)) == digest
    for field, value in [("comparison_mode", "manual"), ("context_regions", [])]:
        assert reference_digest({**raw, field: value}) != digest
    changed = copy.deepcopy(raw)
    changed["variants"][0]["strokes"][1]["point"][0] += 1
    assert reference_digest(changed) != digest


def test_stored_confirmation_requires_original_valid_hash_and_fixed_gate():
    confirmed = normalize_reference(reference(), trusted_confirmed=True)
    assert validate_confirmed_reference(confirmed, require_fixed=True) == confirmed
    for invalid in [normalize_reference(reference()), {**confirmed, "reference_hash": "stale"}]:
        with pytest.raises(ValueError):
            validate_confirmed_reference(invalid)
    tampered = copy.deepcopy(confirmed)
    tampered["variants"][0]["strokes"][1]["point"][0] += 1
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_confirmed_reference(tampered)
    manual = normalize_reference(reference("manual"), trusted_confirmed=True)
    assert validate_confirmed_reference(manual) == manual
    with pytest.raises(ValueError, match="manual"):
        validate_confirmed_reference(manual, require_fixed=True)


@pytest.mark.parametrize("value", [True, False, float("nan"), float("inf"), -1, 1001, "500", None])
def test_non_finite_boolean_or_non_numeric_points_rejected(value):
    raw = reference()
    raw["variants"][0]["strokes"][1]["point"][0] = value
    with pytest.raises(ValueError):
        normalize_reference(raw)


@pytest.mark.parametrize("change", [
    {"version": "unknown"}, {"coordinate_space": "pixels"}, {"comparison_mode": "auto"},
    {"variants": []}, {"variants": [{"strokes": []}]}, {"variants": [{}]},
    {"context_regions": [[1, 2, 1, 3]]}, {"context_regions": [[0, 0, 1001, 900]]},
    {"image_path": "../not-allowed.png"},
])
def test_invalid_contract_rejected(change):
    with pytest.raises(ValueError):
        normalize_reference({**reference(), **change})


def test_bounded_variants_strokes_and_polyline_points():
    raw = reference()
    for oversized in [
        {**raw, "variants": raw["variants"] * 5},
        {**raw, "variants": [{"strokes": [{"kind": "point", "point": [50, 50]}] * 65}]},
        {**raw, "variants": [{"strokes": [{"kind": "polyline", "points": [[i, i] for i in range(129)]}]}]},
        {**raw, "variants": [{"strokes": [{"kind": "polyline", "points": [[1, 2], [1, 2]]}]}]},
    ]:
        with pytest.raises(ValueError):
            normalize_reference(oversized)


def test_manual_without_geometry_is_explicit_and_cannot_render():
    raw = {**reference("manual"), "variants": []}
    assert normalize_reference(raw)["comparison_mode"] == "manual"
    with pytest.raises(ValueError):
        render_overlay(np.full((820, 1000, 3), 255, np.uint8), raw)


def test_widening_is_exact_ellipse_and_multiply_preserves_original():
    raw = reference()
    student = np.full((820, 1000, 3), 245, np.uint8)
    cv2.line(student, (100, 246), (400, 410), (0, 0, 0), 3)
    student[100:120, 100:120] = 90
    before = student.copy()
    thin = render_reference_mask(raw, 1000, 820)
    expected_mask = cv2.dilate(thin, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)),
                               borderType=cv2.BORDER_CONSTANT, borderValue=0)
    assert np.array_equal(render_display_mask(raw, 1000, 820), expected_mask)
    result = render_overlay(student, raw)
    weight = .45 * expected_mask.astype(np.float64)[..., None] / 255
    expected = np.rint(student.astype(np.float64) * (1 - weight + weight * np.array([0, 160, 240]) / 255)).astype(np.uint8)
    assert np.array_equal(result, expected)
    assert np.array_equal(student, before)
    assert np.array_equal(result[expected_mask == 0], student[expected_mask == 0])
    assert np.all(result <= student)
    assert np.all(result[np.all(student == 0, axis=2)] == 0)


def test_complete_variants_are_not_unioned_or_fitted_to_student():
    raw = reference()
    raw["variants"] = [{"strokes": [{"kind": "point", "point": point}]} for point in [[300, 300], [700, 700]]]
    first = render_display_mask(raw, 1000, 820, 0)
    second = render_display_mask(raw, 1000, 820, 1)
    assert first[246, 300] and not first[574, 700]
    assert second[574, 700] and not second[246, 300]
    for value in (0, 120, 255):
        student = np.full((820, 1000, 3), value, np.uint8)
        render_overlay(student, raw, 0)
        assert np.array_equal(render_display_mask(raw, 1000, 820, 0), first)


def test_geometry_coordinates_and_dilation_scale_with_full_template():
    raw = {**reference(), "variants": [{"strokes": [{"kind": "point", "point": [300, 500]}]}]}
    mask = render_reference_mask(raw, 2000, 1640)
    assert mask[820, 600] == 255
    expected = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (17, 17)),
                         borderType=cv2.BORDER_CONSTANT, borderValue=0)
    assert np.array_equal(render_display_mask(raw, 2000, 1640), expected)


@pytest.mark.parametrize("width,height", [(True, 500), (0, 500), (500, -1), (20000, 2), (10000, 10000)])
def test_invalid_canvas_rejected(width, height):
    with pytest.raises(ValueError):
        render_reference_mask(reference(), width, height)


def test_answerspec_reference_is_editable_content_not_directory_metadata():
    confirmed = normalize_reference(reference(), trusted_confirmed=True)
    spec = AnswerSpec.model_validate({"question_no": "3", "question_type": "solve", "answer_kind": "mixed",
                                    "drawing_reference": confirmed})
    assert "drawing_reference" not in spec.directory_metadata()
    assert spec.model_dump()["drawing_reference"] == confirmed
    entry = spec.to_answer_map_entry()
    assert entry["drawing_reference"] == confirmed
    entry["drawing_reference"]["variants"].clear()
    assert spec.drawing_reference["variants"]
    legacy = AnswerSpec.model_validate({"question_no": "1", "question_type": "fill"})
    assert "drawing_reference" not in legacy.model_dump() and "drawing_reference" not in legacy.to_answer_map_entry()
    with pytest.raises(ValueError):
        AnswerSpec.model_validate({"question_no": "1", "question_type": "fill", "drawing_reference": confirmed})


@pytest.mark.parametrize("stroke", [
    {"kind": "ellipse", "center": [500, 500], "radii": [100, 150], "fill": False},
    {"kind": "ellipse", "center": [500, 500], "radii": [100, 150], "fill": True},
    {"kind": "polygon", "points": [[400, 400], [600, 400], [600, 600], [400, 600]]},
    {"kind": "brush", "points": [[450, 500], [550, 500]], "width": 30},
])
def test_teacher_shapes_render_hash_and_validate_full_containment(stroke):
    from services.drawing_grading import validate_reference_geometry
    raw = {**reference(), "context_regions": [], "variants": [{"strokes": [stroke]}]}
    confirmed = normalize_reference(raw, teacher_confirmed=True)
    assert validate_confirmed_reference(confirmed) == confirmed
    mask = render_reference_mask(confirmed, 1200, 800)
    assert mask.shape == (800, 1200) and np.count_nonzero(mask) > 50
    validate_reference_geometry(confirmed, [[300, 300, 700, 700]])
    with pytest.raises(ValueError):
        validate_reference_geometry(confirmed, [[490, 490, 510, 510]])
    changed = copy.deepcopy(raw)
    changed["variants"][0]["strokes"][0] = {"kind": "point", "point": [500, 500]}
    assert reference_digest(changed) != confirmed["reference_hash"]


@pytest.mark.parametrize("stroke", [
    {"kind": "ellipse", "center": [950, 500], "radii": [100, 100], "fill": False},
    {"kind": "ellipse", "center": [500, 500], "radii": [0, 100], "fill": False},
    {"kind": "ellipse", "center": [500, 500], "radii": [100, 100], "fill": "true"},
    {"kind": "polygon", "points": [[100, 100], [200, 200], [300, 300]]},
    {"kind": "brush", "points": [[100, 100]], "width": True},
    {"kind": "brush", "points": [[100, 100]], "width": 81},
])
def test_invalid_teacher_shapes_are_rejected(stroke):
    with pytest.raises(ValueError):
        normalize_reference({**reference(), "variants": [{"strokes": [stroke]}]})
