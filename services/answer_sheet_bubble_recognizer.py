"""Local pixel recognizer for filled answer-sheet bubbles (answer-sheet slice 4).

Each bubble-grid row -- a whole choice question (``"3"``) or one small
question (``"1(2)"``) -- is decided as 对 / 错 / 空答 / 多涂 / needs_review by
comparing pixels only: no vision model, no network, no file access.  Input is a
student page already registered to the answer-sheet template's canonical frame
plus the teacher-confirmed grid geometry resolved from the frozen template
(``AnswerSheetTemplateRegistry.resolve_bubble_regions``).

The student's own ink is the baseline: whether a cell is filled is judged by
how it compares with the same student's other cells of that row, never against
a blank-template image or an absolute darkness threshold.  The comparison
target is the answer's fill state (only the answer cells filled).  No option
letter is "read"; the per-cell difference is classified directly.  Thresholds
and their rationale: internal design note 2026-09-27《气泡像素容差》 (not
published with this repository).

Every function is pure and deterministic (integer pixel arithmetic, no
randomness): identical pixels and geometry give identical results.  Input
outside the contract raises ``ValueError``; unreadable or ambiguous content
becomes ``needs_review`` for the teacher, never a guessed verdict.
"""

from __future__ import annotations

from itertools import product
from typing import Any, Mapping, Sequence

import numpy as np

from services.answer_sheet_layout_contract import (
    GRID_ORDERS,
    answer_key,
    choice_row_keys,
    normalize_choice_options,
    normalized_bbox_to_pixels,
)
from services.grading_contract import normalize_choice_answer

RECOGNIZER_VERSION = "answer-sheet-bubble-pixel-v1"

OUTCOME_CORRECT = "correct"
OUTCOME_WRONG = "wrong"
OUTCOME_BLANK = "blank"
OUTCOME_MULTIPLE = "multiple"
OUTCOME_NEEDS_REVIEW = "needs_review"
OUTCOME_LABELS = {
    OUTCOME_CORRECT: "对",
    OUTCOME_WRONG: "错",
    OUTCOME_BLANK: "空答",
    OUTCOME_MULTIPLE: "多涂",
    OUTCOME_NEEDS_REVIEW: "needs_review",
}

CELL_MARKED = "marked"
CELL_EMPTY = "empty"
CELL_UNCERTAIN = "uncertain"
CELL_UNREAD = "unread"

# --- tolerance matrix ------------------------------------------------------
# "ink" is the mean darkness of a cell's inner area relative to the page's own
# paper level (0 = paper, 1 = black).  "excess" is ink above the lightest cell
# of the same row (the printed-bubble level of that student's row), so print,
# paper tint and light shading cancel out.  See the tolerance note for why
# each value protects correct answers.
INNER_MARGIN_PERCENT = 15      # trimmed from each cell side: borders, residual misregistration
PAPER_PERCENTILE = 95          # paper level = this percentile of page gray
PAPER_SAMPLE_STRIDE = 4        # page sampling step for the paper level
MIN_PAPER_LEVEL = 96           # darker "paper" = unreadable page
MIN_INNER_PIXELS = 16          # smaller inner areas are unreadable
NOISE_FLOOR = 0.05             # below: no ink beyond print variation and scan noise
REAL_MARK = 0.10               # at/above: a real (possibly light) mark; between: faint
MARKED_RELATIVE = 0.60         # filled: >= this share of the row's strongest mark
EMPTY_RELATIVE = 0.30          # empty: <= this share (erased residue, bleed)
BRIDGE_RELATIVE = 0.50         # ink between two marked cells joining them = stray ink
PRINT_CEILING = 0.25           # a page's lightest cell this dark cannot be print only


# --- pixel measurement -----------------------------------------------------

def _gray(image_rgb: np.ndarray) -> np.ndarray:
    """Integer BT.601 luma; exact on every platform."""
    rgb = image_rgb.astype(np.int64)
    return (rgb[..., 0] * 299 + rgb[..., 1] * 587 + rgb[..., 2] * 114 + 500) // 1000


def paper_level(image_rgb: np.ndarray) -> int:
    """Nearest-rank ``PAPER_PERCENTILE`` of the page's gray (its paper white)."""
    sample = _gray(image_rgb[::PAPER_SAMPLE_STRIDE, ::PAPER_SAMPLE_STRIDE]).ravel()
    counts = np.bincount(sample, minlength=256)
    rank = -(-sample.size * PAPER_PERCENTILE // 100)
    return int(np.searchsorted(np.cumsum(counts), rank))


def inner_box(pixel_box: Sequence[int]) -> list[int]:
    """Cell pixel box shrunk by ``INNER_MARGIN_PERCENT`` on every side."""
    x1, y1, x2, y2 = pixel_box
    margin_x = (x2 - x1) * INNER_MARGIN_PERCENT // 100
    margin_y = (y2 - y1) * INNER_MARGIN_PERCENT // 100
    return [x1 + margin_x, y1 + margin_y, x2 - margin_x, y2 - margin_y]


def _area(box: Sequence[int]) -> int:
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def region_ink(image_rgb: np.ndarray, box: Sequence[int], paper: int) -> float:
    """Mean darkness of a half-open pixel box relative to the paper level."""
    x1, y1, x2, y2 = box
    darkness = np.clip(paper - _gray(image_rgb[y1:y2, x1:x2]), 0, None)
    return float(darkness.sum(dtype=np.int64)) / float(darkness.size * paper)


def _bridge_box(first: Sequence[int], second: Sequence[int], grid_order: str) -> list[int] | None:
    """Pixels between two neighbouring inner boxes, or None when they do not face."""
    if grid_order == "left_to_right":
        box = [first[2], max(first[1], second[1]), second[0], min(first[3], second[3])]
    else:
        box = [max(first[0], second[0]), first[3], min(first[2], second[2]), second[1]]
    return box if box[2] > box[0] and box[3] > box[1] else None


# --- pure decision ---------------------------------------------------------

def _answer_options(raw: object, options: Sequence[str], allow_multiple: bool, label: str) -> list[str]:
    items = list(raw) if isinstance(raw, (str, list, tuple)) else None
    if (
        not items
        or any(not isinstance(item, str) or item not in options for item in items)
        or len(set(items)) != len(items)
    ):
        raise ValueError(f"{label}标准答案涂态必须是本行选项格中不重复的选项")
    if not allow_multiple and len(items) != 1:
        raise ValueError(f"{label}为单选涂卡，标准答案必须恰好一个选项")
    return [option for option in options if option in items]


def _cell_state(excess: float, top: float) -> str:
    if excess < NOISE_FLOOR or excess <= EMPTY_RELATIVE * top:
        return CELL_EMPTY
    if excess >= REAL_MARK and excess >= MARKED_RELATIVE * top:
        return CELL_MARKED
    return CELL_UNCERTAIN


def _interpretation(selected: set[str], answer: set[str], allow_multiple: bool) -> tuple:
    """Outcome of one reading of the row, plus what scoring could still need."""
    if not selected:
        return (OUTCOME_BLANK,)
    if not allow_multiple and len(selected) > 1:
        return (OUTCOME_MULTIPLE,)
    if selected == answer:
        return (OUTCOME_CORRECT,)
    if allow_multiple:
        # Partial-credit rules downstream distinguish "extra option chosen"
        # from "answer options missed", so two readings only agree when both
        # facts agree too.
        extra = bool(selected - answer)
        return (OUTCOME_WRONG, extra, frozenset() if extra else frozenset(selected & answer))
    return (OUTCOME_WRONG,)


def classify_row(
    cell_inks: Sequence[float],
    *,
    options: Sequence[str],
    answer_options: Sequence[str] | str,
    allow_multiple: bool,
    page_floor: float,
    bridge_excess: Sequence[float | None] | None = None,
) -> dict[str, Any]:
    """Decide one grid row from its measured cell inks.

    ``page_floor`` is the lightest cell ink on the student's page (its print
    level); ``bridge_excess`` holds, per neighbouring cell pair, the ink between
    the two cells above the page's lightest such gap (None: no gap measured).
    """
    options = list(options)
    label = "涂卡行"
    if len(options) != len(set(options)) or len(options) < 2 or len(cell_inks) != len(options):
        raise ValueError(f"{label}选项格与测量值数量不一致")
    inks = [float(value) for value in cell_inks]
    if any(not np.isfinite(value) or not 0.0 <= value <= 1.0 for value in inks):
        raise ValueError(f"{label}墨迹测量值无效")
    if not isinstance(allow_multiple, bool):
        raise ValueError(f"{label}单选/多选设置无效")
    answer = _answer_options(answer_options, options, allow_multiple, label)
    row_floor = min(inks)
    if not np.isfinite(page_floor) or page_floor > row_floor:
        raise ValueError(f"{label}页面基准不能高于本行最浅格")
    bridges = list(bridge_excess) if bridge_excess is not None else [None] * (len(options) - 1)
    if len(bridges) != len(options) - 1:
        raise ValueError(f"{label}相邻格间隙测量数量不一致")

    lift = row_floor - page_floor
    excess = [ink - row_floor for ink in inks]
    top = max(excess)
    states = [_cell_state(value, top) for value in excess]
    marked = [option for option, state in zip(options, states) if state == CELL_MARKED]
    uncertain = [option for option, state in zip(options, states) if state == CELL_UNCERTAIN]

    reason = ""
    outcome = OUTCOME_NEEDS_REVIEW
    if lift >= REAL_MARK:
        # Even the row's lightest cell carries a real mark relative to the
        # page's print level: all options inked, a smear or a shadow band.
        reason = "row_uniformly_inked"
    else:
        for index, value in enumerate(bridges):
            if (
                value is not None
                and states[index] != CELL_EMPTY
                and states[index + 1] != CELL_EMPTY
                and value >= NOISE_FLOOR
                and value >= BRIDGE_RELATIVE * min(excess[index], excess[index + 1])
            ):
                # One connected stroke or blot across both cells, not two fills.
                reason = "ink_bridges_cells"
                break
    if not reason:
        answer_set = set(answer)
        readings = {
            _interpretation(
                set(marked) | {option for option, on in zip(uncertain, choice) if on},
                answer_set,
                allow_multiple,
            )
            for choice in product((False, True), repeat=len(uncertain))
        }
        if len(readings) > 1:
            # Only uncertainty that changes the verdict goes to the teacher.
            reason = "uncertain_marks" if marked else "faint_marks"
        else:
            outcome = next(iter(readings))[0]
            if outcome == OUTCOME_BLANK and lift >= NOISE_FLOOR:
                reason, outcome = "row_faint_uniform_ink", OUTCOME_NEEDS_REVIEW
            else:
                reason = {
                    OUTCOME_CORRECT: "matches_answer_fill_state",
                    OUTCOME_WRONG: "differs_from_answer_fill_state",
                    OUTCOME_BLANK: "no_marks",
                    OUTCOME_MULTIPLE: "multiple_marks_on_single_choice",
                }[outcome]

    decided = outcome != OUTCOME_NEEDS_REVIEW
    return {
        "outcome": outcome,
        "label": OUTCOME_LABELS[outcome],
        "reason": reason,
        "needs_review": not decided,
        "answer_options": answer,
        "marked_options": marked,
        "uncertain_options": uncertain,
        "missing_options": [option for option in answer if option not in marked] if decided else [],
        "extra_options": [option for option in marked if option not in answer] if decided else [],
        "cell_states": states,
        "excess": excess,
        "relative": [value / top if top > 0 else 0.0 for value in excess],
        "row_floor": row_floor,
        "row_lift": lift,
        "top_excess": top,
    }


# --- page / sheet ----------------------------------------------------------

def _checked_image(image_rgb: object) -> np.ndarray:
    if (
        not isinstance(image_rgb, np.ndarray)
        or image_rgb.dtype != np.uint8
        or image_rgb.ndim != 3
        or image_rgb.shape[2] != 3
        or image_rgb.shape[0] < 1
        or image_rgb.shape[1] < 1
    ):
        raise ValueError("学生页面图像必须是 RGB uint8 数组")
    return image_rgb


def _checked_bbox(value: object, label: str) -> list[int]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 4
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
        or not (0 <= value[0] < value[2] <= 1000 and 0 <= value[1] < value[3] <= 1000)
    ):
        raise ValueError(f"{label}模板坐标无效")
    return list(value)


def _prepared_row(row: object, width: int, height: int) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        raise ValueError("涂卡网格行格式错误")
    question_no = str(row.get("question_no") or "").strip()
    subquestion_no = str(row.get("subquestion_no") or "").strip()
    if not question_no:
        raise ValueError("涂卡网格行缺少题号")
    key = answer_key(question_no, subquestion_no)
    label = f"第{key}题"
    if list(row.get("page_size") or []) != [width, height]:
        # Registration to the template frame happens upstream; a page in any
        # other frame would put every cell box on the wrong pixels.
        raise ValueError(f"{label}学生页面尺寸与答题卡模板页面不一致，须先配准到模板坐标系")
    allow_multiple = row.get("allow_multiple")
    if not isinstance(allow_multiple, bool):
        raise ValueError(f"{label}涂卡单选/多选设置无效")
    grid_order = row.get("grid_order")
    if grid_order not in GRID_ORDERS:
        raise ValueError(f"{label}涂卡网格排列方向无效")
    cells = row.get("cells")
    if not isinstance(cells, list) or any(not isinstance(cell, Mapping) for cell in cells):
        raise ValueError(f"{label}涂卡网格缺少选项格")
    options = [cell.get("option") for cell in cells]
    if any(not isinstance(option, str) for option in options) or normalize_choice_options(options) != options:
        raise ValueError(f"{label}涂卡选项格必须是 2–8 个不重复的大写字母 A–H")
    boxes: list[list[int]] = []
    for option, cell in zip(options, cells):
        expected = normalized_bbox_to_pixels(_checked_bbox(cell.get("bbox"), f"{label}{option}选项格"), width, height)
        if list(cell.get("pixel_box") or []) != expected:
            raise ValueError(f"{label}{option}选项格像素框与模板几何不一致")
        boxes.append(expected)
    return {
        "answer_key": key,
        "label": label,
        "question_no": question_no,
        "subquestion_no": subquestion_no,
        "page_id": str(row.get("page_id") or ""),
        "region_id": str(row.get("region_id") or ""),
        "allow_multiple": allow_multiple,
        "grid_order": grid_order,
        "options": options,
        "pixel_boxes": boxes,
        "inner_boxes": [inner_box(box) for box in boxes],
    }


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(float(value), 4)


def _result(
    row: dict[str, Any],
    decision: dict[str, Any],
    *,
    paper: int,
    page_floor: float | None,
    inks: Sequence[float] | None,
    bridge_excess: Sequence[float | None],
) -> dict[str, Any]:
    states = decision.get("cell_states") or [CELL_UNREAD] * len(row["options"])
    excess = decision.get("excess") or [None] * len(row["options"])
    relative = decision.get("relative") or [None] * len(row["options"])
    return {
        "answer_key": row["answer_key"],
        "question_no": row["question_no"],
        "subquestion_no": row["subquestion_no"],
        "page_id": row["page_id"],
        "region_id": row["region_id"],
        "allow_multiple": row["allow_multiple"],
        "outcome": decision["outcome"],
        "label": decision["label"],
        "reason": decision["reason"],
        "needs_review": decision["needs_review"],
        "answer_options": list(decision["answer_options"]),
        "marked_options": list(decision.get("marked_options") or []),
        "uncertain_options": list(decision.get("uncertain_options") or []),
        "missing_options": list(decision.get("missing_options") or []),
        "extra_options": list(decision.get("extra_options") or []),
        "cells": [
            {
                "option": option,
                "pixel_box": list(box),
                "inner_box": list(inner),
                "ink": _rounded(inks[index]) if inks is not None else None,
                "excess": _rounded(excess[index]),
                "relative": _rounded(relative[index]),
                "state": states[index],
            }
            for index, (option, box, inner) in enumerate(
                zip(row["options"], row["pixel_boxes"], row["inner_boxes"])
            )
        ],
        "measures": {
            "paper_level": paper,
            "page_floor": _rounded(page_floor),
            "row_floor": _rounded(decision.get("row_floor")),
            "row_lift": _rounded(decision.get("row_lift")),
            "top_excess": _rounded(decision.get("top_excess")),
            "bridge_excess": [_rounded(value) for value in bridge_excess],
        },
        "recognizer_version": RECOGNIZER_VERSION,
    }


def _review(row: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "outcome": OUTCOME_NEEDS_REVIEW,
        "label": OUTCOME_LABELS[OUTCOME_NEEDS_REVIEW],
        "reason": reason,
        "needs_review": True,
        "answer_options": row["answer"],
    }


def recognize_bubble_page(
    image_rgb: np.ndarray,
    rows: Sequence[Mapping[str, Any]],
    answers: Mapping[str, Sequence[str] | str],
) -> dict[str, dict[str, Any]]:
    """Recognize every bubble-grid row of one registered student page.

    ``rows`` are resolved grid rows of a single page (pass all of them: the
    page's lightest cell and gap are the print reference); ``answers`` maps
    answer keys (``"3"``, ``"1(2)"``) to the answer's filled options.
    """
    image = _checked_image(image_rgb)
    height, width = image.shape[:2]
    if not isinstance(rows, (list, tuple)) or not rows:
        raise ValueError("没有可识别的涂卡网格")
    if not isinstance(answers, Mapping):
        raise ValueError("标准答案涂态格式错误")
    prepared = [_prepared_row(row, width, height) for row in rows]
    keys = [row["answer_key"] for row in prepared]
    if len(set(keys)) != len(keys):
        raise ValueError("同一页涂卡网格行重复")
    if len({row["page_id"] for row in prepared}) != 1:
        raise ValueError("一次只能识别同一页的涂卡网格")
    for row in prepared:
        if row["answer_key"] not in answers:
            raise ValueError(f"{row['label']}缺少标准答案涂态，禁止判分")
        row["answer"] = _answer_options(
            answers[row["answer_key"]], row["options"], row["allow_multiple"], row["label"],
        )

    paper = paper_level(image)
    if paper < MIN_PAPER_LEVEL:
        return {
            row["answer_key"]: _result(
                row, _review(row, "page_unreadable"), paper=paper, page_floor=None,
                inks=None, bridge_excess=[None] * (len(row["options"]) - 1),
            )
            for row in prepared
        }
    for row in prepared:
        row["readable"] = all(_area(box) >= MIN_INNER_PIXELS for box in row["inner_boxes"])
        if not row["readable"]:
            continue
        row["inks"] = [region_ink(image, box, paper) for box in row["inner_boxes"]]
        bands = [
            _bridge_box(first, second, row["grid_order"])
            for first, second in zip(row["inner_boxes"], row["inner_boxes"][1:])
        ]
        row["bridge_inks"] = [
            None if band is None else region_ink(image, band, paper) for band in bands
        ]
    readable = [row for row in prepared if row["readable"]]
    page_floor = min((ink for row in readable for ink in row["inks"]), default=None)
    bridge_floor = min(
        (value for row in readable for value in row["bridge_inks"] if value is not None),
        default=None,
    )

    results: dict[str, dict[str, Any]] = {}
    for row in prepared:
        no_bridges: list[float | None] = [None] * (len(row["options"]) - 1)
        if not row["readable"]:
            results[row["answer_key"]] = _result(
                row, _review(row, "cell_too_small"), paper=paper, page_floor=page_floor,
                inks=None, bridge_excess=no_bridges,
            )
            continue
        if page_floor >= PRINT_CEILING:
            # No cell on the page is light enough to be printed bubble only,
            # so the student's print reference is missing.
            results[row["answer_key"]] = _result(
                row, _review(row, "page_floor_implausible"), paper=paper, page_floor=page_floor,
                inks=row["inks"], bridge_excess=no_bridges,
            )
            continue
        bridge_excess = [
            None if value is None else value - bridge_floor for value in row["bridge_inks"]
        ]
        decision = classify_row(
            row["inks"],
            options=row["options"],
            answer_options=row["answer"],
            allow_multiple=row["allow_multiple"],
            page_floor=page_floor,
            bridge_excess=bridge_excess,
        )
        results[row["answer_key"]] = _result(
            row, decision, paper=paper, page_floor=page_floor,
            inks=row["inks"], bridge_excess=bridge_excess,
        )
    return results


def choice_row_answer(spec: Mapping[str, Any], subquestion_no: str = "") -> list[str]:
    """Answer fill state of one grid row from the frozen paper template.

    A whole question uses its confirmed ``standard_answer``.  A small question
    ``n`` uses ``answer_slots[n-1]`` and only when ``answer_slots`` lists one
    answer per declared small question: a single ``standard_answer`` cannot
    say which small question it belongs to, so anything else fails closed.
    """
    question_no = str(spec.get("question_no") or "").strip()
    sub = str(subquestion_no or "").strip()
    label = f"第{answer_key(question_no, sub)}题"
    if str(spec.get("question_type") or "") != "choice":
        raise ValueError(f"{label}不是试卷模板中的选择题")
    mode = str(spec.get("choice_mode") or "")
    if mode not in {"single", "multiple"}:
        raise ValueError(f"{label}尚未确认单选/多选，禁止判分")
    row_keys = choice_row_keys(dict(spec))
    if sub not in row_keys:
        raise ValueError(f"{label}不在试卷模板的小问结构中")
    if sub:
        slots = spec.get("answer_slots")
        if not isinstance(slots, list) or len(slots) != len(row_keys):
            raise ValueError(f"{label}的小问标准答案未逐小问确认（answer_slots 须与小问一一对应），禁止判分")
        raw = slots[int(sub) - 1]
    else:
        raw = spec.get("standard_answer")
    answer = normalize_choice_answer(raw)
    if not answer or (mode == "single" and len(answer) != 1) or (mode == "multiple" and len(answer) < 2):
        raise ValueError(f"{label}标准答案与单选/多选设置不一致，禁止判分")
    return list(answer)


def _sheet_rows_and_answers(
    resolved: Mapping[str, Any],
    question_specs: Sequence[Mapping[str, Any]],
) -> tuple[str, str, Mapping[str, Any], dict[str, list[Mapping[str, Any]]], dict[str, list[str]]]:
    """Validated (code, fingerprint, rows, rows per page, answer fill states)."""
    code = resolved.get("template_code") if isinstance(resolved, Mapping) else None
    fingerprint = resolved.get("template_fingerprint") if isinstance(resolved, Mapping) else None
    questions = resolved.get("questions") if isinstance(resolved, Mapping) else None
    if not isinstance(code, str) or not code or not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError("涂卡网格必须来自已发布的答题卡模板")
    if not isinstance(questions, Mapping) or not questions:
        raise ValueError("答题卡模板没有已确认的涂卡网格")
    specs = {str(spec.get("question_no") or "").strip(): spec for spec in question_specs}
    answers: dict[str, list[str]] = {}
    by_page: dict[str, list[Mapping[str, Any]]] = {}
    for key, row in questions.items():
        question_no = str(row.get("question_no") or "").strip()
        sub = str(row.get("subquestion_no") or "").strip()
        if answer_key(question_no, sub) != key:
            raise ValueError(f"第{key}题涂卡网格题号不一致")
        spec = specs.get(question_no)
        if spec is None:
            raise ValueError(f"第{key}题不在试卷模板中，禁止判分")
        answers[key] = choice_row_answer(spec, sub)
        if row.get("allow_multiple") is not (str(spec.get("choice_mode")) == "multiple"):
            raise ValueError(f"第{key}题单选/多选设置与试卷模板不一致")
        by_page.setdefault(str(row.get("page_id") or ""), []).append(row)
    return code, fingerprint, questions, by_page, answers


def recognize_bubble_sheet(
    page_images: Mapping[str, np.ndarray],
    resolved: Mapping[str, Any],
    question_specs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Recognize every confirmed grid row of one student's answer sheet.

    ``resolved`` is ``resolve_bubble_regions`` output (frozen, teacher-
    confirmed geometry); ``question_specs`` is the batch's frozen paper-template
    snapshot; ``page_images`` maps page ids to registered student pages.
    """
    code, fingerprint, questions, by_page, answers = _sheet_rows_and_answers(resolved, question_specs)
    results: dict[str, dict[str, Any]] = {}
    for page_id, rows in by_page.items():
        image = page_images.get(page_id)
        if image is None:
            raise ValueError(f"缺少页面 {page_id} 的学生图像，不能识别涂卡")
        results.update(recognize_bubble_page(image, rows, answers))
    ordered = {key: results[key] for key in questions}
    return {
        "template_code": code,
        "template_fingerprint": fingerprint,
        "recognizer_version": RECOGNIZER_VERSION,
        "questions": ordered,
        "needs_review": [key for key, result in ordered.items() if result["needs_review"]],
    }


def recognize_bubble_template_page(
    image_rgb: np.ndarray,
    resolved: Mapping[str, Any],
    question_specs: Sequence[Mapping[str, Any]],
    page_id: str,
) -> dict[str, Any]:
    """Recognize the confirmed grid rows of ONE registered template page.

    Same validation as ``recognize_bubble_sheet`` (every row of the sheet must
    have a valid answer contract, otherwise nothing is judged); only the rows
    printed on ``page_id`` are measured, all of them together so the page's
    own print reference is complete.  A page without grid rows returns an
    empty ``questions`` map.
    """
    code, fingerprint, questions, by_page, answers = _sheet_rows_and_answers(resolved, question_specs)
    rows = by_page.get(str(page_id or ""), [])
    results = recognize_bubble_page(image_rgb, rows, answers) if rows else {}
    ordered = {key: results[key] for key in questions if key in results}
    return {
        "template_code": code,
        "template_fingerprint": fingerprint,
        "recognizer_version": RECOGNIZER_VERSION,
        "page_id": str(page_id or ""),
        "questions": ordered,
        "needs_review": [key for key, result in ordered.items() if result["needs_review"]],
    }
