"""What the layout model is asked, and how its answer is read.

- ``_PROMPT`` asks the model only for the column boundaries of one rectified
  template page, with ``_DOUBLE_LAYOUT_EXAMPLE`` as the example answer.
  ``_DOUBLE_PROMPT`` and ``_TRIPLE_PROMPT`` pin the count when the teacher has
  already chosen two or three columns (the triple one shows
  ``_TRIPLE_LAYOUT_EXAMPLE``).
- ``_extract_json_text`` finds the JSON object in the model's message;
  ``_strict_payload`` accepts it only if it matches the contract (coordinate
  space, column count, contiguous boundaries, crossing evidence) and returns the
  layout payload.

Moved unchanged from services/template_column_layout.py, which still exposes these names.
"""

from __future__ import annotations

import json
from typing import Any

from services.template_column_layout_contract import (
    LAYOUT_COORDINATE_SPACE,
    TemplateCutError,
    _integer_json_number,
    _layout_columns_from_value,
    _payload_from_columns,
    _validate_crossing_evidence,
)


_DOUBLE_LAYOUT_EXAMPLE = (
    '{"column_count":2,"columns":[{"column_index":1,"x1_1000":0,"x2_1000":500},'
    '{"column_index":2,"x1_1000":500,"x2_1000":1000}]}'
)
_TRIPLE_LAYOUT_EXAMPLE = (
    '{"column_count":3,"columns":[{"column_index":1,"x1_1000":0,"x2_1000":333},'
    '{"column_index":2,"x1_1000":333,"x2_1000":667},'
    '{"column_index":3,"x1_1000":667,"x2_1000":1000}]}'
)
_PROMPT = (
    """你是试卷模板版面分栏模型。
输入是一张已经完成旋转、透视和页面边界矫正的完整试卷模板。只按服务端提供的栏数要求确定分界；如果服务端没有指定栏数，才判断整页需要单栏、双栏还是三栏。不要识别题干、答案或学生笔迹。
服务端指定的栏数是硬约束，返回的 column_count 必须与其一致；页眉或标题跨栏不算一栏。分界线不得穿过题目或作答区域。
这个分栏结果会由系统冻结，并复用于同一页型的所有学生试卷，所以必须依据当前图片实际排版，不能按固定的 500/333 像素猜测。
坐标为整图横向 0..1000。各栏必须从左到右连续、无空隙、无重叠，并完整覆盖 0..1000。
只返回 JSON，不要解释或 Markdown：
"""
    + _DOUBLE_LAYOUT_EXAMPLE
)

# An explicit triple choice must not leave a two-column example or a generic
# "choose the minimum" branch for the model to resolve.  Derive the fixed
# contract from the compatible general prompt so future common wording stays
# aligned without duplicating the full instruction block.
_TRIPLE_PROMPT = (
    _PROMPT
    .replace(
        "如果服务端没有指定栏数，才判断整页需要单栏、双栏还是三栏",
        "教师已经选择三栏；不要自行选择栏数，必须返回恰好三栏和两条实际边界",
    )
    .replace(_DOUBLE_LAYOUT_EXAMPLE, _TRIPLE_LAYOUT_EXAMPLE)
)

# An explicit double choice pins the count the same way the triple contract
# does, so the model cannot drift into an invented column count.
_DOUBLE_PROMPT = (
    _PROMPT
    .replace(
        "如果服务端没有指定栏数，才判断整页需要单栏、双栏还是三栏",
        "教师已经选择双栏；不要自行选择栏数，必须返回恰好两栏和一条实际边界",
    )
)


def _extract_json_text(message: object) -> str:
    if not isinstance(message, dict):
        return ""
    fields = [message.get("content"), message.get("reasoning_content")]
    texts = [value.strip() for value in fields if isinstance(value, str) and value.strip()]
    for text in texts:
        if text.startswith("{"):
            return text
    for text in texts:
        decoder = json.JSONDecoder()
        for index, char in enumerate(text):
            if char != "{":
                continue
            try:
                value, _end = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return json.dumps(value, ensure_ascii=False)
    return texts[0] if texts else ""


def _strict_payload(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise TemplateCutError("PROVIDER_DATA_INVALID", "分栏返回结构无效")
    coordinate_space = raw.get("coordinate_space")
    if coordinate_space is not None and str(coordinate_space) != LAYOUT_COORDINATE_SPACE:
        raise TemplateCutError("PROVIDER_DATA_INVALID", "分栏坐标合同无效")
    count = _integer_json_number(raw.get("column_count"))
    columns = raw.get("columns")
    if columns is None:
        compact_boundaries = raw.get("boundaries", raw.get("x_boundaries"))
        if isinstance(compact_boundaries, list):
            boundaries = [_integer_json_number(item) for item in compact_boundaries]
            if all(item is not None for item in boundaries):
                columns = [0, *boundaries, 1000]
    if (
        count is None
        or not 1 <= count <= 3
        or not isinstance(columns, list)
        or len(columns) != count
    ):
        raise TemplateCutError("PROVIDER_DATA_INVALID", "分栏数量无效")

    try:
        normalized = _layout_columns_from_value(columns, allow_implicit_index=False)
    except TemplateCutError as exc:
        # Preserve the legacy provider error namespace and message while
        # retaining the stricter contiguous/minimum-width checks.
        message = str(exc)
        if "宽度过窄" in message:
            message = "分栏宽度过窄"
        elif "覆盖" in message or "连续" in message:
            message = "分栏边界无效"
        else:
            message = "分栏结构无效"
        raise TemplateCutError("PROVIDER_DATA_INVALID", message) from exc
    if len(normalized) != count:
        raise TemplateCutError("PROVIDER_DATA_INVALID", "分栏数量无效")
    try:
        _validate_crossing_evidence(normalized, raw)
    except TemplateCutError as exc:
        raise TemplateCutError("PROVIDER_DATA_INVALID", str(exc)) from exc
    return _payload_from_columns(
        normalized,
        source_key="model_column_layout",
        trust="model_coarse",
    )
