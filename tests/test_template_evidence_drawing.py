"""Drawing is a response medium; geometry never retypes or reconstructs work."""
import copy
import hashlib

import cv2
import numpy as np
from PIL import Image
import pytest

from services import template_evidence as evidence
from services.template_anchor_crops import build_anchor_crop_plan
from services import template_evidence_sheets
from services import template_evidence_columns


def drawing(number="2.3", kind="drawing", box=None, anchor=None):
    return {"question_number": number, "question_type": "solve", "answer_kind": kind,
            "drawing_regions": [box or [600, 400, 900, 700]],
            "label_anchor": anchor or [590, 397], "directory_node_id": "n10",
            "directory_parent_id": "n4", "printed_path": ["二", "3"]}


def layout(mode="marked", number="2.3"):
    return {"page_type_id": "page_1", "template_mode": mode,
            "question_numbers": ["2.2", number],
            "question_schema": [{"question_number": "2.2", "question_type": "solve"}, drawing(number)],
            "question_anchors": [
                {"question_number": "2.2", "question_number_bbox": [100, 100, 140, 140]},
                {"question_number": number, "question_number_bbox": [100, 750, 140, 790]}],
            "template_answer_regions": [{"question_number": "2.2", "regions": []},
                                         {"question_number": number, "regions": [[600, 400, 900, 700]]}],
            "template_handwriting_regions": [{"question_number": "2.2", "regions": []},
                                               {"question_number": number, "regions": [[700, 500, 800, 600]]}]}


def prefreeze(value, width=1000, height=1000):
    return evidence.validate_drawing_label_geometry(
        value["question_schema"], value["question_anchors"], value["template_answer_regions"],
        value["template_handwriting_regions"], page_width=width, page_height=height,
        question_crop_plan=value.get("question_crop_plan"), template_mode=value["template_mode"])


def test_legacy_schema_defaults_do_not_change_schema_or_hash_input():
    original = {"question_number": "22", "question_type": "solve"}
    defaults = {**original, "directory_node_id": "", "directory_parent_id": None,
                "printed_path": [], "answer_kind": "non_graphic", "drawing_regions": [], "label_anchor": None}
    assert evidence.normalize_question_schema([original]) == evidence.normalize_question_schema([defaults])


def test_schema_preserves_directory_and_drawing_without_creating_new_question_type():
    row = drawing()
    before = copy.deepcopy(row)
    normalized = evidence.normalize_question_schema([row])[0]
    for key, value in before.items():
        assert normalized[key] == value
    normalized["drawing_regions"][0][0] = 10
    assert row == before


def test_schema_keeps_unprinted_path_nodes_as_none_instead_of_inventing_labels():
    row = {**drawing(), "printed_path": ["二", None, "①"]}
    normalized = evidence.normalize_question_schema([row])[0]
    assert normalized["printed_path"] == ["二", None, "①"]
    assert row["printed_path"] == ["二", None, "①"]


@pytest.mark.parametrize("changes", [
    {"question_type": "drawing"}, {"answer_kind": "unknown"},
    {"answer_kind": []}, {"drawing_regions": []}, {"drawing_regions": [[0, 0, 1001, 100]]},
    {"drawing_regions": [[600, 400, 600, 700]]}, {"drawing_regions": [[600, float("nan"), 900, 700]]},
    {"label_anchor": [True, 500]}, {"label_anchor": None},
    {"printed_path": [1]}, {"directory_node_id": 123},
])
def test_invalid_drawing_or_directory_metadata_is_rejected(changes):
    with pytest.raises(ValueError):
        evidence.normalize_question_schema([{**drawing(), **changes}])


def test_explicit_non_graphic_mark_resolves_stale_drawing_geometry():
    """Product ruling (incident a5d95c2a): an explicit non-graphic mark on a row
    carrying stale model drawing geometry RESOLVES — the geometry clears —
    instead of blocking the save. The residue is invisible model metadata the
    teacher cannot delete in the UI. Mark-less geometry keeps failing closed.
    """
    normalized = evidence.normalize_question_schema([
        {**drawing(), "answer_kind": "non_graphic"},
    ])[0]
    assert normalized["answer_kind"] == "non_graphic"
    assert normalized["drawing_regions"] == []
    assert normalized["label_anchor"] is None
    with pytest.raises(ValueError):
        evidence.normalize_question_schema([
            {key: value for key, value in drawing().items() if key != "answer_kind"},
        ])


def test_schema_accepts_valid_non_left_drawing_anchor():
    row = {**drawing(), "label_anchor": [600, 500]}
    normalized = evidence.normalize_question_schema([row])[0]
    assert normalized["label_anchor"] == [600, 500]


@pytest.mark.parametrize("question_type", ["choice", "fill", "solve"])
@pytest.mark.parametrize("answer_kind", ["drawing", "mixed"])
@pytest.mark.parametrize("mode", ["marked", "blank"])
def test_graphic_type_is_preserved_with_exactly_one_q_in_gate_and_difference(
    tmp_path, monkeypatch, question_type, answer_kind, mode,
):
    value = layout(mode)
    value["question_schema"][1].update(question_type=question_type, answer_kind=answer_kind,
                                      choice_mode="single" if question_type == "choice" else "unknown")
    before = copy.deepcopy(value)
    receipt = prefreeze(value)
    labels = [p for p in receipt["placements"] if p["question_number"] == "2.3"]
    assert len(labels) == 1 and labels[0]["source"] == "drawing_region_left"
    assert value == before
    source = tmp_path / "template.png"
    student = np.full((1000, 1000, 3), 255, np.uint8)
    Image.fromarray(student).save(source)
    artifact = evidence.build_template_artifact(
        source, tmp_path / "artifacts", 0, value["question_schema"], template_mode=mode,
        question_anchors=value["question_anchors"], template_answer_regions=value["template_answer_regions"],
        template_handwriting_regions=value["template_handwriting_regions"] if mode == "marked" else [],
    )
    verified = evidence.verify_template_artifacts(tmp_path / "artifacts", {"page_1": artifact})["page_1"]
    assert verified["question_schema"][1]["question_type"] == question_type
    monkeypatch.setattr(
        template_evidence_sheets,
        "_answer_only_pixels_v2",
        lambda *a, **k: student.copy(),
    )
    monkeypatch.setattr(
        template_evidence_columns,
        "_answer_only_pixels_v2",
        lambda *a, **k: student.copy(),
    )
    result = evidence.build_full_column_difference_evidence_from_aligned(
        student, np.full((1000, 1000), 255, np.uint8), student, {"page_type_id": "page_1", **verified}, tmp_path / "diff")
    actual = [p for c in result.diagnostics["evidence_columns"] for p in c["label_placements"]
              if p["question_number"] == "2.3"]
    assert len(actual) == 1 and actual[0]["source"] == "drawing_region_left"
    assert actual[0]["label_bbox"] == labels[0]["label_bbox"]


def test_blank_pure_graphic_fill_needs_no_text_answer_box_but_mixed_does(tmp_path):
    value = layout("blank")
    value["question_schema"][1]["question_type"] = "fill"
    value["template_answer_regions"][1]["regions"] = []
    source = tmp_path / "template.png"
    Image.new("RGB", (1000, 1000), "white").save(source)
    kwargs = dict(template_mode="blank", question_anchors=value["question_anchors"],
                  template_answer_regions=value["template_answer_regions"], template_handwriting_regions=[])
    artifact = evidence.build_template_artifact(source, tmp_path / "pure", 0, value["question_schema"], **kwargs)
    evidence.verify_template_artifacts(tmp_path / "pure", {"page_1": artifact})
    value["question_schema"][1]["answer_kind"] = "mixed"
    with pytest.raises(ValueError, match="answer region"):
        evidence.build_template_artifact(source, tmp_path / "mixed", 0, value["question_schema"], **kwargs)


@pytest.mark.parametrize("stage,number", [("primary", "2.3"), ("junior", "8"), ("senior", "22")])
@pytest.mark.parametrize("mode", ["marked", "blank"])
def test_three_stage_number_styles_keep_normal_solve_fixed_and_drawing_at_region_left(stage, number, mode):
    value = layout(mode, number)
    before = copy.deepcopy(value)
    receipt = prefreeze(value)
    positions = {item["question_number"]: item for item in receipt["placements"]}
    assert set(positions) == {"2.2", number}
    assert positions["2.2"]["source"] == "printed_question_number"
    assert positions["2.2"]["label_bbox"][2] == 140
    assert positions[number]["source"] == "drawing_region_left"
    assert positions[number]["label_bbox"][2] == 590
    assert positions[number]["placement_offset_px"] == [0, 0]
    assert value == before
    assert receipt["student_foreground_checked"] is False


def test_label_centre_above_graph_top_is_allowed_when_rectangle_overlaps_vertically():
    value = layout()
    value["question_schema"][1].update(drawing_regions=[[703, 353, 900, 502]], label_anchor=[700, 350])
    value["template_answer_regions"][1]["regions"] = [[703, 353, 900, 502]]
    value["template_handwriting_regions"][1]["regions"] = []
    placement = prefreeze(value, 1477, 2064)["placements"][-1]
    assert placement["label_bbox"][2] == round(700 * 1477 / 1000)
    assert placement["label_vertical_overlap_with_main_drawing"] is True


@pytest.mark.parametrize("anchor", [[590, 300], [590, 800], [590, 1], [590, 999]])
def test_geometry_accepts_valid_auxiliary_anchor_without_rewriting_it(anchor):
    value = layout()
    value["question_schema"][1]["label_anchor"] = anchor
    before = copy.deepcopy(value)
    receipt = prefreeze(value)
    assert receipt["placements"]
    assert value == before


def assert_safe_placements(value, receipt):
    for p in receipt["placements"]:
        dx, dy = p["label_layout"]["source_offset_px"]
        x1, y1, x2, y2 = p["label_bbox"]
        protected = [box for item in value["template_answer_regions"] + value["template_handwriting_regions"] for box in item["regions"]]
        protected += [box for q in value["question_schema"] for box in q.get("drawing_regions", [])]
        for l, t, r, b in protected:
            assert not (x1 < r + dx and x2 + 1 > l + dx and y1 < b + dy and y2 + 1 > t + dy)
        assert p["fixed_position_overwrite_allowed"] is False


def test_drawing_anchor_near_page_edge_uses_rail_without_changing_anchor():
    value = layout()
    value["question_schema"][1]["label_anchor"] = [5, 500]
    before = copy.deepcopy(value)
    receipt = prefreeze(value)
    assert_safe_placements(value, receipt)
    assert receipt["placements"][-1]["placement_mode"] == "external_rail"
    assert value == before


def test_pre_freeze_routes_graphic_label_outside_other_question_answer_region():
    value = layout()
    value["template_answer_regions"][0]["regions"] = [[480, 360, 595, 430]]
    assert_safe_placements(value, prefreeze(value))


def test_pre_freeze_routes_colliding_labels_to_separate_lanes():
    value = layout()
    value["question_anchors"][0]["question_number_bbox"] = [550, 380, 590, 414]
    receipt = prefreeze(value)
    assert_safe_placements(value, receipt)
    first, second = [p["label_bbox"] for p in receipt["placements"]]
    assert first[2] < second[0] or second[2] < first[0] or first[3] < second[1] or second[3] < first[1]


def test_pre_freeze_routes_fixed_other_q_off_drawing_itself():
    value = layout()
    value["question_anchors"][0]["question_number_bbox"] = [700, 480, 740, 520]
    assert_safe_placements(value, prefreeze(value))


@pytest.mark.parametrize("mode", ["marked", "blank"])
def test_actual_difference_preserves_drawing_student_pixels_and_one_q_for_mixed(tmp_path, monkeypatch, mode):
    value = layout(mode, "3")
    value["question_schema"][1]["answer_kind"] = "mixed"
    value["question_schema"][1]["drawing_regions"].append([600, 800, 900, 900])
    before = copy.deepcopy(value)
    student = np.full((1000, 1000, 3), 247, np.uint8)
    cv2.rectangle(student, (610, 410), (890, 690), (90, 120, 10), 2)
    cv2.line(student, (650, 600), (820, 480), (10, 10, 10), 3)
    original_student = student.copy()
    plain = np.full_like(student, 255)
    plain[910:930, 10:30] = 35
    monkeypatch.setattr(
        template_evidence_sheets,
        "_answer_only_pixels_v2",
        lambda *args, **kwargs: plain.copy(),
    )
    monkeypatch.setattr(
        template_evidence_columns,
        "_answer_only_pixels_v2",
        lambda *args, **kwargs: plain.copy(),
    )
    result = evidence.build_full_column_difference_evidence_from_aligned(
        student, np.full((1000, 1000), 255, np.uint8), np.full_like(student, 255), value, tmp_path)
    pixels = np.asarray(Image.open(result.evidence_path).convert("RGB"))
    assert np.array_equal(pixels[400:700, 600:900], student[400:700, 600:900])
    assert np.array_equal(pixels[800:900, 600:900], student[800:900, 600:900])
    assert np.array_equal(pixels[910:930, 10:30], plain[910:930, 10:30])
    labels = result.diagnostics["evidence_columns"][0]["label_placements"]
    assert [label["question_number"] for label in labels].count("3") == 1
    assert next(label for label in labels if label["question_number"] == "3")["label_bbox"][2] == 590
    assert result.diagnostics["drawing_label_policy"]["context_pixel_source"] == "global_aligned_student_original_pixels"
    assert result.diagnostics["drawing_label_policy"]["student_graph_reconstructed"] is False
    assert result.diagnostics["template_text_removed"] is False
    protected = np.zeros(student.shape[:2], bool)
    protected[400:700, 600:900] = True
    protected[800:900, 600:900] = True
    for label in labels:
        x, y, right, bottom = label["label_bbox"]
        protected[y:bottom + 1, x:right + 1] = True
    assert np.array_equal(pixels[~protected], plain[~protected])
    assert np.array_equal(student, original_student) and value == before


def test_missing_student_drawing_source_is_review_not_synthetic_fill(tmp_path, monkeypatch):
    value = layout()
    student = np.full((1000, 1000, 3), 255, np.uint8)
    mask = np.full((1000, 1000), 255, np.uint8)
    mask[500, 700] = 0
    monkeypatch.setattr(
        template_evidence_sheets,
        "_answer_only_pixels_v2",
        lambda *args, **kwargs: student.copy(),
    )
    monkeypatch.setattr(
        template_evidence_columns,
        "_answer_only_pixels_v2",
        lambda *args, **kwargs: student.copy(),
    )
    with pytest.raises(evidence.TemplateEvidenceError, match="照片不完整"):
        evidence.build_full_column_difference_evidence_from_aligned(student, mask, student, value, tmp_path)
    assert not list(tmp_path.glob("evidence-*.png"))


def test_artifact_roundtrip_retains_new_metadata_and_legacy_ordinary_solve(tmp_path):
    source = tmp_path / "template.png"
    Image.new("RGB", (1000, 1000), "white").save(source)
    value = layout()
    artifact = evidence.build_template_artifact(
        source, tmp_path / "artifact-root", 0, value["question_schema"], template_mode="marked",
        question_anchors=value["question_anchors"],
        template_answer_regions=value["template_answer_regions"],
        template_handwriting_regions=value["template_handwriting_regions"])
    verified = evidence.verify_template_artifacts(tmp_path / "artifact-root", {"page_1": artifact})["page_1"]
    assert verified["question_schema"] == artifact["question_schema"]
    assert verified["question_schema"][1]["directory_node_id"] == "n10"
    assert verified["question_schema"][1]["label_anchor"] == [590, 397]
    assert "answer_kind" not in verified["question_schema"][0]


def test_geometry_drawing_may_omit_printed_bbox_but_normal_solve_may_not():
    value = layout()
    value["question_anchors"][1].pop("question_number_bbox")
    assert prefreeze(value)["placements"][-1]["source"] == "drawing_region_left"
    value["question_anchors"][0].pop("question_number_bbox")
    with pytest.raises(ValueError, match="缺少作答框或印刷题号"):
        prefreeze(value)


def test_legacy_without_drawing_does_not_apply_new_geometry_gates():
    receipt = evidence.validate_drawing_label_geometry(
        [{"question_number": "22", "question_type": "solve"}], [], [], [], page_width=0, page_height=0)
    assert receipt["placements"] == []


def two_column_plan(width=1000, height=1000):
    return build_anchor_crop_plan(
        {"column_layout": {"column_count": 2, "columns": [
            {"column_index": 1, "x1_1000": 0, "x2_1000": 500},
            {"column_index": 2, "x1_1000": 500, "x2_1000": 1000}]}},
        artifact_sha256="a" * 64, expected_question_numbers=["2.2", "22"])


def test_actual_frozen_column_conversion_uses_page_width_not_column_width(tmp_path, monkeypatch):
    value = layout("marked", "22")
    value["question_schema"][1].update(drawing_regions=[[750, 400, 950, 700]], label_anchor=[740, 500])
    value["question_crop_plan"] = two_column_plan()
    value["template_answer_regions"][1]["regions"] = [[750, 400, 950, 700]]
    value["template_handwriting_regions"][1]["regions"] = []
    student = np.full((1000, 1000, 3), 245, np.uint8)
    monkeypatch.setattr(
        template_evidence_sheets,
        "_answer_only_pixels_v2",
        lambda *args, **kwargs: np.full_like(student, 255),
    )
    monkeypatch.setattr(
        template_evidence_columns,
        "_answer_only_pixels_v2",
        lambda *args, **kwargs: np.full_like(student, 255),
    )
    result = evidence.build_full_column_difference_evidence_from_aligned(
        student, np.full((1000, 1000), 255, np.uint8), student, value, tmp_path)
    assert len(result.evidence_paths) == 2
    second = result.diagnostics["evidence_columns"][1]
    placement = second["label_placements"][0]
    assert placement["question_number"] == "22"
    assert placement["label_bbox"][2] == 240
    assert np.array_equal(np.asarray(Image.open(result.evidence_paths[1]))[400:700, 250:450], student[400:700, 750:950])


def test_exemption_mask_uses_union_of_answer_and_handwriting_boxes():
    merged = evidence._merge_template_exemption_regions(
        [{"question_number": "2.2", "regions": [[100, 200, 600, 500]]}],
        [{"question_number": "2.2", "regions": [[150, 250, 450, 400]]}],
    )
    assert merged == [{
        "question_number": "2.2",
        "regions": [[100, 200, 600, 500], [150, 250, 450, 400]],
    }]


def test_main_graph_crossing_frozen_column_boundary_uses_safe_placement():
    value = layout("marked", "22")
    value["question_schema"][1].update(drawing_regions=[[450, 400, 700, 700]], label_anchor=[440, 500])
    value["question_crop_plan"] = two_column_plan()
    receipt = prefreeze(value)
    assert receipt["placements"]


def test_real_matcher_transmits_drawing_metadata_and_preserves_aligned_graph_pixels(tmp_path):
    rng = np.random.default_rng(72)
    template = np.full((1000, 1000, 3), 255, np.uint8)
    for _ in range(260):
        x, y = (int(v) for v in rng.integers(40, 960, size=2))
        cv2.circle(template, (x, y), int(rng.integers(2, 7)), (35, 35, 35), -1)
    for point in range(610, 901, 30):
        cv2.line(template, (point, 400), (point, 700), (110, 110, 110), 1)
    source = tmp_path / "synthetic-template.png"
    Image.fromarray(template).save(source)
    marked = template.copy()
    cv2.line(marked, (650, 650), (810, 430), (0, 0, 0), 3)
    transform = cv2.getPerspectiveTransform(
        np.float32([[0, 0], [999, 0], [999, 999], [0, 999]]),
        np.float32([[40, 30], [1050, 15], [1060, 1080], [20, 1050]]))
    student_path = tmp_path / "synthetic-photo.png"
    Image.fromarray(cv2.warpPerspective(marked, transform, (1100, 1120), borderValue=(255, 255, 255))).save(student_path)
    hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (source, student_path)}
    value = layout("blank")
    root = tmp_path / "artifacts"
    artifact = evidence.build_template_artifact(
        source, root, 0, value["question_schema"], template_mode="blank",
        question_anchors=value["question_anchors"], template_answer_regions=value["template_answer_regions"],
        template_handwriting_regions=[])
    result = evidence.TemplateEvidenceMatcher({"page_1": artifact}, root).match_and_build(
        student_path, tmp_path / "evidence")
    assert result.question_numbers == ["2.2", "2.3"]
    assert result.diagnostics["drawing_label_policy"]["placed_labels"] == 1
    assert result.diagnostics["drawing_label_policy"]["student_graph_reconstructed"] is False
    rendered = np.asarray(Image.open(result.evidence_path).convert("RGB"))
    aligned = np.asarray(Image.open(result.aligned_path).convert("RGB"))
    assert np.array_equal(rendered[400:700, 600:900], aligned[400:700, 600:900])
    expected = {item["question_number"]: item["label_bbox"] for item in prefreeze(value)["placements"]}
    observed = {item["question_number"]: item["label_bbox"] for item in result.diagnostics["evidence_columns"][0]["label_placements"]}
    assert observed == expected
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in hashes.items())
