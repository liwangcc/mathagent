"""The whole-page locator request: its prompt must describe the images it really carries.

The request holds one image per column, each cut from the page after one page
grid was stamped on it. The prompt states one coordinate system, no pixel
size, and how the grid is numbered; the tests below read the request the real
pipeline builds and check every such statement against the images' pixels.
"""
import asyncio
import json
import re

import numpy as np
import pytest
from PIL import Image

from services import template_directory_pipeline as pipeline
from services import template_max_owned as owned
from services import template_recognition
from services.teaching_context import make_teaching_context, use_teaching_context
from tests.test_template_locator_tiles import COUNTS
from tests.test_template_one_three import v4_payloads

PAGE = (2064, 1564)
LAYOUTS = {
    "one_column": [(0, 1000)],
    "two_columns": [(0, 514), (514, 1000)],
    "three_columns": [(0, 300), (300, 660), (660, 1000)],
}


def columns_of(layout):
    return [{"column_index": index, "x1_1000": float(left), "x2_1000": float(right)}
            for index, (left, right) in enumerate(LAYOUTS[layout], 1)]


def locator_request(tmp_path, monkeypatch, layout):
    """Run the real pipeline on a blank page up to its whole-page Max locator request."""
    source = tmp_path / "paper.png"
    Image.new("RGB", PAGE, "white").save(source)
    raw_hierarchy, _ = v4_payloads(COUNTS)
    seen = {}

    async def request(images, prompt, phase, validator, **kwargs):
        if phase == "template-hierarchy-v1":
            return validator(raw_hierarchy)
        if phase == "template-directory-v1":
            seen["prompt"] = prompt
            seen["images"] = []
            for path in images:
                with Image.open(path) as image:
                    seen["images"].append(np.asarray(image.convert("RGB")))
        raise RuntimeError("stop after the locator request")

    monkeypatch.setattr(pipeline, "_request", request)
    with use_teaching_context(make_teaching_context("primary")):
        asyncio.run(pipeline.recognize_directory_template(
            str(source), template_mode="marked", layout_result={"column_layout": {"columns": columns_of(layout)}}))
    return seen["prompt"], seen["images"]


def label_ink(pixels) -> int:
    """Pixels of label text on a white page: dark red, far darker than the translucent grid lines."""
    return int(((pixels[..., 0] < 200) & (pixels[..., 1] < 90) & (pixels[..., 2] < 90)).sum())


def line_rows(pixels) -> list[int]:
    """Rows that a grid line crosses from edge to edge."""
    inked = (pixels != 255).any(axis=2).mean(axis=1)
    return [int(row) for row in np.flatnonzero(inked > 0.9)]


@pytest.mark.parametrize("layout", LAYOUTS)
def test_the_page_prompt_states_one_coordinate_system_and_no_pixel_size(tmp_path, monkeypatch, layout):
    prompt, _ = locator_request(tmp_path, monkeypatch, layout)

    assert prompt.startswith("按目录在原图中定位学生需要完成的作答区域。坐标normalized_1000_v1，一律以整页为准："
                             "整页左上角为(0,0)，右下角为(1000,1000)。")
    assert not re.search(r"\d+\s*[×xX*]\s*\d+", prompt)
    assert str(PAGE[0]) not in prompt and str(PAGE[1]) not in prompt and "像素" not in prompt
    assert "网格刻度就是要返回的坐标，按刻度直接读数，不再做任何换算。" in prompt
    # The columns stay in page coordinates, as the layout review fixed them.
    assert json.loads(re.search(r"\n分栏：(.*)\n目录：", prompt).group(1)) == columns_of(layout)


@pytest.mark.parametrize("layout", LAYOUTS)
def test_what_the_prompt_says_about_the_images_is_true_of_their_pixels(tmp_path, monkeypatch, layout):
    prompt, images = locator_request(tmp_path, monkeypatch, layout)
    width, height = PAGE

    # "输入的每张图是同一整页按分栏裁出的一栏，按分栏顺序排列"
    assert "输入的每张图是同一整页按分栏裁出的一栏，按分栏顺序排列" in prompt
    assert [image.shape[:2] for image in images] == [
        (height, round(right * width / 1000) - round(left * width / 1000)) for left, right in LAYOUTS[layout]]

    # "网格每100一条线……横线自上而下依次为y=0（上边缘）、100、200……，下边缘为y=1000"
    step = owned.GRID_STEP
    assert step == template_recognition._COORDINATE_GRID_STEP_1000
    assert f"网格每{step}一条线" in prompt and f"横线自上而下依次为y=0（上边缘）、{step}、{2 * step}……，下边缘为y=1000" in prompt
    for image in images:
        assert line_rows(image) == [round(tick * height / 1000) for tick in range(0, 1000, step)]

    # "竖线顶端标有x刻度" and "y刻度只印在第一张图的左侧，其余各图的横线与之逐条对应"
    assert "竖线顶端标有x刻度" in prompt and "y刻度只印在第一张图的左侧，其余各图的横线与之逐条对应" in prompt
    assert all(label_ink(image[:24]) > 0 for image in images)
    rows_below_the_x_labels = [label_ink(image[30:, :70]) for image in images]
    assert rows_below_the_x_labels[0] > 0 and not any(rows_below_the_x_labels[1:])
