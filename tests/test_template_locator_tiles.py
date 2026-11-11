"""Locator tiles: planning, tile-local coordinates, page stitching and pipeline wiring.

The fake Max locator below is a perfect reader of whatever image it is given:
it finds each owned box by its colour and reports the coordinates of that
image. A stitched page result is therefore right only if the tile was cut,
stamped, addressed and mapped back correctly.
"""
import asyncio
import copy
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageDraw

import config
from services import flash_grading
from services import template_directory_pipeline as pipeline
from services import template_locator_tiles as tiles
from services import template_one_three as contract
from services.teaching_context import make_teaching_context, use_teaching_context
from tests.test_template_one_three import v4_payloads

PAGE = (1000, 1500)
COLUMNS = [{"column_index": 1, "x1_1000": 0, "x2_1000": 500}, {"column_index": 2, "x1_1000": 500, "x2_1000": 1000}]
# Twelve questions, six per column; question 3 has two answer positions.
COUNTS = (1, 1, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1)
PHASE = "template-directory-v1"


def colour(number, role):
    """Red stays 0: no grid line or label blended over a box can produce it."""
    return (0, 20 * number, 60 + 60 * role)


def truth_px():
    """question number -> [number box, answer box, ...] in page pixels."""
    result = {}
    for index, count in enumerate(COUNTS):
        x, y = (index // 6) * 500, (index % 6) * 250
        boxes = [[x + 83, y + 39, x + 113, y + 69], [x + 303, y + 33, x + 437, y + 93]]
        if count == 2:
            boxes.append([x + 153, y + 123, x + 277, y + 183])
        result[str(index + 1)] = boxes
    return result


def normalized(box, size=PAGE):
    return [box[0] * 1000 / size[0], box[1] * 1000 / size[1], box[2] * 1000 / size[0], box[3] * 1000 / size[1]]


def draw_page(path):
    page = Image.new("RGB", PAGE, "white")
    draw = ImageDraw.Draw(page)
    for number, boxes in truth_px().items():
        for role, box in enumerate(boxes):
            draw.rectangle((box[0], box[1], box[2] - 1, box[3] - 1), fill=colour(int(number), role))
    page.save(path)


def find_box(images, rgb):
    """Bounding box of one colour across images laid side by side, in 0..1000."""
    offset, found = 0, None
    for image in images:
        mask = None
        for band, value in zip(image.split(), rgb):
            hit = band.point(lambda v, value=value: 255 if v == value else 0)
            mask = hit if mask is None else ImageChops.multiply(mask, hit)
        box = mask.getbbox()
        if box:
            box = [box[0] + offset, box[1], box[2] + offset, box[3]]
            found = box if found is None else [min(found[0], box[0]), min(found[1], box[1]),
                                               max(found[2], box[2]), max(found[3], box[3])]
        offset += image.width
    assert found is not None, f"the request images do not show {rgb}"
    return normalized(found, (offset, images[0].height))


def perfect_locator(image_paths, prompt):
    """Locate the directory questions in the request's own image space."""
    owned = json.loads(re.search(r"\n目录：(.*)\n返回", prompt).group(1))
    images = [Image.open(path).convert("RGB") for path in image_paths]
    questions = []
    for row in owned:
        number = row["q_id"][1:]
        questions.append({
            "question_number": number, "question_number_bbox": find_box(images, colour(int(number), 0)),
            "question_type": "fill", "choice_mode": "unknown", "label_anchor": None,
            "regions": [{"slot_id": slot["slot_id"], "kind": "answer",
                         "bbox": find_box(images, colour(int(number), role))}
                        for role, slot in enumerate(row["slots"], 1)]})
    return {"coordinate_space": "normalized_1000_v1", "questions": questions, "needs_review": []}


def coarse_payload():
    return {"coordinate_space": "normalized_1000_v1", "questions": [
        {"question_number": str(index + 1),
         "bbox": [(index // 6) * 500, (index % 6) * 1000 / 6, (index // 6) * 500 + 500, (index % 6 + 1) * 1000 / 6]}
        for index in range(len(COUNTS))]}


def v4_contract(counts):
    raw, _ = v4_payloads(counts)
    return contract.question_contract(contract.receive_hierarchy(raw))


def test_plan_gives_every_question_one_tile_by_column_then_reading_order():
    manifest = v4_contract(COUNTS)
    plan = tiles.plan_tiles(manifest, coarse_payload(), COLUMNS, width=1000, height=1500, max_questions=3)
    assert [tile["numbers"] for tile in plan] == [
        ["1", "2", "3"], ["4", "5", "6"], ["7", "8", "9"], ["10", "11", "12"]]
    # Whole column width plus padding, clamped to the page; rows padded too.
    assert [tile["box"] for tile in plan] == [
        [0, 0, 530, 840], [0, 660, 530, 1500], [470, 0, 1000, 840], [470, 660, 1000, 1500]]
    # Runs are balanced rather than "full, then the rest".
    balanced = tiles.plan_tiles(manifest, coarse_payload(), COLUMNS, width=1000, height=1500, max_questions=4)
    assert [len(tile["numbers"]) for tile in balanced] == [3, 3, 3, 3]
    by_column = tiles.plan_tiles(manifest, coarse_payload(), COLUMNS, width=1000, height=1500, max_questions=20)
    assert [tile["numbers"] for tile in by_column] == [[str(n) for n in range(1, 7)], [str(n) for n in range(7, 13)]]


def test_a_tile_holds_a_coarse_box_that_leaves_its_column_and_is_never_too_short():
    manifest = v4_contract((1, 1, 1))
    coarse = {"coordinate_space": "normalized_1000_v1", "questions": [
        {"question_number": "1", "bbox": [20, 100, 480, 140]},
        # Centre in the left column, options continue into the right one.
        {"question_number": "2", "bbox": [20, 900, 700, 940]},
        {"question_number": "3", "bbox": [520, 100, 980, 140]}]}
    plan = tiles.plan_tiles(manifest, coarse, COLUMNS, width=2000, height=1000, max_questions=1)
    assert [tile["numbers"] for tile in plan] == [["1"], ["2"], ["3"]]
    assert plan[1]["box"][2] == 1460  # (700 + 30) of 1000, on a 2000 px page
    # A 40-high hint still gets a quarter of the page, kept inside it.
    assert [tile["box"][3] - tile["box"][1] for tile in plan] == [250, 250, 250]
    assert plan[0]["box"][1] == 0 and plan[1]["box"][3] == 1000


@pytest.mark.parametrize("case", ["off", "no_coarse", "one_tile", "wrong_numbers", "duplicate_numbers"])
def test_no_plan_keeps_the_single_page_request(case):
    manifest = v4_contract(COUNTS)
    coarse = coarse_payload()
    limit = 3
    if case == "off":
        limit = 0
    elif case == "no_coarse":
        coarse = None
    elif case == "one_tile":
        manifest, limit = v4_contract((1, 1)), 20
        coarse = {"coordinate_space": "normalized_1000_v1", "questions": coarse["questions"][:2]}
    elif case == "wrong_numbers":
        coarse["questions"][0]["question_number"] = "99"
    else:
        coarse["questions"][1]["question_number"] = "1"
    assert tiles.plan_tiles(manifest, coarse, COLUMNS, width=1000, height=1500, max_questions=limit) == []


def uneven_column(heights):
    """One column of single-slot questions stacked with the given heights."""
    tops = [sum(heights[:index]) for index in range(len(heights))]
    coarse = {"coordinate_space": "normalized_1000_v1", "questions": [
        {"question_number": str(index + 1), "bbox": [20, top, 480, top + high]}
        for index, (top, high) in enumerate(zip(tops, heights))]}
    return v4_contract((1,) * len(heights)), coarse


def test_a_tall_run_is_split_even_when_it_is_within_the_question_limit():
    # Five tall questions then ten short ones: by count alone the first tile
    # would carry most of the page height.
    manifest, coarse = uneven_column([100] * 5 + [30] * 10)
    column = [{"column_index": 1, "x1_1000": 0.0, "x2_1000": 1000.0}]
    plan = lambda **limits: [len(tile["numbers"]) for tile in tiles.plan_tiles(  # noqa: E731
        manifest, coarse, column, width=1000, height=1000, **limits)]
    assert plan(max_questions=8) == [8, 7]
    # 800 of height needs three runs of at most 300. Heights 300 / 290 / 210:
    # of the splits that fit, this one leaves the two later tiles most even.
    assert plan(max_questions=8, max_span=300) == [3, 5, 7]
    # The height limit alone is enough to split; the question limit still holds.
    assert plan(max_questions=20, max_span=450) == [4, 11]


def test_a_question_taller_than_the_span_limit_keeps_a_tile_of_its_own():
    manifest, coarse = uneven_column([600, 100, 100])
    column = [{"column_index": 1, "x1_1000": 0.0, "x2_1000": 1000.0}]
    plan = tiles.plan_tiles(manifest, coarse, column, width=1000, height=1000, max_questions=8, max_span=250)
    assert [tile["numbers"] for tile in plan] == [["1"], ["2", "3"]]


def test_tiling_is_configured_per_page_and_only_for_shared_slots(monkeypatch):
    monkeypatch.setattr(config, "OCR_TEMPLATE_LOCATOR_TILE_QUESTIONS", 8)
    monkeypatch.setattr(config, "OCR_TEMPLATE_LOCATOR_TILE_SPAN", 400)
    assert tiles.configured_limits({"question_identity_version": 4}) == {"max_questions": 8, "max_span": 400.0}
    assert tiles.configured_limits({"question_identity_version": 3})["max_questions"] == 0
    monkeypatch.setattr(config, "OCR_TEMPLATE_LOCATOR_TILE_QUESTIONS", 0)
    assert tiles.configured_limits({"question_identity_version": 4})["max_questions"] == 0


def located_row(number, box, regions, *, drawing=(), anchor=None):
    return {"question_number": number, "question_number_bbox": box, "regions": regions,
            "drawing_regions": list(drawing), "label_anchor": anchor}


def test_tile_coordinates_map_back_to_the_page_and_placeholders_survive():
    tile_box = [470, 660, 1000, 1500]  # 530 x 840 px of a 1000 x 1500 page
    located = {"questions": [
        located_row("1", [0, 0, 1000, 1000], [[100, 250, 500, 750], []], drawing=[[100, 250, 500, 750]],
                    anchor=[100, 500]),
        located_row("2", None, [[]])], "needs_review": []}
    original = copy.deepcopy(located)
    page = tiles.to_page(located, tile_box, width=1000, height=1500)
    assert located == original
    first, second = page["questions"]
    assert first["question_number_bbox"] == [470.0, 440.0, 1000.0, 1000.0]
    assert first["regions"] == [[523.0, 580.0, 735.0, 860.0], []]
    assert first["drawing_regions"] == [[523.0, 580.0, 735.0, 860.0]]
    assert first["label_anchor"] == [523.0, 720.0]
    assert second["question_number_bbox"] is None and second["regions"] == [[]]


def test_edge_contact_ignores_the_pages_own_edges():
    located = {"questions": [
        located_row("1", [0, 0, 40, 30], [[300, 950, 700, 1000]]),
        located_row("2", [100, 100, 140, 130], [[900, 400, 999, 450]], drawing=[[1, 500, 80, 600]])]}
    inner = tiles.edge_contact(located, [100, 100, 500, 500], width=1000, height=1000)
    assert inner == {"left": ["1", "2"], "top": ["1"], "right": ["2"], "bottom": ["1"]}
    corner = tiles.edge_contact(located, [0, 0, 500, 500], width=1000, height=1000)
    assert corner == {"right": ["2"], "bottom": ["1"]}
    assert tiles.edge_contact(located, [0, 0, 1000, 1000], width=1000, height=1000) == {}
    assert tiles.widen([100, 100, 500, 500], inner, width=1000, height=1000) == [20, 20, 580, 580]
    assert tiles.widen([10, 100, 500, 990], {"left": ["1"], "bottom": ["1"]}, width=1000, height=1000) == [0, 100, 500, 1000]


def raw_question(row, number_box, answer_box):
    return {"question_number": row["q_id"][1:], "question_number_bbox": number_box, "question_type": "fill",
            "choice_mode": "unknown", "label_anchor": None,
            "regions": [{"slot_id": row["slots"][0]["slot_id"], "kind": "answer", "bbox": answer_box}]}


def run_two_tiles(tmp_path, answer_boxes_for_tile_one, fail_tile=None):
    """Two stacked tiles on a 400 x 900 page; tile 1 answers with the given boxes in turn."""
    manifest = v4_contract((1, 1))
    page = Image.new("RGB", (400, 900), "white")
    plan = [{"numbers": ["1"], "box": [0, 0, 400, 300]}, {"numbers": ["2"], "box": [0, 300, 400, 600]}]
    coarse = {"coordinate_space": "normalized_1000_v1", "questions": [
        {"question_number": "1", "bbox": [0, 0, 1000, 300]}, {"question_number": "2", "bbox": [0, 350, 1000, 650]}]}
    calls, cancelled, report = [], [], []
    pending = list(answer_boxes_for_tile_one)

    async def request(images, prompt, phase, validator, **kwargs):
        number = kwargs["expected_ids"][0]
        with Image.open(images[0]) as image:
            calls.append({"stage_key": kwargs["stage_key"], "size": image.size, "prompt": prompt,
                          "expected_ids": kwargs["expected_ids"]})
        if fail_tile == number:
            await asyncio.sleep(0)
            raise ValueError("tile failed")
        if fail_tile:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.append(kwargs["stage_key"])
                raise
        row = manifest[int(number) - 1]
        box = pending.pop(0) if number == "1" else [600, 400, 900, 600]
        return validator({"coordinate_space": "normalized_1000_v1", "needs_review": [],
                          "questions": [raw_question(row, [50, 400, 100, 500], box)]})

    async def run():
        return await asyncio.wait_for(tiles.locate_in_tiles(
            request, page, tmp_path, manifest, coarse, plan,
            columns=[{"column_index": 1, "x1_1000": 0.0, "x2_1000": 1000.0}],
            phase=PHASE, max_tokens=100, report=report), 10)

    try:
        return asyncio.run(run()), calls, report, cancelled
    except ValueError as exc:
        return exc, calls, report, cancelled


def test_a_box_at_a_cut_edge_is_asked_again_on_a_wider_tile(tmp_path):
    # First answer reaches the bottom cut; the top is the page edge and is fine.
    result, calls, report, _ = run_two_tiles(tmp_path, [[600, 0, 900, 1000], [600, 0, 900, 500]])
    first = [call for call in calls if call["expected_ids"] == ("1",)]
    assert [call["stage_key"] for call in first] == [f"{PHASE}#1", f"{PHASE}#1-wide"]
    assert [call["size"] for call in first] == [(400, 300), (400, 372)]
    # Each request is told the crop it actually carries: 300 and then 372 of the page's 900 px.
    assert "纵向0%–33%" in first[0]["prompt"] and "纵向0%–41%" in first[1]["prompt"]
    assert report[0] == {"question_numbers": ["1"], "box_px": [0, 0, 400, 372], "attempts": 2, "edge_contact": {}}
    assert report[1]["attempts"] == 1
    assert result["needs_review"] == []
    # Mapped with the widened tile, not the planned one: 500 of 372 px on a 900 px page.
    assert result["questions"][0]["regions"] == [[600.0, 0.0, 900.0, 206.667]]
    assert result["questions"][1]["regions"] == [[600.0, 466.667, 900.0, 533.333]]
    assert [q["question_number"] for q in result["questions"]] == ["1", "2"]
    assert result["slot_source"] == contract.max_owned.SHARED_SLOT_SOURCE and result["max_flags"] == {}


def test_a_box_still_at_the_cut_edge_goes_to_teacher_review(tmp_path):
    result, calls, report, _ = run_two_tiles(tmp_path, [[600, 0, 900, 1000], [600, 0, 900, 999]])
    assert len(calls) == 3
    assert report[0]["attempts"] == 2 and report[0]["edge_contact"] == {"bottom": ["1"]}
    assert result["needs_review"] == ["第1题的框贴近定位裁图边缘，可能未框全，请核对"]
    assert result["questions"][0]["regions"] == [[600.0, 0.0, 900.0, 412.92]]


def test_one_failed_tile_cancels_the_others_and_fails_the_locate(tmp_path):
    result, _, _, cancelled = run_two_tiles(tmp_path, [], fail_tile="1")
    assert isinstance(result, ValueError) and "tile failed" in str(result)
    assert cancelled == [f"{PHASE}#2"]


def test_tile_prompt_lists_its_own_questions_with_hints_and_columns_in_tile_coordinates():
    manifest = v4_contract(COUNTS)
    owned = manifest[9:12]
    prompt = tiles.tile_prompt(owned, coarse_payload(), [470, 660, 1000, 1500], columns=COLUMNS,
                               width=1000, height=1500)
    assert [row["q_id"] for row in json.loads(re.search(r"\n目录：(.*)\n返回", prompt).group(1))] == ["Q10", "Q11", "Q12"]
    hints = json.loads(prompt.rsplit("：", 1)[1])
    # Page boxes [500, 500.., 1000, 666..] etc. re-expressed in the tile's own 0..1000.
    assert hints == {"coordinate_space": "normalized_1000_v1", "questions": [
        {"question_number": "10", "bbox": [57, 107, 1000, 405]},
        {"question_number": "11", "bbox": [57, 405, 1000, 702]},
        {"question_number": "12", "bbox": [57, 702, 1000, 1000]}]}
    # The page's column cut at 500 is where the hints start: the padding left of it shows the other column.
    assert json.loads(re.search(r"\n分栏（本图坐标）：(.*)\n目录", prompt).group(1)) == [
        {"column_index": 1, "x1_1000": 0, "x2_1000": 57}, {"column_index": 2, "x1_1000": 57, "x2_1000": 1000}]
    # A column the tile does not show is not listed.
    left = tiles.tile_prompt(manifest[0:3], coarse_payload(), [0, 0, 480, 840], columns=COLUMNS,
                             width=1000, height=1500)
    assert json.loads(re.search(r"\n分栏（本图坐标）：(.*)\n目录", left).group(1)) == [
        {"column_index": 1, "x1_1000": 0, "x2_1000": 1000}]


def test_merge_rebuilds_directory_order_and_rejects_gaps_or_overlap():
    manifest = v4_contract((1, 1, 1))

    def part(numbers, issues=(), flags=None):
        return {"questions": [located_row(n, [1, 1, 2, 2], [[3, 3, 4, 4]]) for n in numbers],
                "needs_review": list(issues), "slot_source": contract.max_owned.SHARED_SLOT_SOURCE,
                "max_flags": flags or {}}

    flag = {"scope": "question", "status": "extra", "reason": "x", "source": "locator"}
    merged = tiles.merge_tiles([part(["3"], ["b"], {"3": [flag]}), part(["1", "2"], ["a"])], manifest)
    assert [q["question_number"] for q in merged["questions"]] == ["1", "2", "3"]
    assert merged["needs_review"] == ["b", "a"] and merged["max_flags"] == {"3": [flag]}
    assert set(merged) == {"coordinate_space", "questions", "needs_review", "slot_source", "max_flags"}
    with pytest.raises(ValueError, match="覆盖"):
        tiles.merge_tiles([part(["1", "2"])], manifest)
    with pytest.raises(ValueError, match="重复"):
        tiles.merge_tiles([part(["1", "2"]), part(["2", "3"])], manifest)
    with pytest.raises(ValueError, match="合同版本"):
        tiles.merge_tiles([{**part(["1", "2", "3"]), "slot_source": "max_locator_v3"}], manifest)


def rounded(value):
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, dict):
        return {key: rounded(item) for key, item in value.items()}
    return [rounded(item) for item in value] if isinstance(value, list) else value


def run_pipeline_with(tmp_path, monkeypatch, tile_questions):
    """Real _request, receivers and grid; only the Flash and Max transports are faked."""
    from services import analyzer
    monkeypatch.setattr(config, "OCR_TEMPLATE_LOCATOR_TILE_QUESTIONS", tile_questions)
    # Tiles by question count only: the drawn boxes are placed clear of these
    # tiles' grid lines, which the colour-matching fake cannot see through.
    monkeypatch.setattr(config, "OCR_TEMPLATE_LOCATOR_TILE_SPAN", 0)
    monkeypatch.setattr(config, "OCR_TEMPLATE_SOLVER_SHARDS", 1)
    source = tmp_path / f"paper-{tile_questions}.png"
    draw_page(source)
    raw_hierarchy, payloads = v4_payloads(COUNTS)
    locator_calls = []

    async def flash(messages, **kwargs):
        prompt = messages[0]["content"][0]["text"]
        if prompt == pipeline._HIERARCHY_PROMPT:
            payload = raw_hierarchy
        elif prompt == pipeline._COARSE_LOCATOR_PROMPT:
            payload = coarse_payload()
        else:
            payload = payloads[contract.TRANSCRIBE_PHASE]
        raw = json.dumps(payload, ensure_ascii=False)
        kwargs["response_validator"](raw)
        return {"content": raw, "model": "qwen3.8-flash", "receipt": {}}

    async def max_call(images, prompt, **kwargs):
        if "normalized_1000_v1" in prompt:
            sizes = []
            for path in images:
                with Image.open(path) as image:
                    sizes.append(image.size)
            locator_calls.append({"prompt": prompt, "sizes": sizes})
            payload = perfect_locator(images, prompt)
        else:
            payload = payloads[contract.SOLVE_PHASE]
        raw = json.dumps(payload, ensure_ascii=False)
        kwargs["response_validator"](raw)
        return raw

    async def forbidden(*args, **kwargs):
        raise AssertionError("no real network")

    monkeypatch.setattr(flash_grading, "call_flash", flash)
    monkeypatch.setattr(pipeline, "call_gpt_vision", max_call)
    monkeypatch.setattr(analyzer, "_call_api", forbidden)
    progress = {"stages": {}}

    async def run():
        with use_teaching_context(make_teaching_context("primary")):
            with pipeline.template_import_context(asyncio.get_running_loop().time() + 60, progress):
                return await pipeline.recognize_directory_template(
                    str(source), template_mode="marked", layout_result={"column_layout": {"columns": COLUMNS}})

    return asyncio.run(run()), locator_calls, progress


def test_tiled_locate_produces_the_single_request_template(tmp_path, monkeypatch):
    single, single_calls, single_progress = run_pipeline_with(tmp_path, monkeypatch, 0)
    assert "error" not in single
    assert [call["sizes"] for call in single_calls] == [[(500, 1500), (500, 1500)]]
    assert PHASE in single_progress["stages"]
    assert single["template_ocr_diagnostics"]["locator_tiles"] == 0
    assert single["template_ocr_diagnostics"]["locator_tile_report"] == []
    assert single["template_ocr_diagnostics"]["request_count"] == 5
    # The template really holds the drawn boxes, in page coordinates.
    truth = {number: [[round(v) for v in normalized(box)] for box in boxes] for number, boxes in truth_px().items()}
    assert single["question_anchors"] == [
        {"question_number": number, "question_number_bbox": boxes[0]} for number, boxes in truth.items()]
    assert [row["regions"] for row in single["template_answer_regions"]] == [boxes[1:] for boxes in truth.values()]

    result, calls, progress = run_pipeline_with(tmp_path, monkeypatch, 3)
    assert "error" not in result
    for field in ("questions", "question_directory", "question_anchors", "template_answer_regions",
                  "template_handwriting_regions", "requires_teacher_confirmation"):
        # Stitched floats are rounded to 1/1000 of a unit; nothing else may differ.
        assert rounded(result[field]) == rounded(single[field]), field
    diagnostics = result["template_ocr_diagnostics"]
    assert diagnostics["locator_tiles"] == 4 and diagnostics["request_count"] == 8
    assert diagnostics["locator_tile_report"] == [
        {"question_numbers": ["1", "2", "3"], "box_px": [0, 0, 530, 840], "attempts": 1, "edge_contact": {}},
        {"question_numbers": ["4", "5", "6"], "box_px": [0, 660, 530, 1500], "attempts": 1, "edge_contact": {}},
        {"question_numbers": ["7", "8", "9"], "box_px": [470, 0, 1000, 840], "attempts": 1, "edge_contact": {}},
        {"question_numbers": ["10", "11", "12"], "box_px": [470, 660, 1000, 1500], "attempts": 1, "edge_contact": {}}]
    # One image per request, described in its own coordinates: no pixel size of the crop
    # or the page, and the page's columns only as they fall inside the crop.
    assert sorted(call["sizes"] for call in calls) == [[(530, 840)]] * 4
    assert not any(re.search(r"\d+×\d+", call["prompt"]) for call in calls)
    assert all("分栏（本图坐标）" in call["prompt"] and '"x2_1000":500' not in call["prompt"] for call in calls)
    owned = sorted([row["q_id"] for row in json.loads(re.search(r"\n目录：(.*)\n返回", call["prompt"]).group(1))]
                   for call in calls)
    assert owned == sorted([f"Q{n}" for n in numbers] for numbers in (range(1, 4), range(4, 7), range(7, 10), range(10, 13)))
    stage_keys = {f"{PHASE}#{index}" for index in range(1, 5)}
    assert stage_keys <= set(progress["stages"]) and PHASE not in progress["stages"]
    assert all(progress["stages"][key]["status"] == "completed" for key in stage_keys)


def test_without_coarse_boxes_the_page_is_located_in_one_request(tmp_path, monkeypatch):
    from services import analyzer
    monkeypatch.setattr(config, "OCR_TEMPLATE_LOCATOR_TILE_QUESTIONS", 3)
    monkeypatch.setattr(config, "OCR_TEMPLATE_SOLVER_SHARDS", 1)
    source = tmp_path / "paper.png"
    draw_page(source)
    raw_hierarchy, payloads = v4_payloads(COUNTS)
    locator_images = []

    async def flash(messages, **kwargs):
        prompt = messages[0]["content"][0]["text"]
        if prompt == pipeline._COARSE_LOCATOR_PROMPT:
            raise ValueError("coarse hint failed")
        payload = raw_hierarchy if prompt == pipeline._HIERARCHY_PROMPT else payloads[contract.TRANSCRIBE_PHASE]
        raw = json.dumps(payload, ensure_ascii=False)
        kwargs["response_validator"](raw)
        return {"content": raw, "model": "qwen3.8-flash", "receipt": {}}

    async def max_call(images, prompt, **kwargs):
        if "normalized_1000_v1" in prompt:
            locator_images.append(len(images))
            payload = perfect_locator(images, prompt)
        else:
            payload = payloads[contract.SOLVE_PHASE]
        raw = json.dumps(payload, ensure_ascii=False)
        kwargs["response_validator"](raw)
        return raw

    async def forbidden(*args, **kwargs):
        raise AssertionError("no real network")

    monkeypatch.setattr(flash_grading, "call_flash", flash)
    monkeypatch.setattr(pipeline, "call_gpt_vision", max_call)
    monkeypatch.setattr(analyzer, "_call_api", forbidden)

    async def run():
        with use_teaching_context(make_teaching_context("primary")):
            return await pipeline.recognize_directory_template(
                str(source), template_mode="marked", layout_result={"column_layout": {"columns": COLUMNS}})

    result = asyncio.run(run())
    assert "error" not in result
    assert locator_images == [2]
    assert result["template_ocr_diagnostics"]["locator_tiles"] == 0


@pytest.mark.parametrize("name,value", [
    ("OCR_TEMPLATE_LOCATOR_TILE_QUESTIONS", "1"), ("OCR_TEMPLATE_LOCATOR_TILE_QUESTIONS", "21"),
    ("OCR_TEMPLATE_LOCATOR_TILE_QUESTIONS", "-1"),
    ("OCR_TEMPLATE_LOCATOR_TILE_SPAN", "149"), ("OCR_TEMPLATE_LOCATOR_TILE_SPAN", "1001")])
def test_config_rejects_tile_limits_outside_their_ranges(tmp_path, name, value):
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env[name] = value
    env["PYTHON_DOTENV_DISABLED"] = "1"
    env["JWT_SECRET"] = "test-only-jwt-secret-with-at-least-32-characters"
    env["PYTHONPATH"] = str(project_root)
    completed = subprocess.run([sys.executable, "-c", "import config"], cwd=tmp_path, env=env,
                               text=True, capture_output=True)
    assert completed.returncode != 0
    assert name in completed.stderr
