"""Golden characterization of ``build_full_column_difference_evidence_from_aligned``.

The function used to be one 424-line body; before it was split this file
pinned, on synthetic pixels only:

* which error a malformed input is refused with when several checks fail at
  once (the order of the checks), and whether the output directory exists
  afterwards (``mkdir`` happens after layout validation, before rendering);
* for accepted inputs, the complete diagnostics (key order included), the
  pixel digest (size + raw RGB) of every evidence PNG and the files left in
  the output directory.

Regenerate only for an intended behaviour change:

    MATHAGENT_UPDATE_GOLDEN=1 pytest tests/test_full_column_evidence_characterization.py
"""
import copy
import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from services import template_evidence as evidence
from services.template_anchor_crops import build_anchor_crop_plan


GOLDEN = Path(__file__).parent / "golden" / "full_column_evidence.json"
UPDATE_GOLDEN = os.environ.get("MATHAGENT_UPDATE_GOLDEN") == "1"
HEIGHT, WIDTH = 400, 600


def _pixels():
    aligned = np.full((HEIGHT, WIDTH, 3), 250, np.uint8)
    aligned[60:90, 200:420] = 20      # answer ink, question 1
    aligned[190:230, 120:300] = 35    # answer ink, question 2
    aligned[300:340, 380:560] = 15    # answer ink, question 3
    template = np.full((HEIGHT, WIDTH, 3), 250, np.uint8)
    template[20:30, 10:40] = 0        # printed number glyphs
    template[150:160, 10:40] = 0
    template[270:280, 310:340] = 0
    aligned[20:30, 10:40] = 0
    aligned[150:160, 10:40] = 0
    aligned[270:280, 310:340] = 0
    mask = np.full((HEIGHT, WIDTH), 255, np.uint8)
    return aligned, mask, template


def _plan(columns: int, numbers: list[str]):
    return build_anchor_crop_plan({"column_layout": {"column_count": columns, "columns": [
        {"column_index": index + 1, "x1_1000": round(index * 1000 / columns),
         "x2_1000": round((index + 1) * 1000 / columns)}
        for index in range(columns)
    ]}}, artifact_sha256="b" * 64, expected_question_numbers=numbers)


def _layout(kind: str = "fill", mode: str = "blank", columns: int = 1, *, regions: bool = True):
    numbers = ["1", "2", "3"]
    boxes = {"1": [300, 120, 720, 260], "2": [180, 450, 520, 600], "3": [620, 720, 950, 880]}
    numbers_bbox = {"1": [15, 45, 70, 80], "2": [15, 370, 70, 405], "3": [515, 670, 570, 705]}
    layout = {
        "page_type_id": "page_1",
        "template_mode": mode,
        "question_numbers": numbers,
        "question_anchors": [
            {"question_number": number, "question_number_bbox": numbers_bbox[number], "human_confirmed": True}
            for number in numbers
        ],
        "question_schema": [{"question_number": number, "question_type": kind} for number in numbers],
    }
    if regions:
        answer_regions = [{"question_number": number, "regions": [boxes[number]]} for number in numbers]
        layout["template_answer_regions"] = answer_regions
        layout["template_handwriting_regions"] = copy.deepcopy(answer_regions)
    if columns > 1:
        layout["question_crop_plan"] = _plan(columns, numbers)
    return layout


def _solve_without_regions(columns: int = 1):
    return _layout("solve", "marked", columns, regions=False)


def _unmarked_across_columns():
    """Question 2: number in column 1, frozen box in column 2, ink before the number."""
    layout = _layout("fill", "blank", 2)
    for anchor in layout["question_anchors"]:
        if anchor["question_number"] == "2":
            anchor["question_number_bbox"] = [300, 450, 350, 480]
    for key in ("template_answer_regions", "template_handwriting_regions"):
        for item in layout[key]:
            if item["question_number"] == "2":
                item["regions"] = [[600, 440, 900, 600]]
    for item in layout["question_schema"]:
        if item["question_number"] == "2":
            item["answer_position"] = "unmarked"
    return layout


def _unmarked_pixels(aligned, mask, template, layout):
    aligned[190:230, 120:300] = 250   # no ink inside question 2's frozen box area
    template[180:192, 180:210] = 0    # printed "2" at the number box
    aligned[180:192, 180:210] = 0
    aligned[178:196, 40:150] = 25     # student ink in the margin before the number
    return aligned, mask, template, layout


def _drop_anchor_bbox(layout, number="2"):
    for anchor in layout["question_anchors"]:
        if anchor["question_number"] == number:
            anchor.pop("question_number_bbox")
    return layout


def _cases():
    """name -> (aligned, mask, template, layout) builders."""

    def base(layout_builder=_layout, **pixel_changes):
        def build():
            aligned, mask, template = _pixels()
            layout = layout_builder()
            return pixel_changes.get("mutate", lambda *a: a)(aligned, mask, template, layout)
        return build

    def mutate(function):
        return {"mutate": function}

    bad_image = lambda a, m, t, layout: (a.astype(np.int16), m, t, layout)  # noqa: E731
    bad_mask = lambda a, m, t, layout: (a, np.where(m == 255, 7, 0).astype(np.uint8), t, layout)  # noqa: E731

    def with_layout(change):
        return lambda a, m, t, layout: (a, m, t, change(layout))

    def chain(*changes):
        def apply(a, m, t, layout):
            for change in changes:
                a, m, t, layout = change(a, m, t, layout)
            return a, m, t, layout
        return apply

    def duplicate_numbers(layout):
        layout["question_numbers"] = ["1", "1", "3"]
        return layout

    def set_key(key, value):
        def change(layout):
            layout[key] = value
            return layout
        return change

    def drop_region(number):
        def change(layout):
            for key in ("template_answer_regions", "template_handwriting_regions"):
                layout[key] = [item for item in layout[key] if item["question_number"] != number]
            return layout
        return change

    def schema_mismatch(layout):
        layout["question_schema"] = layout["question_schema"][:2]
        return layout

    def anchor_mismatch(layout):
        layout["question_anchors"] = layout["question_anchors"][:2]
        return layout

    return {
        # accepted
        "fill_blank_one_column": base(lambda: _layout("fill", "blank", 1)),
        "fill_marked_two_columns": base(lambda: _layout("fill", "marked", 2)),
        "solve_with_regions_two_columns": base(lambda: _layout("solve", "blank", 2)),
        "solve_printed_numbers_one_column": base(lambda: _solve_without_regions(1)),
        "solve_printed_numbers_two_columns": base(lambda: _solve_without_regions(2)),
        "legacy_solve_fallback": base(lambda: _drop_anchor_bbox({
            **_solve_without_regions(1), "_legacy_solve_label_fallback": True,
        })),
        "prewhitened_baseline": base(lambda: {**_layout(), "template_baseline_prewhitened": True}),
        "caller_diagnostics_kept": base(lambda: _layout("fill", "marked", 1)),
        "unmarked_answer_moves_across_columns": base(_unmarked_across_columns, **mutate(_unmarked_pixels)),
        # refused, in check order
        "snapshot_not_dict_beats_bad_image": base(**mutate(chain(bad_image, lambda a, m, t, layout: (a, m, t, ["not", "a", "dict"])))),
        "bad_page_type_beats_bad_image": base(**mutate(chain(bad_image, with_layout(set_key("page_type_id", "../x"))))),
        "bad_image_beats_bad_mask": base(**mutate(chain(bad_image, bad_mask))),
        "bad_mask_beats_bad_sequence": base(**mutate(chain(bad_mask, with_layout(duplicate_numbers)))),
        "bad_sequence": base(**mutate(with_layout(duplicate_numbers))),
        "anchors_do_not_match": base(**mutate(with_layout(anchor_mismatch))),
        "schema_does_not_match": base(**mutate(with_layout(schema_mismatch))),
        "solve_without_box_or_number": base(lambda: _drop_anchor_bbox(_solve_without_regions(1))),
        "blank_region_missing": base(**mutate(with_layout(drop_region("2")))),
        "prewhitened_not_bool": base(**mutate(with_layout(set_key("template_baseline_prewhitened", "yes")))),
        "sequence_beats_prewhitened": base(**mutate(with_layout(lambda layout: set_key("template_baseline_prewhitened", 1)(duplicate_numbers(layout))))),
        "coverage_beats_prewhitened": base(**mutate(with_layout(lambda layout: set_key("template_baseline_prewhitened", "yes")(drop_region("2")(layout))))),
    }


def _png_digest(path: str) -> str:
    # Pin the decoded pixels, not the encoded bytes: PNG bytes depend on the
    # zlib build behind Pillow (zlib-ng and stock zlib deflate differently),
    # so a byte hash turns a pure encoder change into a golden failure on any
    # machine or CI image with a different Pillow wheel. Size + raw RGB keeps
    # the comparison pixel-exact and stays stable across Pillow 11/12.
    with Image.open(path) as image:
        rgb = image.convert("RGB")
        body = f"{rgb.size[0]}x{rgb.size[1]}".encode() + rgb.tobytes()
    return hashlib.sha256(body).hexdigest()


def _scrub(value, root: str):
    if isinstance(value, dict):
        return {key: _scrub(item, root) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(item, root) for item in value]
    if isinstance(value, str) and root in value:
        return value.replace(root, "<OUT>").replace("\\", "/")
    return value


def _run(name, build, tmp_path):
    aligned, mask, template, layout = build()
    before = copy.deepcopy(layout)
    output = tmp_path / name / "evidence"
    caller_diagnostics = {"caller": "kept", "order": [1, 2]} if name == "caller_diagnostics_kept" else None
    observed = {}
    try:
        result = evidence.build_full_column_difference_evidence_from_aligned(
            aligned, mask, template, layout, output, diagnostics=caller_diagnostics,
        )
    except Exception as exc:  # noqa: BLE001 - the refusal is what is pinned
        observed["raised"] = [type(exc).__name__, str(exc)]
    else:
        root = str(output.resolve())
        observed["result"] = {
            "page_type_id": result.page_type_id,
            "evidence_png_sha256": [_png_digest(path) for path in result.evidence_paths],
            "evidence_path_is_first": result.evidence_path == (result.evidence_paths[0] if result.evidence_paths else ""),
            "question_numbers": result.question_numbers,
            "blank_question_numbers": result.blank_question_numbers,
            "diagnostics": _scrub(result.diagnostics, root),
        }
    observed["layout_unchanged"] = layout == before
    observed["output_dir_exists"] = output.is_dir()
    observed["files_left"] = len(list(output.iterdir())) if output.is_dir() else 0
    return json.loads(json.dumps(observed, ensure_ascii=False, default=str))


def test_full_column_evidence_golden(tmp_path):
    observed = {name: _run(name, build, tmp_path) for name, build in _cases().items()}
    rendered = json.dumps(observed, ensure_ascii=False, indent=1) + "\n"
    if UPDATE_GOLDEN:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(rendered, encoding="utf-8", newline="\n")
        return
    expected = GOLDEN.read_text(encoding="utf-8")
    assert json.loads(rendered) == json.loads(expected)
    assert rendered == expected, "same values, different key order"


@pytest.mark.parametrize("name", sorted(_cases()))
def test_full_column_evidence_case_is_deterministic(tmp_path, name):
    first = _run(name, _cases()[name], tmp_path / "first")
    second = _run(name, _cases()[name], tmp_path / "second")
    assert first == second
