"""Synthetic regression inputs, not model responses or real student records."""
import copy

import numpy as np
import pytest
from PIL import Image

from services import template_evidence as evidence


@pytest.mark.parametrize("size", [(320, 240), (1200, 800), (2400, 1600)])
@pytest.mark.parametrize("mode", ["blank", "marked"])
@pytest.mark.parametrize("kind", ["fill", "solve"])
def test_dense_edge_long_labels_preserve_every_foreground_pixel(tmp_path, monkeypatch, size, mode, kind):
    width, height = size
    # Pale full-page signal deliberately removes every in-page safe gutter.
    pixels = np.full((height, width, 3), 254, np.uint8)
    pixels[height // 4:height // 2:3, :] = 30
    regions = [
        {"question_number": "1.22.333.4444", "regions": [[0, 200, 650, 420]]},
        {"question_number": "2", "regions": [[300, 410, 1000, 630]]},
        {"question_number": "3", "regions": [[0, 980, 1000, 1000]]},
    ]
    anchors = [{"question_number": r["question_number"], "question_number_bbox": [0, r["regions"][0][1], 20, r["regions"][0][1] + 10], "human_confirmed": True} for r in regions]
    layout = {"page_type_id": "page_1", "template_mode": mode,
              "question_label_policy": evidence.PRINTED_SOLVE_LABEL_POLICY,
              "question_numbers": [r["question_number"] for r in regions],
              "question_schema": [{"question_number": r["question_number"], "question_type": kind} for r in regions],
              "question_anchors": anchors, "template_answer_regions": regions,
              "template_handwriting_regions": regions}
    before = copy.deepcopy(layout)
    monkeypatch.setattr(evidence, "_answer_only_pixels_v2", lambda *a, **kw: pixels.copy())
    result = evidence.build_full_column_difference_evidence_from_aligned(
        pixels, np.full((height, width), 255, np.uint8), np.full_like(pixels, 255), layout, tmp_path)
    column = result.diagnostics["evidence_columns"][0]
    with Image.open(result.evidence_path) as im:
        rendered = np.asarray(im)
    mapping = column.get("label_layout", {})
    x, y = mapping.get("source_offset_px", [0, 0])
    assert np.array_equal(rendered[y:y + height, x:x + width], pixels)
    assert layout == before
    assert column["question_numbers"] == layout["question_numbers"]
    labels = column["label_placements"]
    assert len(labels) == len(regions)
    for label in labels:
        l, t, r, b = label["label_bbox"]
        assert 0 <= l <= r < x  # Entire opaque label outside the source column.
        assert 0 <= t < b < rendered.shape[0]
        assert label["foreground_preserved"] is True
    from services.drawing_grading import reconstruct_canvas
    assert np.array_equal(reconstruct_canvas(result.evidence_paths, result.diagnostics), pixels)


def test_separator_cannot_recolor_bottom_answer_stroke(tmp_path, monkeypatch):
    pixels = np.full((800, 1200, 3), 255, np.uint8)
    pixels[319:321, 600:840] = 0
    regions = [{"question_number": "1", "regions": [[500, 300, 700, 400]]}]
    monkeypatch.setattr(evidence, "_answer_only_pixels_v2", lambda *a, **kw: pixels.copy())
    result = evidence.build_full_column_difference_evidence_from_aligned(
        pixels, np.full((800, 1200), 255, np.uint8), np.full_like(pixels, 255),
        {"page_type_id": "page_1", "template_mode": "blank", "question_numbers": ["1"],
         "template_answer_regions": regions}, tmp_path)
    with Image.open(result.evidence_path) as im:
        actual = np.asarray(im)
    x, y = result.diagnostics["evidence_columns"][0].get("label_layout", {}).get("source_offset_px", [0, 0])
    assert np.array_equal(actual[y + 319:y + 321, x + 600:x + 840], pixels[319:321, 600:840])


def test_drawing_text_mask_applies_source_offset_and_keeps_rail(tmp_path, monkeypatch):
    from services.drawing_grading import build_text_evidence, reconstruct_canvas
    source = np.full((400, 600, 3), 254, np.uint8)
    source[50:350:3, :] = 20
    questions = [{"question_number": "1", "question_type": "fill"},
        {"question_number": "2", "question_type": "solve", "answer_kind": "mixed",
         "drawing_regions": [[500, 300, 900, 700]], "label_anchor": [490, 450]}]
    regions = [{"question_number": "1", "regions": [[0, 10, 400, 250]]},
               {"question_number": "2", "regions": [[500, 720, 900, 990]]}]
    monkeypatch.setattr(evidence, "_answer_only_pixels_v2", lambda *a, **kw: source.copy())
    result = evidence.build_full_column_difference_evidence_from_aligned(
        source, np.full(source.shape[:2], 255, np.uint8), np.full_like(source, 255),
        {"page_type_id": "page_1", "template_mode": "marked", "question_numbers": ["1", "2"],
         "question_schema": questions, "template_answer_regions": regions}, tmp_path / "evidence")
    paths = build_text_evidence(result.evidence_paths, result.diagnostics, questions, str(tmp_path / "text"))
    expected = source.copy()
    expected[120:280, 300:540] = 255
    assert np.array_equal(reconstruct_canvas(paths, result.diagnostics), expected)
    dx = result.diagnostics["evidence_columns"][0]["label_layout"]["source_offset_px"][0]
    assert dx > 0
    with Image.open(paths[0]) as text, Image.open(result.evidence_path) as graphic:
        assert np.array_equal(np.array(text)[:, :dx], np.array(graphic)[:, :dx])
    for malformed in [[], {"version": "bad"}, {**result.diagnostics["evidence_columns"][0]["label_layout"], "source_offset_px": [0, 0]}]:
        broken = copy.deepcopy(result.diagnostics)
        broken["evidence_columns"][0]["label_layout"] = malformed
        with pytest.raises(ValueError, match="label source"):
            reconstruct_canvas(paths, broken)
