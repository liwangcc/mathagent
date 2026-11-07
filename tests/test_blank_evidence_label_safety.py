"""Deterministic blank-template label safety; no model calls or student data."""

from pathlib import Path

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")
from PIL import Image

from services import template_evidence as evidence
from services.template_anchor_crops import build_anchor_crop_plan
from services import template_evidence_sheets


def _layout(regions, *, columns=1, anchors=None, mode="blank", schema=None):
    numbers = [item["question_number"] for item in regions]
    bounds = [round(index * 1000 / columns) for index in range(columns + 1)]
    return {
        "page_type_id": "page_1",
        "template_mode": mode,
        "question_numbers": numbers,
        "question_anchors": anchors or [{"question_number": number} for number in numbers],
        "template_answer_regions": regions,
        "template_handwriting_regions": regions if mode == "marked" else [],
        **({"question_schema": schema} if schema is not None else {}),
        "question_crop_plan": build_anchor_crop_plan(
            {"column_layout": {"column_count": columns, "columns": [
                {"column_index": index + 1, "x1_1000": left, "x2_1000": right}
                for index, (left, right) in enumerate(zip(bounds, bounds[1:]))
            ]}},
            artifact_sha256="a" * 64,
            expected_question_numbers=numbers,
        ),
    }


def _render(tmp_path, monkeypatch, difference, layout, *, allow_fixed_solve_overlay=False):
    # Test the actual production evidence renderer with an exact known
    # difference. This isolates label safety from diff-cleaner recall.
    original = difference.copy()
    monkeypatch.setattr(
        template_evidence_sheets,
        "_answer_only_pixels_v2",
        lambda *args, **kwargs: difference.copy(),
    )
    monkeypatch.setattr(
        evidence,
        "_answer_only_pixels_v2",
        lambda *args, **kwargs: difference.copy(),
    )
    result = evidence.build_full_column_difference_evidence_from_aligned(
        difference.copy(),
        np.full(difference.shape[:2], 255, np.uint8),
        np.full_like(difference, 255),
        layout,
        tmp_path,
    )
    assert np.array_equal(difference, original)
    from services.drawing_grading import reconstruct_canvas
    combined = reconstruct_canvas(result.evidence_paths, result.diagnostics)
    assert combined.shape == original.shape
    foreground = np.any(original != 255, axis=2)
    # No teacher/solve exception: every foreground pixel is evidence.
    assert np.array_equal(combined[foreground], original[foreground])
    return result, combined


@pytest.mark.parametrize("columns", [1, 2, 3])
@pytest.mark.parametrize("size", [(1200, 800), (900, 1400), (2400, 1200)])
def test_multiple_layouts_preserve_all_foreground_and_label_each_question(
    tmp_path, monkeypatch, columns, size,
):
    width, height = size
    difference = np.full((height, width, 3), 255, np.uint8)
    regions = []
    for col in range(columns):
        for row, top in enumerate([220, 640]):
            left = round((col + 0.65) * 1000 / columns)
            right = round((col + 0.90) * 1000 / columns)
            number = str(col * 2 + row + 1)
            regions.append({"question_number": number, "regions": [[left, top, right, top + 80]]})
            x = round(left * width / 1000)
            y = round((top + 40) * height / 1000)
            cv2.line(difference, (x - 45, y - 6), (x + 25, y + 6), (25, 25, 25), 2)
            # A pale tiny point outside the predicted box must also survive.
            difference[y - 10, x - 65] = (254, 254, 254)
    result, _ = _render(tmp_path, monkeypatch, difference, _layout(regions, columns=columns))
    actual = [placement for column in result.diagnostics["evidence_columns"]
              for placement in column["label_placements"]]
    assert len(actual) == 2 * columns
    assert {placement["question_number"] for placement in actual} == {item["question_number"] for item in regions}
    sizes = {(item["label_bbox"][2] - item["label_bbox"][0],
              item["label_bbox"][3] - item["label_bbox"][1]) for item in actual}
    assert len(sizes) == 1  # Same font and padding for all one-digit labels.
    assert result.diagnostics["blank_label_safety"]["all_regions_labeled"] is True
    for column in result.diagnostics["evidence_columns"]:
        assert len(column["question_numbers"]) == 2
        for placement in column["label_placements"]:
            x1, y1, x2, y2 = placement["label_bbox"]
            assert 0 <= x1 < x2 < column["width"]
            assert 0 <= y1 < y2 < height


@pytest.mark.parametrize("shade", [0, 230, 254])
def test_left_of_box_handwriting_and_single_pixel_symbols_move_label(
    tmp_path, monkeypatch, shade,
):
    regions = [{"question_number": "2", "regions": [[550, 400, 700, 470]]}]
    layout = _layout(regions)
    blank = np.full((800, 1000, 3), 255, np.uint8)
    initial, _ = _render(tmp_path / "baseline", monkeypatch, blank, layout)
    first = initial.diagnostics["evidence_columns"][0]["label_placements"][0]
    x1, y1, x2, y2 = first["label_bbox"]
    difference = blank.copy()
    difference[(y1 + y2) // 2, (x1 + x2) // 2] = shade
    result, _ = _render(tmp_path / "ink", monkeypatch, difference, layout)
    actual = result.diagnostics["evidence_columns"][0]["label_placements"][0]
    assert actual["label_bbox"] != first["label_bbox"]
    assert actual["foreground_preserved"] is True


def test_narrow_column_searches_full_left_gutter_before_rejecting(tmp_path, monkeypatch):
    """A crowded local gutter must not create a false save-time rejection."""
    layout = _layout([{"question_number": "1", "regions": [[500, 400, 700, 470]]}])
    difference = np.full((800, 1200, 3), 255, np.uint8)
    # Occupy the former two-label-width search window while leaving a clear
    # gutter farther left.  The label may move there, but must preserve ink.
    difference[250:520, 400:540] = 40
    result, _ = _render(tmp_path, monkeypatch, difference, layout)
    placement = result.diagnostics["evidence_columns"][0]["label_placements"][0]
    assert placement["placement_offset_px"][0] < -100
    assert placement["foreground_preserved"] is True


def test_continuous_region_can_use_right_rail_without_covering_answers(tmp_path, monkeypatch):
    """A full-width answer box must not fail only because its left rail is absent."""
    layout = _layout([{"question_number": "1.1", "regions": [[0, 300, 520, 420]]}])
    result, _ = _render(
        tmp_path,
        monkeypatch,
        np.full((800, 1600, 3), 255, np.uint8),
        layout,
    )
    placement = result.diagnostics["evidence_columns"][0]["label_placements"][0]
    assert placement["label_left_of_region"] is False
    assert placement["placement_offset_px"][0] > 0
    assert result.diagnostics["blank_label_safety"]["all_regions_labeled"] is True


def test_all_crowded_regions_are_labeled_in_external_rail(tmp_path, monkeypatch):
    """Crowding is a presentation problem, not missing answer evidence."""
    layout = _layout([
        {"question_number": "1.1", "regions": [[0, 120, 1000, 300]]},
        {"question_number": "3.3.1", "regions": [[0, 520, 1000, 700]]},
    ])
    result, _ = _render(tmp_path, monkeypatch, np.full((800, 1200, 3), 254, np.uint8), layout)
    labels = result.diagnostics["evidence_columns"][0]["label_placements"]
    assert [p["question_number"] for p in labels] == ["1.1", "3.3.1"]
    assert all(p["placement_mode"] == "external_rail" for p in labels)


def test_multiple_blanks_and_cross_column_continuation_are_independent(tmp_path, monkeypatch):
    regions = [{"question_number": "17", "regions": [
        [180, 220, 230, 270], [370, 220, 470, 270], [720, 450, 900, 700],
    ]}]
    result, _ = _render(tmp_path, monkeypatch, np.full((1200, 2000, 3), 255, np.uint8),
                        _layout(regions, columns=2))
    columns = result.diagnostics["evidence_columns"]
    assert [column["question_numbers"] for column in columns] == [["17"], ["17"]]
    assert [len(column["label_placements"]) for column in columns] == [2, 1]
    assert [item["region_index"] for column in columns for item in column["label_placements"]] == [0, 1, 2]
    assert [item["region_bbox_1000"] for column in columns for item in column["label_placements"]] == regions[0]["regions"]
    assert result.diagnostics["blank_label_safety"]["expected_region_labels"] == 3


def test_adjacent_same_question_blanks_never_overlap_labels(tmp_path, monkeypatch):
    regions = [{"question_number": "4", "regions": [
        [400, 400, 415, 440], [430, 400, 445, 440],
    ]}]
    result, _ = _render(tmp_path, monkeypatch, np.full((900, 1400, 3), 255, np.uint8), _layout(regions))
    placements = result.diagnostics["evidence_columns"][0]["label_placements"]
    first, second = [item["label_bbox"] for item in placements]
    assert (first[2] < second[0] or second[2] < first[0]
            or first[3] < second[1] or second[3] < first[1])


def test_bottom_region_has_complete_label_and_large_solution_uses_start(tmp_path, monkeypatch):
    regions = [
        {"question_number": "20", "regions": [[400, 100, 800, 700]]},
        {"question_number": "21", "regions": [[500, 960, 700, 999]]},
    ]
    layout = _layout(regions, anchors=[
        {"question_number": "20", "question_number_bbox": [350, 85, 380, 105]},
        {"question_number": "21"},
    ])
    result, _ = _render(tmp_path, monkeypatch, np.full((800, 1200, 3), 255, np.uint8), layout)
    first, last = result.diagnostics["evidence_columns"][0]["label_placements"]
    assert first["label_bbox"][3] < 140
    assert last["label_bbox"][3] < 800
    assert last["label_bbox"][1] > 700


@pytest.mark.parametrize("regions", [[], [{"question_number": "1", "regions": []}]])
def test_missing_answer_region_requires_teacher_review(tmp_path, monkeypatch, regions):
    layout = _layout([{"question_number": "1", "regions": [[500, 300, 700, 400]]}])
    layout["template_answer_regions"] = regions
    with pytest.raises(evidence.TemplateEvidenceError, match="教师"):
        _render(tmp_path, monkeypatch, np.full((800, 1200, 3), 255, np.uint8), layout)
    assert list(tmp_path.glob("evidence-*.png")) == []


def test_no_safe_room_adds_rail_without_modifying_foreground(tmp_path, monkeypatch):
    difference = np.full((800, 1200, 3), 254, np.uint8)
    layout = _layout([{"question_number": "1", "regions": [[500, 300, 700, 400]]}])
    result, combined = _render(tmp_path, monkeypatch, difference, layout)
    assert np.array_equal(combined, difference)
    assert result.diagnostics["evidence_columns"][0]["label_layout"]["source_offset_px"][0] > 0


def test_teacher_confirmed_number_box_never_authorizes_ink_overwrite(tmp_path, monkeypatch):
    """A teacher-marked printed number remains usable when the gutter is inked."""
    regions = [{"question_number": "1.1", "regions": [[500, 300, 700, 400]]}]
    layout = _layout(
        regions,
        anchors=[{
            "question_number": "1.1",
            "question_number_bbox": [400, 300, 460, 340],
            "human_confirmed": True,
        }],
    )
    difference = np.full((800, 1200, 3), 255, np.uint8)
    # Teacher confirmation is geometry, never permission to erase pixels.
    difference[240:273, 480:553] = 40
    result, _ = _render(
        tmp_path, monkeypatch, difference, layout,
        allow_fixed_solve_overlay=True,
    )
    placement = result.diagnostics["evidence_columns"][0]["label_placements"][0]
    assert placement["source"] == "template_answer_region"
    assert placement["fixed_position_overwrite_allowed"] is False
    assert placement["foreground_preserved"] is True


def test_failure_in_second_column_removes_first_output(tmp_path, monkeypatch):
    difference = np.full((800, 1200, 3), 255, np.uint8)
    difference[:, 600:] = 254
    layout = _layout([
        {"question_number": "1", "regions": [[300, 300, 440, 400]]},
        {"question_number": "2", "regions": [[800, 300, 950, 400]]},
    ], columns=2)
    from services import evidence_label_layout
    real = evidence_label_layout.render_labels
    calls = []
    def fail_second(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise evidence.TemplateEvidenceError("教师复核：测试注入的渲染故障")
        return real(*args, **kwargs)
    monkeypatch.setattr(evidence_label_layout, "render_labels", fail_second)
    with pytest.raises(evidence.TemplateEvidenceError, match="教师"):
        _render(tmp_path, monkeypatch, difference, layout)
    assert list(tmp_path.glob("evidence-*.png")) == []


def test_empty_final_column_cannot_hide_missing_earlier_labels(tmp_path, monkeypatch):
    layout = _layout([{"question_number": "1", "regions": [[200, 200, 300, 300]]}], columns=3)
    from services import evidence_label_layout
    real = evidence_label_layout.render_labels
    calls = []

    def omit_first(*args, **kwargs):
        canvas, placements, mapping = real(*args, **kwargs)
        calls.append(1)
        return canvas, [] if len(calls) == 1 else placements, mapping

    monkeypatch.setattr(evidence_label_layout, "render_labels", omit_first)
    with pytest.raises(evidence.TemplateEvidenceError, match="标记不完整"):
        _render(tmp_path, monkeypatch, np.full((800, 1800, 3), 255, np.uint8), layout)
    assert list(tmp_path.glob("evidence-*.png")) == []


@pytest.mark.parametrize("mode", ["marked", ""])
def test_marked_and_legacy_use_unified_safe_renderer(tmp_path, monkeypatch, mode):
    layout = _layout([{"question_number": "1", "regions": [[600, 400, 800, 500]]}], mode=mode)
    layout["template_handwriting_regions"] = layout["template_answer_regions"]
    result, _ = _render(tmp_path, monkeypatch, np.full((800, 1200, 3), 255, np.uint8), layout)
    assert result.diagnostics["blank_label_safety"]["all_regions_labeled"]
    assert result.diagnostics["evidence_columns"][0]["label_placements"][0]["source"] == "template_answer_region"


@pytest.mark.parametrize("mode", ["blank", "marked"])
@pytest.mark.parametrize("shade", [0, 230, 254])
def test_solve_keeps_printed_anchor_but_escapes_ink(tmp_path, monkeypatch, mode, shade):
    # Legacy printed anchors are used only when no unified answer box exists.
    regions = [{"question_number": "26", "regions": []}]
    layout = _layout(regions, columns=2, mode=mode,
                     anchors=[{"question_number": "26", "question_number_bbox": [560, 120, 590, 160]}],
                     schema=[{"question_number": "26", "question_type": "solve"}])
    blank = np.full((800, 1200, 3), 255, np.uint8)
    baseline, _ = _render(tmp_path / "baseline", monkeypatch, blank, layout,
                          allow_fixed_solve_overlay=True)
    columns = baseline.diagnostics["evidence_columns"]
    assert columns[0]["question_numbers"] == []
    assert columns[1]["question_numbers"] == ["26"]
    placement = columns[1]["label_placements"][0]
    x1, y1, x2, y2 = placement["label_bbox"]
    # Independent pixel oracle: right edge 590/1000*1200 - second-column 600;
    # vertical midpoint (120+160)/2000*800. No answer-region gap or clamping.
    assert x2 == 108
    assert abs((y1 + y2) / 2 - 112) <= .5
    assert placement["source"] == "printed_question_number"
    assert placement["placement_offset_px"] == [0, 0]
    assert placement["label_left_of_region"] is False
    difference = blank.copy()
    difference[y1, x1 + 600] = shade
    actual, rendered = _render(tmp_path / "ink", monkeypatch, difference, layout,
                               allow_fixed_solve_overlay=True)
    again = actual.diagnostics["evidence_columns"][1]["label_placements"][0]
    assert again["placement_mode"] == "external_rail"
    assert np.array_equal(rendered[y1, x1 + 600], difference[y1, x1 + 600])
    assert again["fixed_position_overwrite_allowed"] is False
    assert actual.blank_question_numbers == []
    assert actual.diagnostics["printed_solve_label_policy"]["placed_labels"] == 1
    assert actual.diagnostics["qwen37_label_contract"]["foreground_preserved"] is True
    assert again["question_number_bbox"] == [560, 120, 590, 160]


@pytest.mark.parametrize("mode", ["blank", "marked"])
def test_explicit_solve_uses_unified_answer_box_and_rejects_missing_geometry(tmp_path, monkeypatch, mode):
    layout = _layout([{"question_number": "26", "regions": [[600, 300, 800, 500]]}], mode=mode,
                     schema=[{"question_number": "26", "question_type": "solve"}])
    result, _ = _render(tmp_path / "valid", monkeypatch, np.full((800, 1200, 3), 255, np.uint8), layout)
    assert result.diagnostics["evidence_columns"][0]["label_placements"][0]["source"] == "template_answer_region"
    layout["template_answer_regions"][0]["regions"] = []
    with pytest.raises(evidence.TemplateEvidenceError, match="印刷题号"):
        _render(tmp_path, monkeypatch, np.full((800, 1200, 3), 255, np.uint8), layout)
    assert list(tmp_path.glob("evidence-*.png")) == []


@pytest.mark.parametrize("mode", ["blank", "marked"])
def test_all_ordinary_types_use_unified_boxes_in_either_mode(tmp_path, monkeypatch, mode):
    regions = [
        {"question_number": "1", "regions": [[600, 200, 800, 250]]},
        {"question_number": "2", "regions": [[600, 450, 850, 500]]},
        {"question_number": "3", "regions": [[600, 750, 850, 900]]},
    ]
    difference = np.full((1000, 1200, 3), 255, np.uint8)
    legacy_layout = _layout(regions, mode=mode)
    before, _ = _render(tmp_path / "legacy", monkeypatch, difference, legacy_layout)
    layout = _layout(regions, mode=mode,
                     anchors=[{"question_number": "1"}, {"question_number": "2"},
                              {"question_number": "3", "question_number_bbox": [200, 620, 240, 650]}],
                     schema=[{"question_number": str(i), "question_type": kind}
                             for i, kind in enumerate(["choice", "fill", "solve"], 1)])
    after, _ = _render(tmp_path / "mixed", monkeypatch, difference, layout,
                       allow_fixed_solve_overlay=True)
    old = {p["question_number"]: p for p in before.diagnostics["evidence_columns"][0]["label_placements"]}
    new = {p["question_number"]: p for p in after.diagnostics["evidence_columns"][0]["label_placements"]}
    for number in ["1", "2", "3"]:
        assert new[number]["label_bbox"] == old[number]["label_bbox"]
        assert new[number]["source"] == old[number]["source"]
    assert new["3"]["source"] == "template_answer_region"
    assert new["3"]["region_bbox_1000"] == regions[2]["regions"][0]


def test_schema_without_explicit_type_keeps_legacy_placement(tmp_path, monkeypatch):
    regions = [{"question_number": "1", "regions": [[600, 300, 800, 500]]}]
    layout = _layout(regions, schema=[{"question_number": "1"}])
    result, _ = _render(tmp_path, monkeypatch, np.full((800, 1200, 3), 255, np.uint8), layout)
    assert result.diagnostics["evidence_columns"][0]["label_placements"][0]["source"] == "template_answer_region"


def test_fixed_solve_label_at_page_edge_is_not_silently_shifted(tmp_path, monkeypatch):
    layout = _layout([{"question_number": "26", "regions": []}],
                     anchors=[{"question_number": "26", "question_number_bbox": [0, 100, 5, 130]}],
                     schema=[{"question_number": "26", "question_type": "solve"}])
    result, _ = _render(tmp_path, monkeypatch, np.full((800, 1200, 3), 255, np.uint8), layout)
    column = result.diagnostics["evidence_columns"][0]
    assert column["label_layout"]["source_offset_px"][0] > 0
    assert column["label_placements"][0]["question_number_bbox"] == [0, 100, 5, 130]
