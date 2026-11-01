"""Column-split transcription contract for answer sheets (answer-sheet slice 5).

A student page registered to the answer-sheet template frame is cut into the
template's frozen full-height columns (``question_crop_plan``) and ONE vision
request carries all N column images of that page, so a small question that
continues into the next column keeps its page context.  Before any image
leaves this module the teacher-confirmed bubble-grid cells are whitened at
their template pixel boxes -- a deterministic mask of known cells, not a
template difference -- so filled bubbles never reach the model.
``choice_mark`` answers come only from the local pixel recognizer and are
referenced here, never transcribed.

This module transcribes; it never grades.  Requests carry layout only
(question numbers, boxes, option letters), never a paper-template answer,
score or rubric.  A response is accepted only when it matches the plan's
entries exactly; anything malformed fails closed with no partial or default
result.  Entries are merged back per question number + small question in
column order.

High-risk transcribed fields then go through a verification stage: their
answer boxes are re-cropped from the same masked page and read again blind
(the second request never sees the first reading).  Agreement confirms the
field; disagreement or a failed check sends it to the teacher with both
readings.  The first reading is never replaced by the second.  Agreement is
exact on the high-risk parts (question number binding, answer state, short
decisive answers); long solution text is compared fuzzily and anything short
of a clear match goes to the teacher (see ``_compare_readings`` and the internal design note 2026-09-27
《转录核验校准》).

Every image sent, every request and every raw response is kept inside the
batch directory for the teacher.  Student identity is not part of this
contract: records name the page they came from, never a student.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import io
import json
import math
import os
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

import numpy as np

from services.answer_sheet_bubble_recognizer import RECOGNIZER_VERSION
from services.answer_sheet_layout_contract import (
    answer_key,
    normalize_choice_options,
    normalized_bbox_to_pixels,
)
from services.template_anchor_crops import validate_anchor_crop_plan

COLUMN_TRANSCRIPTION_CONTRACT_VERSION = "answer-sheet-column-transcription-v1"
VERIFICATION_CONTRACT_VERSION = "answer-sheet-transcription-verification-v1"

STATE_VISIBLE = "visible"
STATE_BLANK = "blank"
STATE_UNREADABLE = "unreadable"
ANSWER_STATES = (STATE_VISIBLE, STATE_BLANK, STATE_UNREADABLE)
UNREADABLE_TEXT = "[无法识别]"

SOURCE_TRANSCRIPTION = "column_transcription"
SOURCE_BUBBLE = "bubble_pixel_recognizer"
TRANSCRIBED_KINDS = ("fill", "solve", "choice_handwritten")

RECORD_TRANSCRIBED = "transcribed"
RECORD_NOT_REQUIRED = "no_transcription_required"
RECORD_FAILED = "failed"

VERIFY_NOT_REQUIRED = "not_required"
VERIFY_PENDING = "pending"
VERIFY_CONFIRMED = "confirmed"
VERIFY_DISPUTED = "disputed"
VERIFY_FAILED = "failed"

MAX_IMAGES_PER_REQUEST = 12       # services.ocr_service.call_vision_images accepts 1..12
VERIFY_CONFIDENCE_FLOOR = 0.90    # same floor as roster identity matching

# --- verification calibration knobs ------------------------------------------
# Synthetic fixtures only; no real-sample calibration yet (see the note in
# internal design note 2026-09-27《转录核验校准》).
#
# Always exact, for every field: the question number / small question the
# check is bound to (by check id) and the answer state (visible / blank /
# unreadable).  Also exact: every fill and handwritten-choice answer and any
# solve text shorter than LONG_TEXT_MIN_CHARS -- a short answer is decisive,
# so one differing character is a real disagreement.
#
# Solve text of at least LONG_TEXT_MIN_CHARS canonical characters is not
# required to be identical (two independent readings of a long derivation
# almost never are), but it only agrees when BOTH hold:
#   * the sequence of numbers in the two readings is identical (a misread
#     digit is the error that changes a math verdict), and
#   * difflib similarity of the canonical texts >= LONG_TEXT_MIN_SIMILARITY.
# Anything else is "disputed" and goes to the teacher with both readings; the
# first reading is still the only one ever graded.
LONG_TEXT_KINDS = frozenset({"solve"})
LONG_TEXT_MIN_CHARS = 24
LONG_TEXT_MIN_SIMILARITY = 0.90
MASK_GRAY = 255
_TOKENS_PER_KIND = {"solve": 768, "fill": 128, "choice_handwritten": 64}
_MIN_TOKENS = 1024
_MAX_TOKENS = 16384
_READING_FIELDS = ("student_answer", "answer_state", "confidence")

VisionImagesCall = Callable[..., Awaitable[str]]


class ColumnTranscriptionError(ValueError):
    """A model response broke the contract; nothing from it is accepted."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


# --- helpers -----------------------------------------------------------------

def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _checked_bbox(value: object, label: str) -> list[int]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 4
        or any(not _is_int(item) for item in value)
        or not (0 <= value[0] < value[2] <= 1000 and 0 <= value[1] < value[3] <= 1000)
    ):
        raise ValueError(f"{label}模板坐标无效")
    return [int(item) for item in value]


def _intersects(first: Sequence[int], second: Sequence[int]) -> bool:
    return (
        min(first[2], second[2]) > max(first[0], second[0])
        and min(first[3], second[3]) > max(first[1], second[1])
    )


def _plan_digest(plan: Mapping[str, Any]) -> str:
    body = {key: value for key, value in plan.items() if key != "plan_sha256"}
    return _sha256(_stable_json(body).encode("utf-8"))


def _checked_plan(plan: object) -> Mapping[str, Any]:
    if (
        not isinstance(plan, Mapping)
        or plan.get("contract_version") != COLUMN_TRANSCRIPTION_CONTRACT_VERSION
        or plan.get("plan_sha256") != _plan_digest(plan)
    ):
        raise ValueError("分栏转录计划无效或已被改动")
    return plan


# --- column plan (template side, no image) -----------------------------------

def _plan_columns(artifact: Mapping[str, Any], page_id: str, width: int, height: int) -> list[dict]:
    try:
        crop_plan = validate_anchor_crop_plan(
            artifact.get("question_crop_plan"),
            artifact_sha256=str(artifact.get("sha256") or ""),
        )
    except ValueError as exc:
        raise ValueError(f"答题卡页面 {page_id} 的分栏计划无效") from exc
    return [
        {
            "column": index,
            "bbox_1000": list(block["bbox_1000"]),
            "pixel_box": normalized_bbox_to_pixels(block["bbox_1000"], width, height),
        }
        for index, block in enumerate(crop_plan["blocks"], 1)
    ]


def _column_for(bbox: list[int], columns: list[dict], label: str) -> dict:
    owners = [
        column for column in columns
        if column["bbox_1000"][0] <= bbox[0] and bbox[2] <= column["bbox_1000"][2]
    ]
    if len(owners) != 1:
        raise ValueError(f"{label}答题区域跨越分栏边界，不能按栏转录")
    return owners[0]


def _box_in_column(bbox: list[int], column: dict, label: str) -> list[int]:
    left, _top, right, _bottom = column["bbox_1000"]
    span = right - left
    box = [
        round((bbox[0] - left) * 1000 / span), bbox[1],
        round((bbox[2] - left) * 1000 / span), bbox[3],
    ]
    if not 0 <= box[0] < box[2] <= 1000:
        raise ValueError(f"{label}答题区域在栏内过窄")
    return box


def _page_bubble_rows(
    bubble_rows: object,
    page_id: str,
    width: int,
    height: int,
    choice_regions: Mapping[str, Mapping[str, Any]],
) -> dict[str, list[dict]]:
    """Teacher-confirmed grid rows of this page, keyed by their region."""
    if not isinstance(bubble_rows, Mapping):
        raise ValueError("涂卡网格解析结果格式错误")
    by_region: dict[str, list[dict]] = {}
    for key, row in bubble_rows.items():
        if not isinstance(row, Mapping):
            raise ValueError("涂卡网格行格式错误")
        if str(row.get("page_id") or "") != page_id:
            continue
        question_no = str(row.get("question_no") or "").strip()
        subquestion_no = str(row.get("subquestion_no") or "").strip()
        label = f"第{key}题"
        if not question_no or answer_key(question_no, subquestion_no) != key:
            raise ValueError(f"{label}涂卡网格题号不一致")
        region_id = str(row.get("region_id") or "")
        region = choice_regions.get(region_id)
        if region is None or str(region.get("question_no") or "").strip() != question_no:
            raise ValueError(f"{label}涂卡网格不属于本页的涂卡区域")
        if list(row.get("page_size") or []) != [width, height]:
            raise ValueError(f"{label}涂卡网格页面尺寸与答题卡模板不一致")
        cells = row.get("cells")
        if not isinstance(cells, list) or not cells:
            raise ValueError(f"{label}涂卡网格缺少选项格")
        boxes: list[list[int]] = []
        cell_bboxes: list[list[int]] = []
        for cell in cells:
            if not isinstance(cell, Mapping):
                raise ValueError(f"{label}涂卡格式错误")
            bbox = _checked_bbox(cell.get("bbox"), f"{label}选项格")
            expected = normalized_bbox_to_pixels(bbox, width, height)
            if list(cell.get("pixel_box") or []) != expected:
                raise ValueError(f"{label}选项格像素框与模板几何不一致")
            boxes.append(expected)
            cell_bboxes.append(bbox)
        by_region.setdefault(region_id, []).append({
            "answer_key": key,
            "subquestion_no": subquestion_no,
            "grid_bbox": _checked_bbox(row.get("grid_bbox"), f"{label}涂卡网格"),
            "mask_boxes": boxes,
            "cell_bboxes": cell_bboxes,
        })
    return by_region


def _key_rank(question_no: str, subquestion_no: str) -> tuple[int, int] | None:
    if not question_no.isdigit() or (subquestion_no and not subquestion_no.isdigit()):
        return None
    return int(question_no), int(subquestion_no or 0)


def build_page_column_plan(
    *,
    page_id: str,
    artifact: Mapping[str, Any],
    answer_regions: Sequence[Mapping[str, Any]],
    bubble_rows: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Column plan of one template page: columns, entries, fields and mask.

    ``artifact`` is the page's frozen layout artifact (``question_crop_plan``,
    ``sha256``, ``width``/``height``); ``bubble_rows`` is the ``questions``
    map of ``AnswerSheetTemplateRegistry.resolve_bubble_regions``.  Identity
    regions are not part of the plan.  Fails closed when a region straddles a
    column boundary, a question's parts are not contiguous in column-major
    reading order, question numbers run backwards, a ``choice_mark`` region
    has no confirmed grid to mask, or a mask cell would cover a text box.
    """
    page_id = str(page_id or "").strip()
    if not page_id or not isinstance(artifact, Mapping):
        raise ValueError("答题卡页面或配准资源无效")
    width, height = artifact.get("width"), artifact.get("height")
    if not _is_int(width) or not _is_int(height) or width <= 0 or height <= 0:
        raise ValueError(f"答题卡页面 {page_id} 的模板尺寸无效")
    columns = _plan_columns(artifact, page_id, width, height)
    if not isinstance(answer_regions, (list, tuple)) or any(
        not isinstance(region, Mapping) for region in answer_regions
    ):
        raise ValueError("答题区域格式错误")
    regions = [region for region in answer_regions if str(region.get("page_id") or "") == page_id]
    choice_regions = {
        str(region.get("region_id") or ""): region
        for region in regions if region.get("region_type") == "choice_mark"
    }
    rows_by_region = _page_bubble_rows(bubble_rows, page_id, width, height, choice_regions)

    occurrences: list[dict] = []
    mask_boxes: list[list[int]] = []
    cell_bboxes: list[list[int]] = []
    text_bboxes: list[tuple[str, list[int]]] = []
    for index, region in enumerate(regions):
        region_id = str(region.get("region_id") or "").strip()
        question_no = str(region.get("question_no") or "").strip()
        subquestion_no = str(region.get("subquestion_no") or "").strip()
        kind = str(region.get("region_type") or "")
        label = f"第{answer_key(question_no, subquestion_no) or '?'}题"
        if not region_id or not question_no:
            raise ValueError(f"{label}答题区域缺少标识或题号")
        bbox = _checked_bbox(region.get("bbox"), label)
        column = _column_for(bbox, columns, label)
        reading_order = region.get("reading_order")
        order = (reading_order if _is_int(reading_order) else index + 1, bbox[1], bbox[0])
        if kind == "choice_mark":
            rows = rows_by_region.get(region_id)
            if not rows:
                raise ValueError(f"{label}涂卡区域没有已确认的涂卡网格，无法遮罩涂卡格")
            for row in sorted(rows, key=lambda item: (item["grid_bbox"][1], item["grid_bbox"][0])):
                occurrences.append({
                    "answer_key": row["answer_key"], "question_no": question_no,
                    "subquestion_no": row["subquestion_no"], "kind": kind,
                    "column": column["column"], "order": (*order, row["grid_bbox"][1]),
                    "region_id": region_id,
                })
                mask_boxes.extend(row["mask_boxes"])
                cell_bboxes.extend(row["cell_bboxes"])
            continue
        if kind not in TRANSCRIBED_KINDS:
            raise ValueError(f"{label}答题区域类型无效")
        text_bboxes.append((label, bbox))
        extras: dict[str, Any] = {}
        if kind == "choice_handwritten":
            extras = {
                "choice_mode": "multiple" if bool(region.get("allow_multiple")) else "single",
                "choice_options": normalize_choice_options(region.get("choice_options")),
            }
        occurrences.append({
            "answer_key": answer_key(question_no, subquestion_no), "question_no": question_no,
            "subquestion_no": subquestion_no, "kind": kind, "column": column["column"],
            "order": (*order, 0), "region_id": region_id, "extras": extras,
            "region": {
                "region_id": region_id,
                "bbox_1000": bbox,
                "pixel_box": normalized_bbox_to_pixels(bbox, width, height),
                "column_box_1000": _box_in_column(bbox, column, label),
            },
        })
    # Checked in template coordinates: pixel rounding of two boxes that only
    # touch may share one edge pixel, which is not an overlap.
    for label, bbox in text_bboxes:
        if any(_intersects(bbox, cell) for cell in cell_bboxes):
            raise ValueError(f"{label}作答框与涂卡格重叠，遮罩会抹去学生笔迹")

    # Column-major reading order; one run per (column, key).
    occurrences.sort(key=lambda item: (item["column"], item["order"]))
    runs: list[dict] = []
    for item in occurrences:
        last = runs[-1] if runs else None
        if last and last["answer_key"] == item["answer_key"] and last["column"] == item["column"]:
            if item["kind"] == "choice_mark":
                raise ValueError(f"第{item['answer_key']}题涂卡网格重复")
            last["items"].append(item)
            continue
        runs.append({"answer_key": item["answer_key"], "column": item["column"], "items": [item]})
    seen_runs = [(run["column"], run["answer_key"]) for run in runs]
    if len(set(seen_runs)) != len(seen_runs):
        raise ValueError("同一题在同一栏内被其他题隔开，阅读顺序无效")

    # A question continues only from the tail of column k to the head of k+1.
    groups: list[list[dict]] = []
    for run in runs:
        previous = groups[-1][-1] if groups else None
        if (
            previous is not None
            and previous["answer_key"] == run["answer_key"]
            and run["column"] == previous["column"] + 1
        ):
            groups[-1].append(run)
        else:
            groups.append([run])
    keys = [group[0]["answer_key"] for group in groups]
    for key in keys:
        if keys.count(key) != 1:
            raise ValueError(f"第{key}题跨栏不连续，只能从上一栏栏尾接到相邻下一栏栏首")

    ranks = [
        _key_rank(group[0]["items"][0]["question_no"], group[0]["items"][0]["subquestion_no"])
        for group in groups
    ]
    if all(rank is not None for rank in ranks):
        for (before, after), group in zip(zip(ranks, ranks[1:]), groups[1:]):
            if after <= before:
                raise ValueError(
                    f"第{group[0]['answer_key']}题的题号顺序与分栏阅读顺序不一致"
                )

    entries: list[dict] = []
    fields: list[dict] = []
    per_column: dict[int, int] = {}
    for group in groups:
        first = group[0]["items"][0]
        items = [item for run in group for item in run["items"]]
        kinds = {item["kind"] for item in items}
        if len(kinds) != 1:
            raise ValueError(f"第{first['answer_key']}题各作答框题型不一致")
        field = {
            "answer_key": first["answer_key"],
            "question_no": first["question_no"],
            "subquestion_no": first["subquestion_no"],
            "answer_kind": first["kind"],
            "columns": [run["column"] for run in group],
            "region_ids": list(dict.fromkeys(item["region_id"] for item in items)),
        }
        if first["kind"] == "choice_mark":
            field.update(
                source=SOURCE_BUBBLE,
                answer_state_ref={
                    "recognizer_version": RECOGNIZER_VERSION,
                    "answer_key": first["answer_key"],
                    "page_id": page_id,
                    "region_id": first["region_id"],
                },
            )
            fields.append(field)
            continue
        extras = [_stable_json(item["extras"]) for item in items]
        if len(set(extras)) != 1:
            raise ValueError(f"第{first['answer_key']}题各作答框的选项设置不一致")
        field.update(source=SOURCE_TRANSCRIPTION, entry_ids=[], **first["extras"])
        for run in group:
            per_column[run["column"]] = per_column.get(run["column"], 0) + 1
            entry_id = f"c{run['column']}-{per_column[run['column']]:02d}"
            entries.append({
                "entry_id": entry_id,
                "column": run["column"],
                "answer_key": first["answer_key"],
                "question_no": first["question_no"],
                "subquestion_no": first["subquestion_no"],
                "answer_kind": first["kind"],
                "regions": [item["region"] for item in run["items"]],
                **first["extras"],
            })
            field["entry_ids"].append(entry_id)
        fields.append(field)

    plan = {
        "contract_version": COLUMN_TRANSCRIPTION_CONTRACT_VERSION,
        "page_id": page_id,
        "page_size": [width, height],
        "columns": columns,
        "entries": entries,
        "fields": fields,
        "mask_boxes": [list(box) for box in sorted({tuple(box) for box in mask_boxes})],
    }
    plan["plan_sha256"] = _plan_digest(plan)
    return plan


# --- images ------------------------------------------------------------------

def masked_page(image_rgb: object, plan: Mapping[str, Any]) -> np.ndarray:
    """Copy of a registered page with every known bubble cell whitened."""
    plan = _checked_plan(plan)
    if (
        not isinstance(image_rgb, np.ndarray)
        or image_rgb.dtype != np.uint8
        or image_rgb.ndim != 3
        or image_rgb.shape[2] != 3
    ):
        raise ValueError("学生页面图像必须是 RGB uint8 数组")
    height, width = image_rgb.shape[:2]
    if [width, height] != list(plan["page_size"]):
        raise ValueError("学生页面尺寸与答题卡模板页面不一致，须先配准到模板坐标系")
    masked = image_rgb.copy()
    for left, top, right, bottom in plan["mask_boxes"]:
        masked[top:bottom, left:right] = MASK_GRAY
    return masked


def _crop(image: np.ndarray, box: Sequence[int]) -> np.ndarray:
    left, top, right, bottom = box
    return np.ascontiguousarray(image[top:bottom, left:right])


def render_column_images(image_rgb: object, plan: Mapping[str, Any]) -> list[np.ndarray]:
    """The N full-height column images of one page, bubbles masked, in column order."""
    page = masked_page(image_rgb, plan)
    return [_crop(page, column["pixel_box"]) for column in plan["columns"]]


def render_verification_images(
    image_rgb: object,
    plan: Mapping[str, Any],
    request: Mapping[str, Any],
) -> list[np.ndarray]:
    """Answer boxes of a verification request re-cropped from the masked page."""
    if request.get("plan_sha256") != _checked_plan(plan)["plan_sha256"]:
        raise ValueError("核对请求与分栏转录计划不一致")
    page = masked_page(image_rgb, plan)
    return [_crop(page, image["pixel_box"]) for image in request["images"]]


# --- prompts -----------------------------------------------------------------

_ROLE = "你是答题卡学生笔迹转录员。只转录，不解题、不判分、不评价对错。\n"

_EXCLUSIONS = """只抄写作答框内学生本人手写的内容；数学符号、字母、数字按图原样抄写，不改写、不化简、不纠正、不补全、不推断。
以下内容都不是学生答案，一律不得抄入：
1. 印刷内容：题号、题干、选项字母、"答题区"等提示文字、分值、页眉页脚、条形码和二维码、边框、横线和格线；
2. 涂卡格：选择题填涂格已按模板涂成白色，白色空白处不是学生作答，不要猜测其中原有内容；
3. 教师批注：对勾、叉、分数、圈画、下划线、评语等批改痕迹（通常是红色或其他彩色笔迹）；
4. 考生身份信息：姓名、学号、准考证号、班级。
作答框内没有学生笔迹：answer_state=blank，student_answer=""。
看不清，或分不清是学生笔迹、印刷内容还是教师批注：answer_state=unreadable，student_answer="[无法识别]"；只有局部看不清时在该处写 [无法识别]，其余照抄，answer_state=visible。
confidence 是 0 到 1 之间的数，表示这条转录与图中学生笔迹一致的把握。
"""

_PRIMARY_LAYOUT = """本次共 {count} 张图片，依次是同一页答题卡从左到右第 1 至第 {count} 栏的整栏图（第 i 张图片就是第 i 栏）。
entry_schema 列出每个待转录条目所在的栏号 column 和栏内作答框 answer_boxes_1000（以该栏图片的宽和高各为 0..1000）。
同一小问跨栏续写时，每个条目只抄本栏作答框内的部分，不要把其他栏的内容并入。
"""

_VERIFY_LAYOUT = """本次共 {count} 张图片，每张是答题卡上一个作答框的裁剪图。
check_schema 列出每个条目使用的图片序号 image_indexes（从 1 开始）；有多张时按顺序连起来就是该条目的完整作答（同一小问跨栏续写）。
"""

_PRIMARY_OUTPUT = """严格按 entry_ids 的顺序，每个条目恰好返回一次，不得增加、合并或省略条目。
严格只返回 JSON，不要 Markdown 或其他文字；每个条目只含以下四个字段：
{"entries":[{"entry_id":"c1-01","student_answer":"","answer_state":"blank|visible|unreadable","confidence":0.0}]}
"""

_VERIFY_OUTPUT = """严格按 check_ids 的顺序，每个条目恰好返回一次，不得增加、合并或省略条目。
严格只返回 JSON，不要 Markdown 或其他文字；每个条目只含以下四个字段：
{"checks":[{"check_id":"v01","student_answer":"","answer_state":"blank|visible|unreadable","confidence":0.0}]}
"""


def _schema_item(item: Mapping[str, Any]) -> dict[str, Any]:
    schema = {
        "question_no": item["question_no"],
        "subquestion_no": item["subquestion_no"],
        "answer_kind": item["answer_kind"],
    }
    if item["answer_kind"] == "choice_handwritten":
        schema["choice_mode"] = item["choice_mode"]
        schema["choice_options"] = list(item["choice_options"])
    return schema


def _max_tokens(kinds: Sequence[str]) -> int:
    return max(_MIN_TOKENS, min(_MAX_TOKENS, sum(_TOKENS_PER_KIND[kind] for kind in kinds)))


def build_transcription_request(plan: Mapping[str, Any]) -> dict[str, Any] | None:
    """The single request of one page: all N column images, every entry.

    ``None`` when the page has nothing to transcribe (only bubbles), so no
    call is made and nothing is invented for it.
    """
    plan = _checked_plan(plan)
    entries = plan["entries"]
    if not entries:
        return None
    count = len(plan["columns"])
    entry_ids = [entry["entry_id"] for entry in entries]
    schema = [
        {
            "entry_id": entry["entry_id"],
            "column": entry["column"],
            **_schema_item(entry),
            "answer_boxes_1000": [region["column_box_1000"] for region in entry["regions"]],
        }
        for entry in entries
    ]
    prompt = (
        _ROLE + _PRIMARY_LAYOUT.format(count=count) + _EXCLUSIONS + _PRIMARY_OUTPUT
        + "entry_ids=" + json.dumps(entry_ids, ensure_ascii=False, separators=(",", ":"))
        + "\nentry_schema=" + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    )
    return {
        "contract_version": COLUMN_TRANSCRIPTION_CONTRACT_VERSION,
        "stage": "primary",
        "page_id": plan["page_id"],
        "plan_sha256": plan["plan_sha256"],
        "image_count": count,
        "images": [
            {"image_index": index, "column": column["column"], "pixel_box": list(column["pixel_box"])}
            for index, column in enumerate(plan["columns"], 1)
        ],
        "entry_ids": entry_ids,
        "prompt": prompt,
        "prompt_sha256": _sha256(prompt.encode("utf-8")),
        "max_tokens": _max_tokens([entry["answer_kind"] for entry in entries]),
    }


# --- strict response parsing -------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?[ \t]*\n(.*?)\n?[ \t]*```", re.S)


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _value in pairs]
    if len(set(keys)) != len(keys):
        raise ColumnTranscriptionError("duplicate_json_key", "转录响应含重复字段")
    return dict(pairs)


def _load_json_object(raw_text: object) -> dict[str, Any]:
    if not isinstance(raw_text, str):
        raise ColumnTranscriptionError("response_not_text", "转录响应不是文本")
    text = raw_text.strip()
    fenced = _FENCE_RE.fullmatch(text)
    if fenced:
        text = fenced.group(1).strip()
    # Only normalize single-backslash LaTeX (\frac would otherwise decode to a
    # form feed); no extraction, repair or fallback of any kind.
    from services.analyzer import _sanitize_json_backslashes

    try:
        payload = json.JSONDecoder(strict=False, object_pairs_hook=_no_duplicate_keys).decode(
            _sanitize_json_backslashes(text)
        )
    except ColumnTranscriptionError:
        raise
    except ValueError as exc:
        raise ColumnTranscriptionError("response_not_json", "转录响应不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise ColumnTranscriptionError("response_not_object", "转录响应必须是 JSON 对象")
    return payload


def _validated_readings(
    raw_text: object,
    *,
    list_key: str,
    id_key: str,
    expected_ids: Sequence[str],
) -> list[dict[str, Any]]:
    payload = _load_json_object(raw_text)
    if set(payload) != {list_key} or not isinstance(payload[list_key], list):
        raise ColumnTranscriptionError("response_shape_invalid", f"转录响应只能包含 {list_key} 列表")
    items = payload[list_key]
    if len(items) != len(expected_ids):
        raise ColumnTranscriptionError("entry_count_mismatch", "转录响应条目数量与请求不一致")
    readings: list[dict[str, Any]] = []
    for position, (item, expected_id) in enumerate(zip(items, expected_ids), 1):
        if not isinstance(item, dict) or set(item) != {id_key, *_READING_FIELDS}:
            raise ColumnTranscriptionError("entry_fields_invalid", f"第{position}个转录条目字段不符合合同")
        if item[id_key] != expected_id:
            raise ColumnTranscriptionError("entry_order_mismatch", f"第{position}个转录条目编号或顺序与请求不一致")
        text = item["student_answer"]
        state = item["answer_state"]
        confidence = item["confidence"]
        if not isinstance(text, str) or state not in ANSWER_STATES:
            raise ColumnTranscriptionError("entry_value_invalid", f"条目 {expected_id} 的转录内容或状态无效")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence)
            or not 0.0 <= confidence <= 1.0
        ):
            raise ColumnTranscriptionError("confidence_invalid", f"条目 {expected_id} 的置信度无效")
        text = text.strip()
        if state == STATE_BLANK and text:
            raise ColumnTranscriptionError("state_text_conflict", f"条目 {expected_id} 标为空白却有内容")
        if state == STATE_UNREADABLE and text not in {"", UNREADABLE_TEXT}:
            raise ColumnTranscriptionError("state_text_conflict", f"条目 {expected_id} 标为无法识别却有内容")
        if state == STATE_VISIBLE and text in {"", UNREADABLE_TEXT}:
            raise ColumnTranscriptionError("state_text_conflict", f"条目 {expected_id} 标为有作答却没有可读内容")
        readings.append({
            "id": expected_id,
            "state": state,
            "student_answer": UNREADABLE_TEXT if state == STATE_UNREADABLE else text,
            "confidence": float(confidence),
        })
    return readings


def parse_transcription_response(raw_text: object, request: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Readings of a primary response, exactly the request's entries in order."""
    return _validated_readings(
        raw_text, list_key="entries", id_key="entry_id", expected_ids=request["entry_ids"],
    )


def parse_verification_response(raw_text: object, request: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Readings of a verification response, exactly the request's checks in order."""
    return _validated_readings(
        raw_text, list_key="checks", id_key="check_id", expected_ids=request["check_ids"],
    )


# --- merge (column fragments -> question number + small question) ------------

def choice_letters(text: str, options: Sequence[str], mode: str) -> str | None:
    """Canonical option letters of a handwritten choice, or ``None`` if not one."""
    raw = re.sub(r"^(?:答案\s*[：:]?\s*|选\s*)", "", str(text)).strip().rstrip(".。．")
    letters = re.sub(r"[\s,，、;；/]+", "", unicodedata.normalize("NFKC", raw).upper())
    if (
        not letters
        or any(letter not in options for letter in letters)
        or len(set(letters)) != len(letters)
        or (mode == "single" and len(letters) != 1)
    ):
        return None
    return "".join(sorted(letters, key=list(options).index))


def _bubble_field(field: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(field[key])
        for key in (
            "answer_key", "question_no", "subquestion_no", "answer_kind",
            "source", "columns", "region_ids", "answer_state_ref",
        )
    }


def merge_page_transcription(
    plan: Mapping[str, Any],
    request: Mapping[str, Any] | None,
    readings: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Join column entries per question number + small question, in column order.

    Bubble keys become references to the pixel recognizer.  A field is blank
    only if every fragment is blank and unreadable if any fragment is; the
    fragments stay attached as evidence.  Review reasons and verification
    needs are derived here, before anything reaches grading.
    """
    plan = _checked_plan(plan)
    entry_ids = [entry["entry_id"] for entry in plan["entries"]]
    if request is None:
        if entry_ids or readings:
            raise ValueError("该页有待转录条目，必须先完成转录请求")
    elif (
        request.get("plan_sha256") != plan["plan_sha256"]
        or list(request.get("entry_ids") or []) != entry_ids
        or [reading["id"] for reading in readings] != entry_ids
    ):
        raise ValueError("转录结果与分栏转录计划不一致")
    by_id = {reading["id"]: reading for reading in readings}
    column_of = {entry["entry_id"]: entry["column"] for entry in plan["entries"]}

    fields: list[dict[str, Any]] = []
    for field in plan["fields"]:
        if field["source"] == SOURCE_BUBBLE:
            fields.append(_bubble_field(field))
            continue
        fragments = [
            {
                "entry_id": entry_id,
                "column": column_of[entry_id],
                "state": by_id[entry_id]["state"],
                "student_answer": by_id[entry_id]["student_answer"],
                "confidence": by_id[entry_id]["confidence"],
            }
            for entry_id in field["entry_ids"]
        ]
        written = [fragment for fragment in fragments if fragment["state"] != STATE_BLANK]
        if not written:
            state, text = STATE_BLANK, ""
        elif any(fragment["state"] == STATE_UNREADABLE for fragment in written):
            state, text = STATE_UNREADABLE, UNREADABLE_TEXT
        else:
            state, text = STATE_VISIBLE, "\n".join(fragment["student_answer"] for fragment in written)
        merged: dict[str, Any] = {
            **{key: copy.deepcopy(field[key]) for key in (
                "answer_key", "question_no", "subquestion_no", "answer_kind",
                "source", "columns", "region_ids", "entry_ids",
            )},
            "state": state,
            "student_answer": text,
            "confidence": min(fragment["confidence"] for fragment in fragments),
            "fragments": fragments,
        }
        review: list[str] = []
        if field["answer_kind"] == "choice_handwritten":
            merged["choice_mode"] = field["choice_mode"]
            merged["choice_options"] = list(field["choice_options"])
            if state == STATE_VISIBLE:
                letters = choice_letters(text, field["choice_options"], field["choice_mode"])
                if letters is None:
                    review.append("choice_transcription_invalid")
                else:
                    merged["choice_letters"] = letters
        if state == STATE_BLANK:
            review.append("blank_requires_teacher_confirmation")
        elif state == STATE_UNREADABLE:
            review.append("unreadable_requires_teacher_confirmation")
        elif UNREADABLE_TEXT in text:
            review.append("partially_unreadable")
        risk: list[str] = []
        if not review:
            if merged["confidence"] < VERIFY_CONFIDENCE_FLOOR:
                risk.append("low_confidence")
            if field["answer_kind"] in {"fill", "choice_handwritten"}:
                risk.append("decisive_short_answer")
            if len(field["columns"]) > 1:
                risk.append("cross_column_join")
        merged["review_reasons"] = review
        merged["high_risk_reasons"] = risk
        merged["verification"] = {"status": VERIFY_PENDING if risk else VERIFY_NOT_REQUIRED}
        fields.append(merged)
    return fields


# --- verification stage ------------------------------------------------------

def build_verification_requests(
    plan: Mapping[str, Any],
    fields: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Blind re-read requests for every field whose verification is pending.

    Each field's answer boxes are re-cropped (one image per box, column
    order); fields are packed in page order into requests of at most
    ``MAX_IMAGES_PER_REQUEST`` images without splitting a field.  The prompt
    never contains the first reading.
    """
    plan = _checked_plan(plan)
    entries = {entry["entry_id"]: entry for entry in plan["entries"]}
    pending = [
        field for field in fields
        if field.get("source") == SOURCE_TRANSCRIPTION
        and field.get("verification", {}).get("status") == VERIFY_PENDING
    ]
    batches: list[list[tuple[dict, list[dict]]]] = []
    image_total = 0
    for field in pending:
        regions = [region for entry_id in field["entry_ids"] for region in entries[entry_id]["regions"]]
        if len(regions) > MAX_IMAGES_PER_REQUEST:
            raise ValueError(f"第{field['answer_key']}题作答框过多，无法一次核对")
        if not batches or image_total + len(regions) > MAX_IMAGES_PER_REQUEST:
            batches.append([])
            image_total = 0
        batches[-1].append((field, regions))
        image_total += len(regions)

    requests: list[dict[str, Any]] = []
    check_number = 0
    for request_index, batch in enumerate(batches, 1):
        images: list[dict] = []
        checks: list[dict] = []
        for field, regions in batch:
            check_number += 1
            check_id = f"v{check_number:02d}"
            indexes = []
            for region in regions:
                images.append({
                    "image_index": len(images) + 1,
                    "check_id": check_id,
                    "region_id": region["region_id"],
                    "pixel_box": list(region["pixel_box"]),
                })
                indexes.append(len(images))
            checks.append({"check_id": check_id, "answer_key": field["answer_key"], "image_indexes": indexes})
        schema = [
            {
                "check_id": check["check_id"],
                **_schema_item(field),
                "image_indexes": check["image_indexes"],
            }
            for check, (field, _regions) in zip(checks, batch)
        ]
        check_ids = [check["check_id"] for check in checks]
        prompt = (
            _ROLE + _VERIFY_LAYOUT.format(count=len(images)) + _EXCLUSIONS + _VERIFY_OUTPUT
            + "check_ids=" + json.dumps(check_ids, ensure_ascii=False, separators=(",", ":"))
            + "\ncheck_schema=" + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        )
        requests.append({
            "contract_version": VERIFICATION_CONTRACT_VERSION,
            "stage": "verification",
            "request_index": request_index,
            "page_id": plan["page_id"],
            "plan_sha256": plan["plan_sha256"],
            "image_count": len(images),
            "images": images,
            "checks": checks,
            "check_ids": check_ids,
            "prompt": prompt,
            "prompt_sha256": _sha256(prompt.encode("utf-8")),
            "max_tokens": _max_tokens([field["answer_kind"] for field, _regions in batch]),
        })
    return requests


def _canonical_text(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).split())


_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def _compare_readings(field: Mapping[str, Any], reading: Mapping[str, Any]) -> dict[str, Any]:
    """How the blind second reading compares with the first (calibration above).

    Returns ``{"match": "exact" | "fuzzy" | "state_only"}`` on agreement,
    otherwise ``{"mismatch": reason}``; ``similarity`` is added whenever the
    fuzzy rule was evaluated.
    """
    if field["state"] != reading["state"]:
        return {"mismatch": "state_mismatch"}
    if field["state"] != STATE_VISIBLE:
        # blank == blank, unreadable == unreadable: nothing else to compare.
        return {"match": "state_only"}
    if field["answer_kind"] == "choice_handwritten":
        second = choice_letters(reading["student_answer"], field["choice_options"], field["choice_mode"])
        if second is not None and second == field.get("choice_letters"):
            return {"match": "exact"}
        return {"mismatch": "text_mismatch"}
    first_text = _canonical_text(field["student_answer"])
    second_text = _canonical_text(reading["student_answer"])
    if first_text == second_text:
        return {"match": "exact"}
    if (
        field["answer_kind"] not in LONG_TEXT_KINDS
        or min(len(first_text), len(second_text)) < LONG_TEXT_MIN_CHARS
    ):
        return {"mismatch": "text_mismatch"}
    similarity = round(difflib.SequenceMatcher(None, first_text, second_text, autojunk=False).ratio(), 4)
    if _NUMBER_RE.findall(first_text) != _NUMBER_RE.findall(second_text):
        return {"mismatch": "number_mismatch", "similarity": similarity}
    if similarity < LONG_TEXT_MIN_SIMILARITY:
        return {"mismatch": "similarity_below_floor", "similarity": similarity}
    return {"match": "fuzzy", "similarity": similarity}


def apply_verification(
    fields: Sequence[Mapping[str, Any]],
    request: Mapping[str, Any],
    readings: Sequence[Mapping[str, Any]] | None,
    *,
    failure_reason: str = "",
) -> list[dict[str, Any]]:
    """Settle the request's pending checks; returns updated copies of ``fields``.

    Agreement (same state; same canonical text or option letters, or the
    calibrated fuzzy rule for long solution text) confirms.  Otherwise the
    field is disputed and goes to the teacher with the second reading beside
    the first.  ``readings=None`` records a failed check (call error or
    malformed response): the field goes to the teacher too.  The first
    reading is never changed.
    """
    updated = [copy.deepcopy(dict(field)) for field in fields]
    by_key = {field["answer_key"]: field for field in updated}
    if readings is not None and [reading["id"] for reading in readings] != list(request["check_ids"]):
        raise ValueError("核对结果与核对请求不一致")
    reading_by_id = {reading["id"]: reading for reading in readings or []}
    for check in request["checks"]:
        field = by_key.get(check["answer_key"])
        if field is None or field.get("verification", {}).get("status") != VERIFY_PENDING:
            raise ValueError(f"第{check['answer_key']}题没有待核对的转录")
        verification: dict[str, Any] = {
            "contract_version": VERIFICATION_CONTRACT_VERSION,
            "request_index": request["request_index"],
            "check_id": check["check_id"],
        }
        if readings is None:
            verification.update(status=VERIFY_FAILED, reason_code=failure_reason or "verification_failed")
            field["review_reasons"].append("verification_failed")
        else:
            reading = reading_by_id[check["check_id"]]
            comparison = _compare_readings(field, reading)
            verification["second_reading"] = {
                "state": reading["state"],
                "student_answer": reading["student_answer"],
                "confidence": reading["confidence"],
            }
            if "similarity" in comparison:
                verification["similarity"] = comparison["similarity"]
            if "mismatch" not in comparison:
                verification.update(status=VERIFY_CONFIRMED, match=comparison["match"])
            else:
                verification.update(status=VERIFY_DISPUTED, mismatch=comparison["mismatch"])
                field["review_reasons"].append("verification_disputed")
        field["verification"] = verification
    return updated


def fail_pending_verification(fields: Sequence[Mapping[str, Any]], reason_code: str) -> list[dict[str, Any]]:
    """Mark every still-pending check as failed (teacher review), never as passed."""
    updated = [copy.deepcopy(dict(field)) for field in fields]
    for field in updated:
        if field.get("verification", {}).get("status") == VERIFY_PENDING:
            field["verification"] = {
                "contract_version": VERIFICATION_CONTRACT_VERSION,
                "status": VERIFY_FAILED,
                "reason_code": reason_code,
            }
            field["review_reasons"].append("verification_failed")
    return updated


# --- gates ---------------------------------------------------------------------

def page_transcription_gate(record: Mapping[str, Any]) -> dict[str, Any]:
    """Whether one page record may be handed to the (separate) grading stage."""
    blocking: list[str] = []
    status = record.get("status") if isinstance(record, Mapping) else None
    if status == RECORD_FAILED:
        blocking.append("transcription_failed")
    elif status not in {RECORD_TRANSCRIBED, RECORD_NOT_REQUIRED}:
        blocking.append("transcription_missing")
    fields = record.get("fields") if isinstance(record, Mapping) else None
    fields = fields if isinstance(fields, list) else []
    pending = [
        field["answer_key"] for field in fields
        if field.get("source") == SOURCE_TRANSCRIPTION
        and field.get("verification", {}).get("status") == VERIFY_PENDING
    ]
    if pending:
        blocking.append("verification_incomplete")
    return {
        "ready_for_grading": not blocking,
        "blocking_reasons": blocking,
        "pending_verification": pending,
        "teacher_review_keys": [field["answer_key"] for field in fields if field.get("review_reasons")],
    }


# --- evidence ------------------------------------------------------------------

EVIDENCE_DIRNAME = "transcription_evidence"
_SAFE_SEGMENT_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_EVIDENCE_NAME_RE = re.compile(
    r"(?:column-\d{2}\.png|verify-\d{2}-\d{2}\.png|record\.json"
    r"|request-(?:primary|verify-\d{2})\.json|response-(?:primary|verify-\d{2})\.txt)\Z"
)


class TranscriptionEvidence:
    """Write-once evidence of one page transcription attempt.

    Lives under ``<batch dir>/transcription_evidence/<paper>/<page>/<attempt>``
    so the batch's authenticated asset access and raw-image retention cover
    it.  Records keep only this relative directory and file names + sha256.
    """

    def __init__(
        self,
        session_dir: str | os.PathLike[str],
        paper_id: str,
        page_id: str,
        attempt_id: str | None = None,
    ):
        attempt = attempt_id or uuid.uuid4().hex
        for value in (paper_id, page_id, attempt):
            if not isinstance(value, str) or not _SAFE_SEGMENT_RE.fullmatch(value):
                raise ValueError("转录证据标识无效")
        self.session_dir = Path(session_dir).resolve()
        self.directory = f"{EVIDENCE_DIRNAME}/{paper_id}/{page_id}/{attempt}"
        self.root = self.session_dir / EVIDENCE_DIRNAME / paper_id / page_id / attempt
        self.files: list[dict[str, Any]] = []

    def path(self, name: str) -> Path:
        if not isinstance(name, str) or not _EVIDENCE_NAME_RE.fullmatch(name):
            raise ValueError("转录证据文件名无效")
        return self.root / name

    def _write(self, name: str, data: bytes, kind: str, **extra: Any) -> dict[str, Any]:
        target = self.path(name)
        self.root.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise FileExistsError(f"转录证据 {name} 已存在")
        temporary = self.root / f".{name}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temporary, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        entry = {"file": name, "kind": kind, "sha256": _sha256(data), "bytes": len(data), **extra}
        if name != "record.json":
            self.files.append(entry)
        return entry

    def write_image(self, name: str, image: np.ndarray, kind: str, **extra: Any) -> dict[str, Any]:
        from PIL import Image

        buffer = io.BytesIO()
        with Image.fromarray(np.ascontiguousarray(image)) as encoded:
            encoded.save(buffer, format="PNG")
        height, width = image.shape[:2]
        return self._write(name, buffer.getvalue(), kind, width=width, height=height, **extra)

    def write_text(self, name: str, text: str, kind: str) -> dict[str, Any]:
        return self._write(name, text.encode("utf-8"), kind)

    def write_json(self, name: str, payload: object, kind: str) -> dict[str, Any]:
        return self._write(name, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"), kind)

    def manifest(self) -> dict[str, Any]:
        return {"directory": self.directory, "files": copy.deepcopy(self.files)}


def read_evidence_file(
    session_dir: str | os.PathLike[str],
    record: Mapping[str, Any],
    name: str,
) -> bytes:
    """Bytes of one evidence file named by ``record``, integrity-checked.

    For an authenticated teacher view: only files listed in the record, only
    inside the batch directory, and only while the bytes still match.
    """
    evidence = record.get("evidence") if isinstance(record, Mapping) else None
    directory = evidence.get("directory") if isinstance(evidence, Mapping) else None
    parts = directory.split("/") if isinstance(directory, str) else []
    if (
        len(parts) != 4
        or parts[0] != EVIDENCE_DIRNAME
        or any(not _SAFE_SEGMENT_RE.fullmatch(part) for part in parts[1:])
        or not isinstance(name, str)
        or not _EVIDENCE_NAME_RE.fullmatch(name)
    ):
        raise ValueError("转录证据路径无效")
    entry = next(
        (item for item in evidence.get("files") or [] if isinstance(item, Mapping) and item.get("file") == name),
        None,
    )
    if entry is None:
        raise ValueError("该文件不是本次转录的证据")
    root = Path(session_dir).resolve()
    path = root.joinpath(*parts, name).resolve()
    path.relative_to(root)
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise ValueError("转录证据已按保留策略清理或缺失") from exc
    if _sha256(data) != entry.get("sha256"):
        raise ValueError("转录证据完整性校验失败")
    return data


# --- orchestration ---------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _finish(record: dict[str, Any], evidence: TranscriptionEvidence) -> dict[str, Any]:
    record["evidence"] = evidence.manifest()
    record["created_at"] = _now()
    evidence.write_json("record.json", record, kind="record")
    return record


async def transcribe_page_columns(
    image_rgb: np.ndarray,
    plan: Mapping[str, Any],
    evidence: TranscriptionEvidence,
    call_images: VisionImagesCall,
) -> dict[str, Any]:
    """Transcribe one registered student page: primary call, then verification.

    ``call_images(paths, prompt, max_tokens=...)`` sends the ordered images of
    ONE request and returns the raw model text.  A failed or malformed primary
    response yields a ``failed`` record with no fields at all -- never blank
    defaults.  Evidence (images exactly as sent, requests, raw responses and
    the record) is written as it happens.
    """
    plan = _checked_plan(plan)
    record: dict[str, Any] = {
        "contract_version": COLUMN_TRANSCRIPTION_CONTRACT_VERSION,
        "page_id": plan["page_id"],
        "plan_sha256": plan["plan_sha256"],
        "status": RECORD_FAILED,
        "fields": [],
        "failure": None,
    }
    request = build_transcription_request(plan)
    if request is None:
        masked_page(image_rgb, plan)
        record.update(status=RECORD_NOT_REQUIRED, fields=merge_page_transcription(plan, None, []))
        return _finish(record, evidence)

    paths = []
    for index, image in enumerate(render_column_images(image_rgb, plan), 1):
        name = f"column-{index:02d}.png"
        evidence.write_image(name, image, kind="column_image", column=index)
        paths.append(str(evidence.path(name)))
    evidence.write_json("request-primary.json", request, kind="primary_request")
    try:
        raw = await call_images(paths, request["prompt"], max_tokens=request["max_tokens"])
    except Exception as exc:
        record["failure"] = {"stage": "primary", "reason_code": "call_failed", "error_type": type(exc).__name__}
        return _finish(record, evidence)
    if isinstance(raw, str):
        evidence.write_text("response-primary.txt", raw, kind="primary_response")
    try:
        readings = parse_transcription_response(raw, request)
    except ColumnTranscriptionError as exc:
        record["failure"] = {"stage": "primary", "reason_code": exc.reason_code, "message": str(exc)}
        return _finish(record, evidence)
    fields = merge_page_transcription(plan, request, readings)

    try:
        verification_requests = build_verification_requests(plan, fields)
    except ValueError:
        verification_requests = []
        fields = fail_pending_verification(fields, "verification_plan_invalid")
    for verification in verification_requests:
        number = verification["request_index"]
        verify_paths = []
        crops = render_verification_images(image_rgb, plan, verification)
        for index, crop in enumerate(crops, 1):
            name = f"verify-{number:02d}-{index:02d}.png"
            evidence.write_image(name, crop, kind="verification_image", request_index=number)
            verify_paths.append(str(evidence.path(name)))
        evidence.write_json(f"request-verify-{number:02d}.json", verification, kind="verification_request")
        try:
            raw = await call_images(verify_paths, verification["prompt"], max_tokens=verification["max_tokens"])
        except Exception:
            fields = apply_verification(fields, verification, None, failure_reason="call_failed")
            continue
        if isinstance(raw, str):
            evidence.write_text(f"response-verify-{number:02d}.txt", raw, kind="verification_response")
        try:
            second = parse_verification_response(raw, verification)
        except ColumnTranscriptionError as exc:
            fields = apply_verification(fields, verification, None, failure_reason=exc.reason_code)
            continue
        fields = apply_verification(fields, verification, second)
    record.update(status=RECORD_TRANSCRIBED, fields=fields)
    return _finish(record, evidence)
