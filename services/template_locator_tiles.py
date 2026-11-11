"""Locate answer regions tile by tile and stitch them back into page space.

One Max locator request for a whole page is slow, and a crop of a page-stamped
grid loses the axis labels of the other edge. Here every request sees exactly
one crop that carries its own complete 0..1000 grid, so the model reports the
coordinates of the image it is looking at. The backend then maps each box back
to the page with fixed arithmetic and joins the tiles by question number: the
result has the shape and coordinate space of the single-request locator, so
template saving and later grading read it unchanged.

Tiles are planned from the Flash coarse boxes before any Max call. Every
question has exactly one owner; the merge never chooses between two boxes.
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
from pathlib import Path

from services import template_max_owned as max_owned

# Page-normalized padding around the coarse boxes a tile owns. Coarse boxes are
# hints and can be tight; handwriting also runs past the printed column.
_PAD_X = 30.0
_PAD_Y = 60.0
# A tile's own grid has ten lines per side; below this share of the page the
# lines and labels would crowd the print they are meant to measure.
_MIN_SIDE = 250.0
# A box this close (tile units) to an edge cut inside the page may be clipped.
_EDGE = 2.0
# Extra page-normalized room given to a clipped side before the single retry.
_WIDEN = 80.0
_SIDES = ("left", "top", "right", "bottom")
_TILE_NOTE = ("\n本图是试卷的局部裁图，整页即指本图，坐标与网格均以本图为准。"
              "只定位目录所列题目；图中露出的其他题目不属于本请求，不返回。")
_COARSE_NOTE = ("\n以下是按量 Flash 的非权威粗定位提示（已换算为本图坐标），仅用于帮助你快速找到题目；"
                "不得照抄其边界，必须重新核对原图、题号、作答框和作图区，并按本合同返回最终权威坐标：")


def configured_limits(hierarchy: dict) -> dict:
    """The two tile limits as ``plan_tiles`` keywords; 0 questions keeps one page request."""
    import config
    shared = hierarchy.get("question_identity_version") == 4
    return {"max_questions": config.OCR_TEMPLATE_LOCATOR_TILE_QUESTIONS if shared else 0,
            "max_span": float(config.OCR_TEMPLATE_LOCATOR_TILE_SPAN)}


def _runs(numbers: list[str], boxes: dict, limit: int, max_span: float) -> list[list[str]]:
    """Fewest consecutive runs within both limits, then the most even ones.

    A run's load is the larger of its share of ``limit`` questions and its
    share of ``max_span`` page height (0 = no height limit). The slowest tile
    sets the wait of the whole locate, so among the splits with the fewest
    runs the one with the smallest largest load wins. A single question taller
    than ``max_span`` cannot be split and keeps a run of its own.
    """
    def load(start: int, stop: int) -> float:
        owned = [boxes[number] for number in numbers[start:stop]]
        span = max(box[3] for box in owned) - min(box[1] for box in owned)
        weight = max((stop - start) / limit, span / max_span if max_span else 0.0)
        return min(weight, 1.0) if stop - start == 1 else weight

    total = len(numbers)
    # best[j]: (largest load, spread, negated cuts) over splits of the first j
    # questions. Negated cuts break exact ties towards the longer first run.
    best: list = [(0.0, 0.0, ())] + [None] * total
    for count in range(1, total + 1):
        step: list = [None] * (total + 1)
        for stop in range(count, total + 1):
            step[stop] = min(
                (max(best[start][0], weight), round(best[start][1] + weight * weight, 9),
                 best[start][2] + (-stop,))
                for start in range(count - 1, stop) if best[start] is not None
                for weight in [load(start, stop)])
        best = step
        if best[total][0] <= 1.0 + 1e-9 or count == total:
            break
    cuts = [0] + [-cut for cut in best[total][2]]
    return [numbers[start:stop] for start, stop in zip(cuts, cuts[1:])]


def _tile_box(boxes: list[list[float]], column: dict, *, width: int, height: int) -> list[int]:
    """Pixel crop holding the whole column width around the owned coarse boxes."""
    x1 = min([float(column["x1_1000"])] + [box[0] for box in boxes]) - _PAD_X
    x2 = max([float(column["x2_1000"])] + [box[2] for box in boxes]) + _PAD_X
    y1 = min(box[1] for box in boxes) - _PAD_Y
    y2 = max(box[3] for box in boxes) + _PAD_Y
    grow = max(0.0, _MIN_SIDE - (y2 - y1)) / 2
    y1, y2 = y1 - grow, y2 + grow
    # Keep the minimum height when one side is already at the page edge.
    if y1 < 0:
        y1, y2 = 0.0, min(1000.0, max(y2, _MIN_SIDE))
    if y2 > 1000:
        y1, y2 = max(0.0, min(y1, 1000.0 - _MIN_SIDE)), 1000.0
    return [max(0, math.floor(x1 * width / 1000)), max(0, math.floor(y1 * height / 1000)),
            min(width, math.ceil(x2 * width / 1000)), min(height, math.ceil(y2 * height / 1000))]


def plan_tiles(manifest, coarse, columns, *, width: int, height: int, max_questions: int,
               max_span: float = 0.0) -> list[dict]:
    """Assign every question to one tile: by column, then in reading-order runs.

    A run holds at most ``max_questions`` questions and, when ``max_span`` is
    set, at most that much page-normalized height of coarse boxes.

    Returns ``[]`` (keep the single page request) when tiling is off, the
    coarse boxes are missing or do not name exactly the directory questions,
    or the page would become a single tile anyway.
    """
    numbers = [row["q_id"][1:] for row in manifest]
    if max_questions < 1 or not coarse or len(numbers) < 2:
        return []
    boxes = {row["question_number"]: row["bbox"] for row in coarse["questions"]}
    if len(boxes) != len(coarse["questions"]) or set(boxes) != set(numbers):
        return []
    by_column: dict[int, list[str]] = {}
    for number in numbers:
        centre = (boxes[number][0] + boxes[number][2]) / 2
        index = next((i for i, column in enumerate(columns)
                      if float(column["x1_1000"]) <= centre < float(column["x2_1000"])), len(columns) - 1)
        by_column.setdefault(index, []).append(number)
    tiles = [{"numbers": owned,
              "box": _tile_box([boxes[n] for n in owned], columns[index], width=width, height=height)}
             for index in sorted(by_column)
             for owned in _runs(by_column[index], boxes, max_questions, max_span)]
    return tiles if len(tiles) > 1 else []


def _to_tile(box, tile_box, *, width: int, height: int) -> list[int]:
    """Page-normalized box -> integer coordinates of the tile, clipped to it."""
    left, top, right, bottom = tile_box
    xs = [(box[i] * width / 1000 - left) * 1000 / (right - left) for i in (0, 2)]
    ys = [(box[i] * height / 1000 - top) * 1000 / (bottom - top) for i in (1, 3)]
    clip = lambda value: int(round(min(1000.0, max(0.0, value))))  # noqa: E731
    return [clip(xs[0]), clip(ys[0]), clip(xs[1]), clip(ys[1])]


def _x_to_page(value: float, tile_box, width: int) -> float:
    left, _, right, _ = tile_box
    return round(min(1000.0, max(0.0, (left + value * (right - left) / 1000) * 1000 / width)), 3)


def _y_to_page(value: float, tile_box, height: int) -> float:
    _, top, _, bottom = tile_box
    return round(min(1000.0, max(0.0, (top + value * (bottom - top) / 1000) * 1000 / height)), 3)


def _box_to_page(box, tile_box, *, width: int, height: int):
    """Tile box -> page-normalized box; unlocated placeholders stay as they are."""
    if not box:
        return box
    return [_x_to_page(box[0], tile_box, width), _y_to_page(box[1], tile_box, height),
            _x_to_page(box[2], tile_box, width), _y_to_page(box[3], tile_box, height)]


def to_page(located: dict, tile_box, *, width: int, height: int) -> dict:
    """Re-express one received tile result in page-normalized coordinates."""
    result = copy.deepcopy(located)
    convert = lambda box: _box_to_page(box, tile_box, width=width, height=height)  # noqa: E731
    for question in result["questions"]:
        question["question_number_bbox"] = convert(question["question_number_bbox"])
        question["regions"] = [convert(box) for box in question["regions"]]
        question["drawing_regions"] = [convert(box) for box in question["drawing_regions"]]
        anchor = question["label_anchor"]
        if anchor is not None:
            question["label_anchor"] = [_x_to_page(anchor[0], tile_box, width),
                                        _y_to_page(anchor[1], tile_box, height)]
    return result


def edge_contact(located: dict, tile_box, *, width: int, height: int) -> dict[str, list[str]]:
    """Questions whose boxes reach a tile edge that was cut inside the page.

    Such a box may continue beyond the crop. Edges that are the page's own
    edges are exempt: nothing lies beyond them.
    """
    left, top, right, bottom = tile_box
    cut = {"left": left > 0, "top": top > 0, "right": right < width, "bottom": bottom < height}
    result: dict[str, list[str]] = {}
    for question in located["questions"]:
        boxes = [question["question_number_bbox"], *question["regions"], *question["drawing_regions"]]
        for box in (box for box in boxes if box):
            reached = {"left": box[0] <= _EDGE, "top": box[1] <= _EDGE,
                       "right": box[2] >= 1000 - _EDGE, "bottom": box[3] >= 1000 - _EDGE}
            for side in _SIDES:
                if cut[side] and reached[side] and question["question_number"] not in result.setdefault(side, []):
                    result[side].append(question["question_number"])
    return {side: numbers for side, numbers in result.items() if numbers}


def widen(tile_box, sides, *, width: int, height: int) -> list[int]:
    """Give the clipped sides more of the page."""
    left, top, right, bottom = tile_box
    dx, dy = math.ceil(_WIDEN * width / 1000), math.ceil(_WIDEN * height / 1000)
    return [max(0, left - dx) if "left" in sides else left, max(0, top - dy) if "top" in sides else top,
            min(width, right + dx) if "right" in sides else right,
            min(height, bottom + dy) if "bottom" in sides else bottom]


def tile_prompt(owned, coarse, tile_box, *, width: int, height: int) -> str:
    """The single-column page prompt, addressed to this tile and its questions."""
    left, top, right, bottom = tile_box
    prompt = max_owned.locator_prompt(
        owned, width=right - left, height=bottom - top,
        columns=[{"column_index": 1, "x1_1000": 0.0, "x2_1000": 1000.0}]) + _TILE_NOTE
    numbers = {row["q_id"][1:] for row in owned}
    hints = [{"question_number": row["question_number"],
              "bbox": _to_tile(row["bbox"], tile_box, width=width, height=height)}
             for row in coarse["questions"] if row["question_number"] in numbers]
    return prompt + _COARSE_NOTE + json.dumps(
        {"coordinate_space": "normalized_1000_v1", "questions": hints}, ensure_ascii=False, separators=(",", ":"))


def merge_tiles(parts, manifest) -> dict:
    """Join page-space tile results by question number into the locator shape."""
    by_number, flags, issues = {}, {}, []
    for part in parts:
        if part.get("slot_source") != max_owned.SHARED_SLOT_SOURCE:
            raise ValueError("定位分块的作答位合同版本不一致")
        for question in part["questions"]:
            if question["question_number"] in by_number:
                raise ValueError("定位分块返回了重复题目")
            by_number[question["question_number"]] = question
        for number, rows in (part.get("max_flags") or {}).items():
            flags.setdefault(number, []).extend(copy.deepcopy(rows))
        issues.extend(part["needs_review"])
    order = [row["q_id"][1:] for row in manifest]
    if set(by_number) != set(order):
        raise ValueError("定位分块没有覆盖全部题目")
    return {"coordinate_space": "normalized_1000_v1", "questions": [by_number[number] for number in order],
            "needs_review": issues, "slot_source": max_owned.SHARED_SLOT_SOURCE, "max_flags": flags}


def _save_tile(page, tile_box, directory, name: str) -> str:
    """Crop first, then stamp: the grid and its labels belong to this image."""
    from services.template_recognition import _render_coordinate_grid
    path = Path(directory) / f"locator-tile-{name}.png"
    crop = page.crop(tuple(tile_box))
    try:
        _render_coordinate_grid(crop).save(path, format="PNG")
    finally:
        crop.close()
    return str(path)


def open_report(plan, report: list) -> None:
    """One report entry per planned tile, filled in as its requests return."""
    report.extend({"question_numbers": list(tile["numbers"]), "box_px": list(tile["box"]),
                   "attempts": 0, "edge_contact": {}} for tile in plan)


async def locate_tile(request, page, directory, manifest, coarse, plan, index: int, *,
                      phase: str, max_tokens: int, report: list) -> dict:
    """Locate tile ``index`` of ``plan`` in page space and update ``report[index]``.

    A tile whose answer reaches a cut edge is asked once more with that side
    widened; if it still does, the questions go to teacher review instead of
    being saved as possibly clipped boxes.
    """
    width, height = page.size
    tile = plan[index]
    rows = {row["q_id"][1:]: row for row in manifest}
    owned = [rows[number] for number in tile["numbers"]]
    box = list(tile["box"])
    for attempt in (1, 2):
        path = _save_tile(page, box, directory, f"{index + 1}-{attempt}")
        located = await request(
            [path], tile_prompt(owned, coarse, box, width=width, height=height), phase,
            lambda raw: max_owned.receive_locator_candidates(raw, owned),
            max_tokens=max_tokens, reasoning_effort="high", expected_ids=tuple(tile["numbers"]),
            stage_key=f"{phase}#{index + 1}" + ("" if attempt == 1 else "-wide"))
        contact = edge_contact(located, box, width=width, height=height)
        report[index].update(box_px=list(box), attempts=attempt, edge_contact=contact)
        if not contact or attempt == 2:
            break
        box = widen(box, contact, width=width, height=height)
    result = to_page(located, box, width=width, height=height)
    for number in sorted({n for numbers in contact.values() for n in numbers}, key=tile["numbers"].index):
        result["needs_review"].append(f"第{number}题的框贴近定位裁图边缘，可能未框全，请核对")
    return result


async def gather_tiles(tasks: list) -> list:
    """Await per-tile tasks; one failure cancels the rest and is re-raised."""
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def locate_in_tiles(request, page, directory, manifest, coarse, plan, *,
                          phase: str, max_tokens: int, report: list) -> dict:
    """Run one ``request`` per tile concurrently and stitch the page result.

    One failed tile cancels the others and fails the locate, exactly like a
    failed single request. ``report`` receives one entry per tile.
    """
    open_report(plan, report)
    parts = await gather_tiles([
        asyncio.create_task(locate_tile(request, page, directory, manifest, coarse, plan, index,
                                        phase=phase, max_tokens=max_tokens, report=report),
                            name=f"template-qwen-max-locator-{index + 1}")
        for index in range(len(plan))])
    return merge_tiles(parts, manifest)
