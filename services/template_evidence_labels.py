"""Question labels on evidence columns, and where a slot without a printed position puts its answer.

- ``_render_column_question_labels`` is the one renderer for the Q ownership
  labels shared by pre-freeze checks, template previews and student evidence
  (``EVIDENCE_Q_LABEL_*``, ``_evidence_question_label_font``,
  ``_drawing_region_pixels``).
- ``validate_drawing_label_geometry`` is the pre-freeze geometry gate: no
  student images, file writes or models.
- ``_route_unmarked_answer_positions`` picks the ink-bearing side for a slot whose
  page prints no answer position: the frozen model box, or the blank margins
  before the printed number and after the stem (``_unmarked_zones``), judged by
  new ink on template-white pixels (``_new_ink_extent``).

Moved unchanged from services/template_evidence.py, which still exposes these names.
"""

from __future__ import annotations

import math

from services.processing_errors import TemplateSchemaError
from services.template_anchor_crops import validate_anchor_crop_plan
from services.template_evidence_base import (
    Image,
    ImageFont,
    MAX_SOURCE_PIXELS,
    TemplateEvidenceError,
    cv2,
    np,
)
from services.template_evidence_crops import _frozen_column_bounds
from services.template_evidence_schema import (
    _has_text_slot_labels,
    _validate_printed_solve_anchors,
    normalize_question_anchors,
    normalize_question_schema,
    normalize_template_handwriting_regions,
)


# qwen3.8-flash receives the difference image, not the teacher overlay. Keep
# the ownership marker small and blue. Horizontal slot labels alone may blend
# at 65% opacity when they overlap a preceding answer box; never draw frames.
EVIDENCE_Q_LABEL_COLOR = (20, 84, 178)
EVIDENCE_Q_LABEL_FONT_SIZE = 30
EVIDENCE_Q_LABEL_PADDING_X = 8
EVIDENCE_Q_LABEL_PADDING_Y = 8
EVIDENCE_Q_LABEL_GAP = 12


def _evidence_question_label_font():
    """Return the largest portable font used for Qwen ownership labels."""
    try:
        return ImageFont.load_default(size=EVIDENCE_Q_LABEL_FONT_SIZE)
    except TypeError:
        # Older Pillow releases do not accept ``size`` for the bundled font.
        return ImageFont.load_default()


def _drawing_region_pixels(box, width: int, height: int, column_left: int = 0) -> list[int]:
    """Whole-page normalized geometry to a half-open local pixel rectangle."""
    return [math.floor(box[0] * width / 1000) - column_left,
            math.floor(box[1] * height / 1000),
            math.ceil(box[2] * width / 1000) - column_left,
            math.ceil(box[3] * height / 1000)]


def _render_column_question_labels(
    crop, *, question_numbers, region_question_numbers, answer_regions, handwriting,
    anchor_by_number, solve_numbers, drawing_questions, column_left, page_width,
    reuse_question_anchor_labels=False,
):
    """One renderer shared by pre-freeze, template preview and student evidence."""
    height, column_width = crop.shape[:2]
    width, left, right = page_width, column_left, column_left + column_width
    # Unified answer boxes own ordinary labels in both historical modes.
    # Printed-number positions remain a compatibility fallback without boxes.
    boxed = {r["question_number"] for r in answer_regions if r["regions"]} - set(drawing_questions)
    solve_numbers = set(solve_numbers) - boxed
    text_slot_numbers = {r["question_number"] for r in answer_regions if _has_text_slot_labels(r, drawing_questions)}
    region_question_numbers = [n for n in question_numbers if n not in solve_numbers and (n not in drawing_questions or n in text_slot_numbers)]
    region_answer_regions = [r for r in answer_regions if r["question_number"] in region_question_numbers]
    from services.evidence_label_layout import render_labels

    def local_box(box):
        return [max(0, round(box[0] * width / 1000) - left),
                max(0, round(box[1] * height / 1000)),
                min(right - left, round(box[2] * width / 1000) - left),
                min(height, round(box[3] * height / 1000))]

    protected = [local_box(box) for item in answer_regions + handwriting
                 for box in item["regions"]]
    protected.extend(local_box(box) for q in drawing_questions.values()
                     for box in q["drawing_regions"])
    targets = []
    for number in region_question_numbers:
        item = next((r for r in region_answer_regions if r["question_number"] == number and r["regions"]), None)
        source = "template_answer_region"
        if item is None and not reuse_question_anchor_labels:
            item = next((r for r in handwriting if r["question_number"] == number and r["regions"]), None)
            source = "template_handwriting_region"
        if item:
            for index, box in enumerate(item["regions"]):
                if number in drawing_questions and item.get("region_kinds", [])[index] != "answer":
                    continue
                x1, y1, x2, y2 = local_box(box)
                if x2 <= x1:
                    continue
                if y2 <= y1:
                    raise TemplateEvidenceError(f"第 {number} 题作答框过小，请教师复核")
                # Large solve areas start near the top. Short adjacent
                # blanks stay at their own vertical midpoint.
                cy = (y1 + y2) / 2
                tall = y2 - y1 > 3 * EVIDENCE_Q_LABEL_FONT_SIZE
                label_top = y1
                if tall:
                    # An answer never sits above its own printed number. A box
                    # drawn up into the previous question keeps its label there.
                    printed = anchor_by_number.get(number, {}).get("question_number_bbox")
                    if printed and left <= (printed[0] + printed[2]) * width / 2000 < right:
                        number_top = round(printed[1] * height / 1000)
                        if y1 < number_top < y2:
                            label_top = number_top
                    cy = label_top + EVIDENCE_Q_LABEL_FONT_SIZE / 2
                targets.append({"question_number": number, "source": source,
                                "region_index": index, "region_bbox_1000": list(box),
                                **({"slot_id": item["slot_ids"][index]} if "slot_ids" in item else {}),
                                **({"label_align": "top", "label_top_px": label_top} if tall else {}),
                                "region_px": [x1, y1, x2, y2], "anchor_px": [x1, cy]})
        else:
            box = anchor_by_number.get(number, {}).get("question_number_bbox")
            if box and left <= (box[0] + box[2]) * width / 2000 < right:
                targets.append({"question_number": number, "source": "legacy_question_anchor",
                                "anchor_px": [round(box[0] * width / 1000) - left,
                                              (box[1] + box[3]) * height / 2000]})
    for number in question_numbers:
        if number in solve_numbers and left <= (anchor_by_number[number]["question_number_bbox"][0] + anchor_by_number[number]["question_number_bbox"][2]) * width / 2000 < right:
            box = anchor_by_number[number]["question_number_bbox"]
            targets.append({"question_number": number, "source": "printed_question_number",
                            "question_number_bbox": list(box),
                            "anchor_px": [round(box[2] * width / 1000) - left,
                                          (box[1] + box[3]) * height / 2000]})
        if number in drawing_questions and left <= drawing_questions[number]["label_anchor"][0] * width / 1000 < right:
            anchor = drawing_questions[number]["label_anchor"]
            main = _drawing_region_pixels(drawing_questions[number]["drawing_regions"][0], width, height, left)
            # The schema has already checked this frozen routing point is at
            # or left of the drawing. Visible text may still move to safe
            # whitespace/a rail; rendering never rewrites the frozen anchor.
            targets.append({"question_number": number, "source": "drawing_region_left",
                            "label_anchor": list(anchor),
                            "label_vertical_overlap_with_main_drawing": True,
                            "main_drawing_pixel_box_exclusive": main,
                            "region_bbox_1000": list(drawing_questions[number]["drawing_regions"][0]),
                            "anchor_px": [round(anchor[0] * width / 1000) - left,
                                          round(anchor[1] * height / 1000)]})
    try:
        return render_labels(
            crop, targets, protected, font=_evidence_question_label_font(),
            color=EVIDENCE_Q_LABEL_COLOR,
            padding_x=EVIDENCE_Q_LABEL_PADDING_X, padding_y=EVIDENCE_Q_LABEL_PADDING_Y,
            gap=EVIDENCE_Q_LABEL_GAP)
    except TemplateSchemaError:
        # Preserve the controlled rule and question through prefreeze/import
        # diagnostics instead of aggregating it into an opaque layout error.
        raise
    except ValueError as exc:
        raise TemplateEvidenceError(str(exc)) from exc


def validate_drawing_label_geometry(
    question_schema, question_anchors, template_answer_regions, template_handwriting_regions,
    *, page_width: int, page_height: int, question_crop_plan=None, template_mode: str = "marked",
) -> dict:
    """Pre-freeze geometry gate without student images, file writes or models.

    Blank-template foreground safety must still be rechecked against each
    student's actual difference. Legacy non-graphic schemas bypass this gate.
    Returned local label rectangles use the same renderer as actual evidence.
    """
    schema = normalize_question_schema(question_schema)
    drawing = {item["question_number"]: item for item in schema
               if item.get("answer_kind") in {"drawing", "mixed"}}
    receipt = {"version": "drawing-label-geometry-v1", "placements": [],
               "student_foreground_checked": False}
    if not drawing:
        return receipt
    if (isinstance(page_width, bool) or isinstance(page_height, bool)
            or not isinstance(page_width, int) or not isinstance(page_height, int)
            or min(page_width, page_height) <= 0 or page_width * page_height > MAX_SOURCE_PIXELS):
        raise TemplateEvidenceError("作图定位页面尺寸无效，请教师复核")
    anchors = normalize_question_anchors(question_anchors or [])
    answer_regions = normalize_template_handwriting_regions(template_answer_regions or [])
    handwriting = normalize_template_handwriting_regions(template_handwriting_regions or [])
    anchor_by_number = {item["question_number"]: item for item in anchors}
    _validate_printed_solve_anchors(schema, anchors, answer_regions)
    plan = validate_anchor_crop_plan(question_crop_plan) if question_crop_plan is not None else None
    columns = _frozen_column_bounds(plan, page_width, page_height) if plan is not None else [(0, page_width)]
    # A graphical fill/choice item has its one Q at the drawing anchor, just
    # like a graphical solve item. Do not add another ordinary answer-region Q.
    region_numbers = [item["question_number"] for item in schema
                      if item["question_type"] != "solve" and item["question_number"] not in drawing]
    region_answers = [item for item in answer_regions if item["question_number"] in region_numbers]
    if template_mode == "blank" and ({item["question_number"] for item in region_answers} != set(region_numbers)
                                      or any(not item["regions"] for item in region_answers)):
        raise TemplateEvidenceError("模板作答框未覆盖全部题号，请教师复核")
    blocking_issues: list[str] = []
    for column_index, (left, right) in enumerate(columns, 1):
        canvas = Image.new("RGB", (right - left, page_height), "white")
        placements = []
        try:
            _, placements, label_layout = _render_column_question_labels(
                np.asarray(canvas), question_numbers=[q["question_number"] for q in schema],
                region_question_numbers=region_numbers, answer_regions=answer_regions,
                handwriting=handwriting, anchor_by_number=anchor_by_number,
                solve_numbers={q["question_number"] for q in schema if q["question_type"] == "solve" and q["question_number"] not in drawing},
                drawing_questions=drawing, column_left=left, page_width=page_width,
                reuse_question_anchor_labels=template_mode == "blank")
        except TemplateEvidenceError as exc:
            issues = (exc.diagnostics or {}).get("issues")
            if not isinstance(issues, list) or not issues:
                issues = [line.strip(" -") for line in str(exc).splitlines() if line.strip()]
            blocking_issues.extend(f"第 {column_index} 栏：{issue}" for issue in issues)
            continue
        receipt["placements"].extend({**item, "column_index": column_index,
                                       "label_layout": label_layout} for item in placements)
    if blocking_issues:
        unique = list(dict.fromkeys(blocking_issues))
        raise TemplateEvidenceError(
            "模板题号标记存在多个阻断项，请一次处理：\n"
            + "\n".join(f"- {issue}" for issue in unique)
            + "\n请教师复核作答框和题号标记位置。",
            {"issues": unique},
        )
    placed = [item["question_number"] for item in receipt["placements"] if item["source"] == "drawing_region_left"]
    if set(placed) != set(drawing) or len(placed) != len(drawing):
        raise TemplateEvidenceError("作图题标签不完整或重复，请教师复核定位")
    return receipt


def _new_ink_extent(student_rgb, template_rgb, window, band, exclude=()):
    """New dark ink on template-white pixels, kept by component centroid.

    Returns (pixels, [left, top, right, bottom] or None). A component whose
    centroid lies in an ``exclude`` rectangle belongs to another slot's zone.
    """
    wx1, wy1, wx2, wy2 = window
    if wx2 <= wx1 or wy2 <= wy1:
        return 0, None
    student = cv2.cvtColor(np.ascontiguousarray(student_rgb[wy1:wy2, wx1:wx2]), cv2.COLOR_RGB2GRAY)
    template = cv2.cvtColor(np.ascontiguousarray(template_rgb[wy1:wy2, wx1:wx2]), cv2.COLOR_RGB2GRAY)
    white = template >= 225
    if int(np.count_nonzero(white)) < 25:
        return 0, None
    shift = max(-35.0, min(35.0, float(np.median(template[white]) - np.median(student[white]))))
    adjusted = np.clip(student.astype(np.float32) + shift, 0, 255)
    mask = white & (adjusted <= 190) & (template.astype(np.float32) - adjusted >= 30)
    count, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    pixels, extent = 0, None
    for index in range(1, count):
        area = int(stats[index, cv2.CC_STAT_AREA])
        cx, cy = centroids[index][0] + wx1, centroids[index][1] + wy1
        if area < 3 or not (band[0] <= cx < band[2] and band[1] <= cy < band[3]) or any(
                r[0] <= cx < r[2] and r[1] <= cy < r[3] for r in exclude):
            continue
        x, y = int(stats[index, cv2.CC_STAT_LEFT]) + wx1, int(stats[index, cv2.CC_STAT_TOP]) + wy1
        box = [x, y, x + int(stats[index, cv2.CC_STAT_WIDTH]), y + int(stats[index, cv2.CC_STAT_HEIGHT])]
        pixels += area
        extent = box if extent is None else [min(extent[0], box[0]), min(extent[1], box[1]),
                                             max(extent[2], box[2]), max(extent[3], box[3])]
    return pixels, extent


def _unmarked_zones(number_px, box_px, column, template_gray, obstacles, h):
    """Blank margin before the printed number and after the stem's last line.

    Both stop at printed text, a fixed answer area or the column edge; a gap
    shared with a neighbouring question on the same row is split at its middle.
    """
    nx1, ny1, nx2, ny2 = number_px
    left_edge, right_edge = column
    min_width = max(8, h // 2)

    def overlaps_box(zone):
        # The frozen box already owns that side, e.g. a marked template's
        # handwriting written before the number; never count its ink twice.
        return (zone[0] < box_px[2] and zone[2] > box_px[0]
                and zone[1] < box_px[3] and zone[3] > box_px[1])

    def printed(y1, y2, x1, x2):
        return (template_gray[y1:y2, x1:x2] < 200).any(axis=0) if x2 > x1 and y2 > y1 else np.zeros(0, bool)

    def blocking(y1, y2):
        return [box for box in obstacles if box[1] < y2 and box[3] > y1]

    # Before the number: step over a tight model box's cut glyph first.
    ink = printed(ny1, ny2, left_edge, nx1)
    join = max(2, round(0.35 * h))
    right, x = nx1, nx1 - 1
    while x >= left_edge and right - x <= join:
        if ink[x - left_edge]:
            right = x
        x -= 1
    left = left_edge
    for x in range(right - 1, left_edge - 1, -1):
        if ink[x - left_edge]:
            left = x + 1 + (right - x - 1) // 2
            break
    left = max([left, right - 10 * h] + [box[2] for box in blocking(ny1, ny2) if box[2] <= right])
    before = [left, ny1 - h // 2, right, ny2 + h // 2]
    if right - left < min_width or overlaps_box(before):
        before = None

    # After the stem: its last printed line above a box on a later row
    # (a choice box framing the options), otherwise the number's own row.
    line = [ny1, ny2]
    if box_px[1] >= ny2:
        rows = (template_gray[ny1:box_px[1], left_edge:right_edge] < 200).any(axis=1)
        y = len(rows) - 1
        while y >= 0 and not rows[y]:
            y -= 1
        if y >= 0:
            bottom = y + 1
            while y >= 0 and rows[y]:
                y -= 1
            if bottom - (y + 1) >= h // 2:
                line = [ny1 + y + 1, ny1 + bottom]
    start = nx2 if line == [ny1, ny2] else left_edge
    ink = printed(line[0], line[1], start, right_edge)
    first = next((i for i, value in enumerate(ink) if value), None)
    after = None
    if first is not None:
        end, gap = start + first, 0
        for i in range(first, len(ink)):
            if ink[i]:
                end, gap = start + i + 1, 0
            else:
                gap += 1
                if gap >= round(1.5 * h):
                    break
        stop = right_edge
        following = next((end + i for i, value in enumerate(ink[end - start:]) if value), None)
        if following is not None:
            stop = end + (following - end) // 2
        stop = min([stop, end + 10 * h] + [box[0] for box in blocking(*line) if box[0] >= end])
        candidate = [end, line[0] - h // 2, stop, line[1] + h // 2]
        if stop - end >= min_width and not overlaps_box(candidate):
            after = candidate
    return before, after


def _route_unmarked_answer_positions(
    schema, answer_regions, handwriting, anchor_by_number, drawing_questions,
    student_rgb, template_rgb, columns,
):
    """Pick the ink-bearing side for a slot whose page prints no position.

    Candidates are the frozen model box, the blank margin before the printed
    number and the blank space after the stem's last line. Ink in exactly one
    candidate decides where the slot label goes; ink in more than one is
    returned for teacher review. Exemptions and frozen geometry never move.
    Ink is counted against the template baseline, so printed text inside an
    exemption box (a bracket, a row of options) is never mistaken for ink.
    """
    height, width = student_rgb.shape[:2]

    def px(box):
        return [round(box[0] * width / 1000), round(box[1] * height / 1000),
                round(box[2] * width / 1000), round(box[3] * height / 1000)]

    by_number = {item["question_number"]: item for item in answer_regions}
    unmarked = [q["question_number"] for q in schema if q.get("answer_position") == "unmarked"]
    # Boxes of unmarked neighbours share a gap at its middle instead of
    # blocking; every other answer, handwriting or drawing area is fixed.
    obstacles = [px(box) for item in answer_regions + handwriting if item["question_number"] not in unmarked
                 for box in item["regions"]]
    obstacles += [px(box) for question in drawing_questions.values() for box in question["drawing_regions"]]
    template_gray = cv2.cvtColor(template_rgb, cv2.COLOR_RGB2GRAY)
    records, plans = [], []
    for number in unmarked:
        item = by_number.get(number)
        record = {"question_number": number}
        records.append(record)
        if (number in drawing_questions or item is None or len(item["regions"]) != 1
                or item.get("region_kinds", ["answer"]) != ["answer"]):
            record["decision"] = "not_single_text_slot"
            continue
        number_box = anchor_by_number.get(number, {}).get("question_number_bbox")
        if not number_box:
            record["decision"] = "no_printed_number"
            continue
        number_px, box_px = px(number_box), px(item["regions"][0])
        h = max(8, number_px[3] - number_px[1])
        center = (number_px[0] + number_px[2]) / 2
        column = next(((left, right) for left, right in columns if left <= center < right), None)
        record.update(slot_id=(item.get("slot_ids") or [None])[0], number_bbox_px=number_px, box_px=box_px)
        if column is None:
            record["decision"] = "number_outside_columns"
            continue
        before, after = _unmarked_zones(number_px, box_px, column, template_gray, obstacles, h)
        record.update(before_number_px=before, after_stem_px=after)
        plans.append((record, item, h, {"box": box_px, "before_number": before, "after_stem": after}))
    review = []
    routed = dict(by_number)
    for record, item, h, zones in plans:
        # Another slot's margin zone owns its ink, even inside this box.
        others = [zone for other in plans if other[0] is not record
                  for side, zone in other[3].items() if side != "box" and zone]
        pad = round(0.3 * h)
        ink, extents = {}, {}
        for side, zone in zones.items():
            if zone is None:
                continue
            ink[side], extents[side] = _new_ink_extent(
                student_rgb, template_rgb,
                [max(0, zone[0] - h), max(0, zone[1] - h), min(width, zone[2] + h), min(height, zone[3] + h)],
                [zone[0] - (pad if side == "box" else 0), zone[1] - pad, zone[2] + (pad if side == "box" else 0),
                 zone[3] + pad], others)
        threshold = max(12, round(0.05 * h * h))
        sides = [side for side, value in ink.items() if value >= threshold]
        record.update(ink_px=ink, min_ink_px=threshold)
        if len(sides) > 1:
            record["decision"] = "ambiguous"
            review.append(record["question_number"])
        elif sides and sides[0] != "box":
            side = sides[0]
            # Frame the ink itself so the label sits level with it.
            x1, y1, x2, y2 = extents[side]
            target = [max(0, x1 - pad), max(0, y1 - pad), min(width, x2 + pad), min(height, y2 + pad)]
            moved = [math.floor(target[0] * 1000 / width), math.floor(target[1] * 1000 / height),
                     math.ceil(target[2] * 1000 / width), math.ceil(target[3] * 1000 / height)]
            routed[record["question_number"]] = {**item, "regions": [moved]}
            record.update(decision=side, label_region_1000=moved)
        else:
            record["decision"] = "box"
    return [routed[item["question_number"]] for item in answer_regions], records, review
