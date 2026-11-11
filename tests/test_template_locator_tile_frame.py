"""One tile, one coordinate frame: crop pixels, grid and page mapping agree.

The page is the geometry of the 2026-11-11 replay: 2064 x 1564, two columns
cut at 514, 25 questions that plan into the five tiles measured there. In one
of two runs of the same tile the answer boxes of questions 6-10 came back
100-140 px left of their place, so every step between a page pixel and a
stitched coordinate is checked here in pixels.
"""
import asyncio
import json
import re

import pytest
from PIL import Image, ImageChops, ImageDraw

from services import template_locator_tiles as tiles
from tests.test_template_locator_tiles import find_box, v4_contract

PAGE = (2064, 1564)
COLUMNS = [{"column_index": 1, "x1_1000": 0.0, "x2_1000": 514.0},
           {"column_index": 2, "x1_1000": 514.0, "x2_1000": 1000.0}]
LIMITS = {"max_questions": 8, "max_span": 350.0}
PHASE = "template-directory-v1"
MEASURED_TILES = [(["1", "5"], [0, 367, 1755, 1080]), (["6", "10"], [0, 907, 1342, 1564]),
                  (["11", "14"], [998, 86, 2064, 743]), (["15", "20"], [998, 555, 2064, 1166]),
                  (["21", "25"], [998, 977, 2064, 1400])]
# Answer boxes of questions 6-10 as the whole-page locate placed them: left edge and width, page pixels.
REFERENCE = {"6": (438, 107), "7": (506, 114), "8": (454, 111), "9": (491, 118), "10": (533, 74)}
# A grid line on a box edge hides one pixel of it from the colour reader; stitched
# coordinates are rounded to 0.001 of a unit, about 0.002 px on this page.
ONE_PIXEL = 1.01


def coarse_rows(first, last, top, bottom, x1, x2):
    step = (bottom - top) / (last - first + 1)
    return [{"question_number": str(number),
             "bbox": [x1, round(top + (number - first) * step, 3), x2, round(top + (number - first + 1) * step, 3)]}
            for number in range(first, last + 1)]


# Envelopes read back from the measured tile boxes; two left-column runs reach into the right column.
COARSE = {"coordinate_space": "normalized_1000_v1", "questions": (
    coarse_rows(1, 5, 295, 630, 40, 820) + coarse_rows(6, 10, 640, 965, 40, 620)
    + coarse_rows(11, 14, 115, 415, 530, 980) + coarse_rows(15, 18, 415, 625, 530, 980)
    + coarse_rows(19, 20, 625, 685, 530, 980) + coarse_rows(21, 25, 685, 835, 530, 980))}


def colour(number, role):
    """Red stays 0: no grid line or label blended over a box can produce it."""
    return (0, 10 * int(number), 60 + 60 * role)


def truth_px():
    """question number -> (printed number box, answer box) in page pixels."""
    result = {}
    for row in COARSE["questions"]:
        number = row["question_number"]
        column_left = 0 if int(number) <= 10 else 1061
        top = round(row["bbox"][1] * PAGE[1] / 1000) + 10
        x, width = REFERENCE.get(number, (column_left + 440, 110))
        result[number] = ([column_left + 157, top, column_left + 187, top + 24], [x, top, x + width, top + 24])
    return result


def drawn_page():
    page = Image.new("RGB", PAGE, "white")
    draw = ImageDraw.Draw(page)
    for number, boxes in truth_px().items():
        for role, box in enumerate(boxes):
            draw.rectangle((box[0], box[1], box[2] - 1, box[3] - 1), fill=colour(number, role))
    return page


def read_exactly(image_paths, prompt):
    """A locator that reports each owned box where it is in the image it was sent."""
    owned = json.loads(re.search(r"\n目录：(.*)\n返回", prompt).group(1))
    with Image.open(image_paths[0]) as opened:
        image = opened.convert("RGB")
    return {"coordinate_space": "normalized_1000_v1", "needs_review": [], "questions": [
        {"question_number": row["q_id"][1:], "question_number_bbox": find_box([image], colour(row["q_id"][1:], 0)),
         "question_type": "fill", "choice_mode": "unknown", "label_anchor": None,
         "regions": [{"slot_id": row["slots"][0]["slot_id"], "kind": "answer",
                      "bbox": find_box([image], colour(row["q_id"][1:], 1))}]}
        for row in owned]}


def locate_page(directory, reader=read_exactly):
    manifest = v4_contract((1,) * 25)
    plan = tiles.plan_tiles(manifest, COARSE, COLUMNS, width=PAGE[0], height=PAGE[1], **LIMITS)

    async def request(images, prompt, phase, validator, **kwargs):
        return validator(reader(images, prompt))

    return asyncio.run(tiles.locate_in_tiles(
        request, drawn_page(), directory, manifest, COARSE, plan, phase=PHASE, max_tokens=100, report=[]))


def worst_offset_px(result):
    """Largest distance in page pixels between a stitched box edge and the drawn one."""
    truth, worst = truth_px(), 0.0
    for question in result["questions"]:
        drawn = truth[question["question_number"]]
        for located, expected in zip((question["question_number_bbox"], question["regions"][0]), drawn):
            worst = max([worst] + [abs(value * PAGE[index % 2] / 1000 - expected[index])
                                   for index, value in enumerate(located)])
    return worst


def test_the_replayed_page_plans_into_the_five_measured_tiles():
    plan = tiles.plan_tiles(v4_contract((1,) * 25), COARSE, COLUMNS, width=PAGE[0], height=PAGE[1], **LIMITS)
    assert [([tile["numbers"][0], tile["numbers"][-1]], tile["box"]) for tile in plan] == MEASURED_TILES


def coded_page():
    """Every pixel names its own place: green = x mod 251, blue = y mod 241, no red."""
    width, height = PAGE
    green = Image.frombytes("L", PAGE, bytes(x % 251 for x in range(width)) * height)
    blue = Image.frombytes("L", PAGE, b"".join(bytes([y % 241]) * width for y in range(height)))
    return Image.merge("RGB", (Image.new("L", PAGE, 0), green, blue))


def mask(band):
    return band.point(lambda value: 255 if value else 0)


@pytest.mark.parametrize("box", [box for _, box in MEASURED_TILES])
def test_a_saved_tile_is_the_page_crop_pixel_for_pixel_under_its_own_grid(tmp_path, box):
    page = coded_page()
    with Image.open(tiles._save_tile(page, box, tmp_path, "probe")) as opened:
        saved = opened.convert("RGB")
    crop = page.crop(tuple(box))
    width, height = box[2] - box[0], box[3] - box[1]
    assert saved.size == crop.size == (width, height)
    gridded = mask(saved.getchannel("R"))  # the page has no red; only the grid adds it
    changed = ImageChops.difference(saved, crop).split()
    changed = mask(ImageChops.lighter(ImageChops.lighter(changed[0], changed[1]), changed[2]))
    # Wherever the grid drew nothing, tile pixel (i, j) is page pixel (left + i, top + j).
    # On this page a margin, a shift or a rescale of the crop would change every pixel.
    assert ImageChops.subtract(changed, gridded).getbbox() is None
    assert gridded.histogram()[255] < width * height / 10
    # Grid line t stands at t/1000 of the tile's own width and height, measured from its corner.
    row, column = round(height * 0.55), round(width * 0.55)
    for tick in range(100, 1000, 100):
        x, y = round(tick * width / 1000), round(tick * height / 1000)
        assert [gridded.getpixel((x + shift, row)) for shift in (-3, 0, 3)] == [0, 255, 0], tick
        assert [gridded.getpixel((column, y + shift)) for shift in (-3, 0, 3)] == [0, 255, 0], tick


def test_every_tile_of_the_replayed_page_puts_its_boxes_back_on_the_page(tmp_path):
    result = locate_page(tmp_path)
    assert [question["question_number"] for question in result["questions"]] == [str(n) for n in range(1, 26)]
    assert result["needs_review"] == []
    assert worst_offset_px(result) <= ONE_PIXEL
    # The five boxes that drifted: tile [0, 907, 1342, 1564], left edge and width in page pixels.
    answers = {question["question_number"]: question["regions"][0] for question in result["questions"]}
    for number, (left, width) in REFERENCE.items():
        assert answers[number][0] * PAGE[0] / 1000 == pytest.approx(left, abs=ONE_PIXEL)
        assert (answers[number][2] - answers[number][0]) * PAGE[0] / 1000 == pytest.approx(width, abs=2 * ONE_PIXEL)


def crop_taken_as_the_page_left_edge(monkeypatch):
    monkeypatch.setattr(tiles, "_x_to_page", lambda value, tile_box, width: round(
        value * (tile_box[2] - tile_box[0]) / width, 3))


def crop_taken_as_the_page_top(monkeypatch):
    monkeypatch.setattr(tiles, "_y_to_page", lambda value, tile_box, height: round(
        value * (tile_box[3] - tile_box[1]) / height, 3))


def tile_reading_stretched_over_the_page_width(monkeypatch):
    monkeypatch.setattr(tiles, "_x_to_page", lambda value, tile_box, width: round(
        min(1000.0, tile_box[0] * 1000 / width + value), 3))


def tile_reading_shrunk_by_its_pixel_width(monkeypatch):
    # What the drifted run looked like: x of the right tile, divided by its width once more.
    original = tiles._x_to_page
    monkeypatch.setattr(tiles, "_x_to_page", lambda value, tile_box, width: original(
        value * 1000 / max(1000, tile_box[2] - tile_box[0]), tile_box, width))


def crop_saved_with_a_margin(monkeypatch):
    original = tiles._save_tile
    monkeypatch.setattr(tiles, "_save_tile", lambda page, box, directory, name: original(
        page, [box[0] - 16, box[1] - 16, box[2] + 16, box[3] + 16], directory, name))


@pytest.mark.parametrize("fault", [
    crop_taken_as_the_page_left_edge, crop_taken_as_the_page_top, tile_reading_stretched_over_the_page_width,
    tile_reading_shrunk_by_its_pixel_width, crop_saved_with_a_margin], ids=lambda fault: fault.__name__)
def test_a_tile_mapped_from_any_other_origin_or_size_misses_the_drawn_boxes(tmp_path, monkeypatch, fault):
    """The pixel check above is sensitive to each way a tile's frame can be mistaken."""
    fault(monkeypatch)
    assert worst_offset_px(locate_page(tmp_path)) > 10.0
