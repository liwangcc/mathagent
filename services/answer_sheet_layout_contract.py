"""Answer-sheet layout contract: choice bubble grids and question-number join.

The answer-sheet template is the layout layer.  Its regions carry
``question_no`` / ``subquestion_no`` and are joined mechanically to the paper
template by those numbers.  Answer sheets are standardized, so the numbers are
trusted keys: there is no signature gate.  The join fails closed only for a
required question without a region, a region whose number is not in the paper
template, and question/sub-question conflicts.

Bubble grids record where each option cell of a ``choice_mark`` region sits,
one grid row per answer key: the whole question (``subquestion_no=""``), or one
row per small question when the paper template declares them (``1(1)``,
``1(2)`` ... inside the question's single ``choice_mark`` region).

A model pass may only *propose* where a grid is (``state="proposed"``, bbox
only).  A row becomes usable only after the teacher explicitly confirms or
adjusts it with concrete option cells; the server stamps who confirmed and
when.  Confirmed rows are frozen into the answer-sheet template, never into a
grading session, and are resolved from there to pixel boxes for the local
mark comparison.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

BUBBLE_GRID_CONTRACT_VERSION = "answer-sheet-bubble-grid-v1"
GRID_ORDERS = {"left_to_right", "top_to_bottom"}
GRID_PROPOSED = "proposed"
GRID_CONFIRMED = "confirmed"
SOURCE_MODEL_PROPOSAL = "model_proposal"
SOURCE_TEACHER_DRAWN = "teacher_drawn"
MIN_GRID_CELLS = 2
MAX_GRID_CELLS = 8
MIN_CELL_EXTENT = 3
MAX_SUBQUESTION_ROWS = 20
_OPTION_LETTERS = "ABCDEFGH"
_REGION_QUESTION_TYPES = {
    "choice_mark": "choice",
    "choice_handwritten": "choice",
    "fill": "fill",
    "solve": "solve",
}
# A proposal is model output; it can never carry the teacher's decision.
_CONFIRMATION_FIELDS = (
    "state", "teacher_confirmed", "confirmed_by_teacher_id", "confirmed_at", "cells",
)
_FROZEN_ROW_FIELDS = {
    "question_no", "subquestion_no", "page_id", "region_id", "allow_multiple",
    "grid_order", "grid_bbox", "cells", "teacher_confirmed",
}


def _int_bbox(value: object, label: str) -> list[int]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError(f"{label}坐标必须是四个整数")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        raise ValueError(f"{label}坐标必须是四个整数")
    left, top, right, bottom = value
    if not (0 <= left < right <= 1000 and 0 <= top < bottom <= 1000):
        raise ValueError(f"{label}坐标越界或面积无效")
    return [left, top, right, bottom]


def _inside(inner: list[int], outer: list[int]) -> bool:
    return (
        outer[0] <= inner[0] and outer[1] <= inner[1]
        and inner[2] <= outer[2] and inner[3] <= outer[3]
    )


def _overlaps(a: list[int], b: list[int]) -> bool:
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def _union(boxes: list[list[int]]) -> list[int]:
    return [
        min(box[0] for box in boxes), min(box[1] for box in boxes),
        max(box[2] for box in boxes), max(box[3] for box in boxes),
    ]


def normalize_choice_options(value: object) -> list[str]:
    raw = value if isinstance(value, (list, tuple)) and value else list("ABCD")
    options = [str(item).strip().upper() for item in raw]
    if (
        not MIN_GRID_CELLS <= len(options) <= MAX_GRID_CELLS
        or len(set(options)) != len(options)
        or any(option not in _OPTION_LETTERS or len(option) != 1 for option in options)
    ):
        raise ValueError("选择题选项必须是 2–8 个不重复的字母 A–H")
    return options


def answer_key(question_no: object, subquestion_no: object = "") -> str:
    """Teacher-facing key: ``"3"`` for a whole question, ``"1(2)"`` for a small one."""
    question = str(question_no or "").strip()
    sub = str(subquestion_no or "").strip()
    return f"{question}({sub})" if sub else question


def _row_key(row: dict[str, Any]) -> tuple[str, str]:
    return (
        str(row.get("question_no") or "").strip(),
        str(row.get("subquestion_no") or "").strip(),
    )


def choice_row_keys(spec: dict[str, Any]) -> list[str]:
    """Grid rows a choice question needs from its paper-template structure.

    ``[""]`` is the whole question; a declared ``subquestion_count`` of n > 1
    gives each small question its own row ``"1"`` .. ``"n"``.
    """
    raw = spec.get("subquestion_count")
    if isinstance(raw, bool):
        raise ValueError(f"第{spec.get('question_no')}题小问数量无效")
    try:
        count = 1 if raw is None or raw == "" else int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"第{spec.get('question_no')}题小问数量无效") from exc
    if not 1 <= count <= MAX_SUBQUESTION_ROWS:
        raise ValueError(f"第{spec.get('question_no')}题小问数量无效")
    return [str(index) for index in range(1, count + 1)] if count > 1 else [""]


def _choice_mark_regions(answer_regions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    regions: dict[str, dict[str, Any]] = {}
    for region in answer_regions:
        if region.get("region_type") != "choice_mark":
            continue
        question_no = str(region.get("question_no") or "").strip()
        if question_no in regions:
            raise ValueError(f"第{question_no}题只能有一个涂卡区域")
        regions[question_no] = region
    return regions


def _is_confirmed(row: dict[str, Any]) -> bool:
    if "state" in row:
        # Draft rows: the state alone is not enough without the stamp of the
        # teacher who confirmed it.
        return (
            row.get("state") == GRID_CONFIRMED
            and isinstance(row.get("confirmed_by_teacher_id"), int)
            and not isinstance(row.get("confirmed_by_teacher_id"), bool)
            and bool(row.get("confirmed_at"))
        )
    # Frozen template rows.  Anything else (e.g. a bare client-claimed
    # ``teacher_confirmed`` flag) is not a confirmation.
    return set(row) == _FROZEN_ROW_FIELDS and row.get("teacher_confirmed") is True


def _validated_cells(
    raw_cells: object,
    *,
    label: str,
    region: dict[str, Any],
    grid_order: str,
) -> list[dict[str, Any]]:
    """Option cells inside the region, exactly one per option, in print order."""
    if not isinstance(raw_cells, list):
        raise ValueError(f"{label}涂卡网格缺少选项格")
    options = normalize_choice_options(region.get("choice_options"))
    region_bbox = region["bbox"]
    cells: list[dict[str, Any]] = []
    for raw_cell in raw_cells:
        if not isinstance(raw_cell, dict):
            raise ValueError(f"{label}涂卡格式错误")
        option = str(raw_cell.get("option") or "").strip().upper()
        bbox = _int_bbox(raw_cell.get("bbox"), f"{label}{option}选项格")
        if not _inside(bbox, region_bbox):
            raise ValueError(f"{label}{option}选项格必须位于涂卡区域内")
        width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if width < MIN_CELL_EXTENT or height < MIN_CELL_EXTENT:
            raise ValueError(f"{label}{option}选项格过小")
        cells.append({"option": option, "bbox": bbox, "cell_size": [width, height]})
    if sorted(cell["option"] for cell in cells) != sorted(options):
        raise ValueError(f"{label}涂卡格必须恰好覆盖选项 {''.join(options)}")
    for index, first in enumerate(cells):
        for second in cells[index + 1:]:
            if _overlaps(first["bbox"], second["bbox"]):
                raise ValueError(f"{label}选项格 {first['option']}/{second['option']} 重叠")
    if grid_order not in GRID_ORDERS:
        raise ValueError(f"{label}涂卡网格排列方向无效")
    axis = 0 if grid_order == "left_to_right" else 1
    cells.sort(key=lambda cell: cell["bbox"][axis] + cell["bbox"][axis + 2])
    if [cell["option"] for cell in cells] != options:
        # A label swap would silently move the answer cell; fail closed.
        raise ValueError(f"{label}选项格排列顺序必须与选项 {''.join(options)} 一致")
    return cells


def _check_region_rows(rows: list[dict[str, Any]]) -> None:
    """Rows sharing one region may not overlap or mix whole and small questions."""
    by_region: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_region.setdefault(str(row.get("region_id")), []).append(row)
    for region_rows in by_region.values():
        subs = [str(row.get("subquestion_no") or "") for row in region_rows]
        if "" in subs and any(subs):
            raise ValueError(
                f"第{region_rows[0].get('question_no')}题整题涂卡行与小问涂卡行不能同时存在"
            )
        placed = [row for row in region_rows if row.get("cells")]
        for index, first in enumerate(placed):
            for second in placed[index + 1:]:
                if _overlaps(first["grid_bbox"], second["grid_bbox"]):
                    raise ValueError(
                        f"第{answer_key(*_row_key(first))}题与第{answer_key(*_row_key(second))}题涂卡行重叠"
                    )


def _resolve_draft_target(
    raw: dict[str, Any],
    choice_regions: dict[str, dict[str, Any]],
    specs: dict[str, dict[str, Any]],
) -> tuple[tuple[str, str], dict[str, Any], str]:
    """Return (key, region, label) for a teacher/model row request."""
    question_no = str(raw.get("question_no") or "").strip()
    subquestion_no = str(raw.get("subquestion_no") or "").strip()
    label = f"第{answer_key(question_no, subquestion_no) or '?'}题"
    region = choice_regions.get(question_no)
    if region is None:
        raise ValueError(f"{label}没有对应的涂卡区域，不能标注涂卡网格")
    spec = specs.get(question_no)
    if spec is None or str(spec.get("question_type") or "") != "choice":
        raise ValueError(f"{label}不是试卷模板中的选择题")
    if subquestion_no not in choice_row_keys(spec):
        raise ValueError(f"{label}不在试卷模板的小问结构中")
    return (question_no, subquestion_no), region, label


def _order_rows(
    rows: dict[tuple[str, str], dict[str, Any]],
    answer_regions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Canonical order: region order on the sheet, then small-question number."""
    region_index = {
        str(region.get("region_id")): index for index, region in enumerate(answer_regions)
    }

    def sort_key(row: dict[str, Any]) -> tuple[int, int]:
        sub = str(row.get("subquestion_no") or "")
        return (
            region_index.get(str(row.get("region_id")), len(region_index)),
            int(sub) if sub.isdigit() else 0,
        )

    return sorted(rows.values(), key=sort_key)


def propose_grid_rows(
    rows: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
    *,
    answer_regions: list[dict[str, Any]],
    question_specs: list[dict[str, Any]],
    proposer: dict[str, Any],
    now: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Record model placement proposals (grid bbox only, never confirmed).

    A teacher-confirmed row is never overwritten by a proposal; its key is
    returned in the skipped list instead.
    """
    if not isinstance(proposals, list):
        raise ValueError("涂卡网格提议格式错误")
    choice_regions = _choice_mark_regions(answer_regions)
    specs = {str(item.get("question_no") or "").strip(): item for item in question_specs}
    by_key = {_row_key(row): row for row in rows}
    skipped: list[str] = []
    seen: set[tuple[str, str]] = set()
    proposer_record = {
        "contract_version": str(proposer.get("contract_version") or ""),
        "provider_route": str(proposer.get("provider_route") or ""),
    }
    for raw in proposals:
        if not isinstance(raw, dict):
            raise ValueError("涂卡网格提议格式错误")
        if any(field in raw for field in _CONFIRMATION_FIELDS):
            raise ValueError("模型提议不能携带教师确认信息或选项格")
        key, region, label = _resolve_draft_target(raw, choice_regions, specs)
        if key in seen:
            raise ValueError(f"{label}涂卡网格提议重复")
        seen.add(key)
        grid_bbox = _int_bbox(raw.get("grid_bbox"), f"{label}涂卡网格提议")
        if not _inside(grid_bbox, region["bbox"]):
            raise ValueError(f"{label}涂卡网格提议必须位于涂卡区域内")
        confidence = raw.get("confidence")
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= float(confidence) <= 1
        ):
            raise ValueError(f"{label}涂卡网格提议置信度无效")
        existing = by_key.get(key)
        if existing is not None and existing.get("state") == GRID_CONFIRMED:
            skipped.append(answer_key(*key))
            continue
        by_key[key] = {
            "question_no": key[0],
            "subquestion_no": key[1],
            "page_id": str(region["page_id"]),
            "region_id": str(region["region_id"]),
            "allow_multiple": bool(region.get("allow_multiple", False)),
            "state": GRID_PROPOSED,
            "grid_order": "left_to_right",
            "grid_bbox": grid_bbox,
            "cells": [],
            "source": SOURCE_MODEL_PROPOSAL,
            "proposal": {
                "grid_bbox": list(grid_bbox),
                "confidence": None if confidence is None else round(float(confidence), 4),
                "proposed_at": now,
                "proposer": dict(proposer_record),
            },
            "confirmed_by_teacher_id": None,
            "confirmed_at": None,
            "adjusted": False,
        }
    ordered = _order_rows(by_key, answer_regions)
    _check_region_rows(ordered)
    return ordered, skipped


def confirm_grid_rows(
    rows: list[dict[str, Any]],
    confirmations: list[dict[str, Any]],
    *,
    answer_regions: list[dict[str, Any]],
    question_specs: list[dict[str, Any]],
    teacher_id: int,
    now: str,
) -> list[dict[str, Any]]:
    """Apply the teacher's explicit confirm/adjust with concrete option cells.

    Only the fields below are read from the request; who and when come from
    the authenticated caller and the server clock.
    """
    if isinstance(teacher_id, bool) or not isinstance(teacher_id, int) or teacher_id <= 0:
        raise ValueError("涂卡网格必须由教师确认")
    if not isinstance(confirmations, list) or not confirmations:
        raise ValueError("请至少确认一个涂卡网格")
    choice_regions = _choice_mark_regions(answer_regions)
    specs = {str(item.get("question_no") or "").strip(): item for item in question_specs}
    by_key = {_row_key(row): row for row in rows}
    seen: set[tuple[str, str]] = set()
    for raw in confirmations:
        if not isinstance(raw, dict):
            raise ValueError("涂卡网格确认格式错误")
        key, region, label = _resolve_draft_target(raw, choice_regions, specs)
        if key in seen:
            raise ValueError(f"{label}涂卡网格确认重复")
        seen.add(key)
        grid_order = str(raw.get("grid_order") or "left_to_right").strip()
        cells = _validated_cells(raw.get("cells"), label=label, region=region, grid_order=grid_order)
        grid_bbox = _union([cell["bbox"] for cell in cells])
        existing = by_key.get(key) or {}
        proposal = existing.get("proposal") if isinstance(existing.get("proposal"), dict) else None
        by_key[key] = {
            "question_no": key[0],
            "subquestion_no": key[1],
            "page_id": str(region["page_id"]),
            "region_id": str(region["region_id"]),
            "allow_multiple": bool(region.get("allow_multiple", False)),
            "state": GRID_CONFIRMED,
            "grid_order": grid_order,
            "grid_bbox": grid_bbox,
            "cells": cells,
            "source": SOURCE_MODEL_PROPOSAL if proposal else SOURCE_TEACHER_DRAWN,
            "proposal": proposal,
            "confirmed_by_teacher_id": teacher_id,
            "confirmed_at": now,
            "adjusted": bool(proposal) and grid_bbox != proposal.get("grid_bbox"),
        }
    ordered = _order_rows(by_key, answer_regions)
    _check_region_rows(ordered)
    return ordered


def discard_grid_rows(
    rows: list[dict[str, Any]],
    keys: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Teacher removes proposed or confirmed rows; they become missing again."""
    if not isinstance(keys, list) or not keys:
        raise ValueError("请选择要删除的涂卡网格")
    by_key = {_row_key(row): row for row in rows}
    for raw in keys:
        if not isinstance(raw, dict):
            raise ValueError("涂卡网格格式错误")
        key = _row_key(raw)
        if key not in by_key:
            raise ValueError(f"第{answer_key(*key) or '?'}题没有可删除的涂卡网格")
        del by_key[key]
    return [row for row in rows if _row_key(row) in by_key]


def reconcile_grid_rows(
    rows: list[dict[str, Any]],
    previous_regions: list[dict[str, Any]],
    answer_regions: list[dict[str, Any]],
    question_specs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep a row only while its region is unchanged after a layout save.

    Any change to the bound region's page, number, box, options or single/
    multiple mode drops the row (proposed or confirmed): the teacher must
    confirm the grid again instead of inheriting a stale placement.
    """
    def identity(region: dict[str, Any]) -> tuple:
        return (
            str(region.get("region_id")), str(region.get("page_id")),
            str(region.get("question_no")), str(region.get("subquestion_no") or ""),
            region.get("region_type"), list(region.get("bbox") or []),
            list(region.get("choice_options") or []), bool(region.get("allow_multiple")),
        )

    before = {str(region.get("region_id")): identity(region) for region in previous_regions}
    after = {str(region.get("region_id")): identity(region) for region in answer_regions}
    specs = {str(item.get("question_no") or "").strip(): item for item in question_specs}
    kept: list[dict[str, Any]] = []
    for row in rows:
        region_id = str(row.get("region_id"))
        spec = specs.get(str(row.get("question_no")))
        if (
            region_id not in after
            or before.get(region_id) != after[region_id]
            or after[region_id][4] != "choice_mark"
            or spec is None
        ):
            continue
        try:
            if str(row.get("subquestion_no") or "") not in choice_row_keys(spec):
                continue
        except ValueError:
            continue
        kept.append(row)
    return kept


def bubble_grid_coverage(
    answer_regions: list[dict[str, Any]],
    grids: list[dict[str, Any]],
    question_specs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Grid rows each joined choice_mark region still needs from the teacher.

    ``missing`` rows were never placed, ``unconfirmed`` rows are only model
    proposals, and ``conflicts`` are rows that no longer match a region or the
    paper's small-question structure.  ``ok`` requires none of the three.
    """
    specs = {str(item.get("question_no") or "").strip(): item for item in question_specs}
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    conflicts: list[str] = []
    for row in grids or []:
        key = _row_key(row)
        if key in by_key:
            conflicts.append(f"第{answer_key(*key)}题涂卡网格重复")
        by_key[key] = row
    missing: list[str] = []
    unconfirmed: list[str] = []
    expected: set[tuple[str, str]] = set()
    for region in answer_regions:
        if region.get("region_type") != "choice_mark":
            continue
        question_no = str(region.get("question_no") or "").strip()
        spec = specs.get(question_no)
        if spec is None or str(spec.get("question_type") or "") != "choice":
            continue  # reported by the question-number join
        try:
            row_keys = choice_row_keys(spec)
        except ValueError as exc:
            conflicts.append(str(exc))
            continue
        for sub in row_keys:
            key = (question_no, sub)
            expected.add(key)
            row = by_key.get(key)
            label = answer_key(question_no, sub)
            if row is None:
                missing.append(label)
            elif str(row.get("region_id")) != str(region.get("region_id")):
                conflicts.append(f"第{label}题涂卡网格不属于该题涂卡区域")
            elif not _is_confirmed(row):
                unconfirmed.append(label)
    for key in by_key:
        if key not in expected:
            conflicts.append(f"第{answer_key(*key)}题涂卡网格没有对应的涂卡区域或小问")
    conflicts = list(dict.fromkeys(conflicts))
    return {
        "ok": not missing and not unconfirmed and not conflicts,
        "missing": missing,
        "unconfirmed": unconfirmed,
        "conflicts": conflicts,
    }


def grid_error_message(coverage: dict[str, Any]) -> str:
    parts = []
    if coverage.get("missing"):
        parts.append("缺少涂卡网格：第" + "、".join(coverage["missing"]) + "题")
    if coverage.get("unconfirmed"):
        parts.append("涂卡网格仅为模型提议、尚未经教师确认：第" + "、".join(coverage["unconfirmed"]) + "题")
    if coverage.get("conflicts"):
        parts.append("涂卡网格冲突：" + "；".join(coverage["conflicts"]))
    return "；".join(parts) or "涂卡网格未通过校验"


def freeze_grid_rows(
    rows: list[dict[str, Any]],
    answer_regions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split confirmed draft rows into frozen geometry and confirmation provenance.

    Geometry is canonical and paper-independent, so the same sheet always
    yields the same template fingerprint; who/when is kept beside it.
    """
    pending = [answer_key(*_row_key(row)) for row in rows if not _is_confirmed(row)]
    if pending:
        raise ValueError("仍有未经教师确认的涂卡网格提议：第" + "、".join(pending) + "题")
    ordered = _order_rows({_row_key(row): row for row in rows}, answer_regions)
    frozen = [{
        "question_no": row["question_no"],
        "subquestion_no": row["subquestion_no"],
        "page_id": row["page_id"],
        "region_id": row["region_id"],
        "allow_multiple": bool(row["allow_multiple"]),
        "grid_order": row["grid_order"],
        "grid_bbox": list(row["grid_bbox"]),
        "cells": [
            {"option": cell["option"], "bbox": list(cell["bbox"]), "cell_size": list(cell["cell_size"])}
            for cell in row["cells"]
        ],
        "teacher_confirmed": True,
    } for row in ordered]
    provenance = [{
        "question_no": row["question_no"],
        "subquestion_no": row["subquestion_no"],
        "source": row["source"],
        "adjusted": bool(row.get("adjusted")),
        "proposal": row.get("proposal"),
        "confirmed_by_teacher_id": row["confirmed_by_teacher_id"],
        "confirmed_at": row["confirmed_at"],
    } for row in ordered]
    validate_frozen_bubble_grids(frozen, answer_regions)
    validate_grid_confirmations(frozen, provenance)
    return frozen, provenance


def validate_frozen_bubble_grids(
    grids: object,
    answer_regions: list[dict[str, Any]],
) -> None:
    """Integrity of frozen template geometry, independent of any paper template."""
    if not isinstance(grids, list):
        raise ValueError("模板涂卡网格格式错误")
    regions = {
        str(region.get("region_id")): region
        for region in answer_regions
        if region.get("region_type") == "choice_mark"
    }
    seen: set[tuple[str, str]] = set()
    for row in grids:
        if not isinstance(row, dict) or set(row) != _FROZEN_ROW_FIELDS:
            raise ValueError("模板涂卡网格字段无效")
        key = _row_key(row)
        label = f"第{answer_key(*key)}题"
        if row["teacher_confirmed"] is not True:
            raise ValueError(f"{label}涂卡网格未经教师确认，不能冻结")
        if key in seen:
            raise ValueError(f"{label}涂卡网格重复")
        seen.add(key)
        sub = key[1]
        if sub and not (sub.isdigit() and 1 <= int(sub) <= MAX_SUBQUESTION_ROWS):
            raise ValueError(f"{label}涂卡网格小问号无效")
        region = regions.get(str(row["region_id"]))
        if (
            region is None
            or str(region.get("page_id")) != str(row["page_id"])
            or str(region.get("question_no")) != key[0]
            or bool(region.get("allow_multiple", False)) is not row["allow_multiple"]
        ):
            raise ValueError(f"{label}涂卡网格与涂卡区域不一致")
        cells = _validated_cells(
            row["cells"], label=label, region=region, grid_order=str(row["grid_order"]),
        )
        if cells != row["cells"] or row["grid_bbox"] != _union([cell["bbox"] for cell in cells]):
            raise ValueError(f"{label}涂卡网格几何数据不一致")
    _check_region_rows(grids)


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def validate_grid_confirmations(
    grids: list[dict[str, Any]],
    confirmations: object,
) -> None:
    """Every frozen row must carry exactly one teacher confirmation record."""
    if not isinstance(confirmations, list) or len(confirmations) != len(grids):
        raise ValueError("涂卡网格教师确认记录缺失")
    expected = [_row_key(row) for row in grids]
    for key, record in zip(expected, confirmations):
        if not isinstance(record, dict) or _row_key(record) != key:
            raise ValueError("涂卡网格教师确认记录与网格不一致")
        teacher_id = record.get("confirmed_by_teacher_id")
        proposal = record.get("proposal")
        source = record.get("source")
        if (
            isinstance(teacher_id, bool)
            or not isinstance(teacher_id, int)
            or teacher_id <= 0
            or not _valid_timestamp(record.get("confirmed_at"))
            or not isinstance(record.get("adjusted"), bool)
            or source not in {SOURCE_MODEL_PROPOSAL, SOURCE_TEACHER_DRAWN}
            or (source == SOURCE_MODEL_PROPOSAL) != isinstance(proposal, dict)
            or (proposal is not None and not isinstance(proposal, dict))
        ):
            raise ValueError(f"第{answer_key(*key)}题涂卡网格教师确认记录无效")


def normalized_bbox_to_pixels(bbox: list[int], width: int, height: int) -> list[int]:
    """Half-open pixel box ``[x1, y1, x2, y2)`` of a normalized 0..1000 box.

    Uses the same floor/ceil rounding as ``crop_normalized_rgb`` so a resolved
    cell covers exactly the pixels that crop would read.
    """
    left, top, right, bottom = bbox
    return [
        left * width // 1000,
        top * height // 1000,
        max(1, -(-right * width // 1000)),
        max(1, -(-bottom * height // 1000)),
    ]


def resolve_bubble_grid_pixels(
    grids: list[dict[str, Any]],
    page_sizes: dict[str, tuple[int, int]],
) -> dict[str, dict[str, Any]]:
    """Map each answer key (``"3"``, ``"1(2)"``) to its grid row's pixel boxes.

    Pure geometry in the template's canonical page frame (the frame student
    pages are registered to); no image is read here.
    """
    resolved: dict[str, dict[str, Any]] = {}
    for row in grids:
        size = page_sizes.get(str(row["page_id"]))
        if (
            not isinstance(size, (list, tuple))
            or len(size) != 2
            or any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in size)
        ):
            raise ValueError(f"第{answer_key(*_row_key(row))}题涂卡网格所在页面尺寸无效")
        width, height = size
        resolved[answer_key(*_row_key(row))] = {
            "question_no": row["question_no"],
            "subquestion_no": row["subquestion_no"],
            "page_id": row["page_id"],
            "region_id": row["region_id"],
            "allow_multiple": row["allow_multiple"],
            "grid_order": row["grid_order"],
            "page_size": [width, height],
            "grid_bbox": list(row["grid_bbox"]),
            "grid_pixel_box": normalized_bbox_to_pixels(row["grid_bbox"], width, height),
            "cells": [
                {
                    "option": cell["option"],
                    "bbox": list(cell["bbox"]),
                    "pixel_box": normalized_bbox_to_pixels(cell["bbox"], width, height),
                }
                for cell in row["cells"]
            ],
        }
    return resolved


def _text_slot_indexes(spec: dict[str, Any]) -> list[str]:
    from services.template_slots import text_slot_ids

    question_no = str(spec.get("question_no"))
    return [
        slot.rsplit("[", 1)[1][:-1]
        for slot in text_slot_ids(question_no, spec)
    ]


def required_answer_keys(spec: dict[str, Any], regions: list[dict[str, Any]]) -> list[str]:
    """Sub-keys a question needs; '' means the whole question."""
    if spec.get("slot_ids") is not None:
        # One text slot is read from a whole-question region; several slots
        # each need their own sub-question region ``question_no(k)``.
        indexes = _text_slot_indexes(spec)
        return indexes if len(indexes) > 1 else [""]
    count = int(spec.get("subquestion_count") or 1)
    if count > 1 and any(str(region.get("subquestion_no") or "") for region in regions):
        return [str(index) for index in range(1, count + 1)]
    return [""]


def question_number_join(
    answer_regions: list[dict[str, Any]],
    question_specs: list[dict[str, Any]],
    *,
    confirmed_only: bool = True,
) -> dict[str, Any]:
    """Join layout regions to paper questions by question and sub-question.

    Returns the three fail-closed categories; ``ok`` is true only when every
    required question is covered, every region's number exists, and no
    question/sub-question conflict remains.
    """
    specs = {str(item.get("question_no") or "").strip(): item for item in question_specs}
    regions = [
        region for region in answer_regions
        if not confirmed_only or region.get("teacher_confirmed")
    ]
    unknown_regions: list[str] = []
    by_question: dict[str, list[dict[str, Any]]] = {}
    for region in regions:
        question_no = str(region.get("question_no") or "").strip()
        if question_no not in specs:
            unknown_regions.append(str(region.get("region_id")))
            continue
        by_question.setdefault(question_no, []).append(region)

    missing: list[str] = []
    conflicts: list[str] = []
    for question_no, spec in specs.items():
        question_regions = by_question.get(question_no, [])
        question_type = str(spec.get("question_type") or "solve")
        for region in question_regions:
            expected_type = _REGION_QUESTION_TYPES.get(str(region.get("region_type")))
            if expected_type != question_type:
                conflicts.append(f"第{question_no}题题型与答题区域类型冲突")
            if question_type == "choice" and bool(region.get("allow_multiple")) != (
                str(spec.get("choice_mode") or "") == "multiple"
            ):
                conflicts.append(f"第{question_no}题单选/多选设置与试卷模板不一致")
        if question_type == "choice" and len(question_regions) > 1:
            conflicts.append(f"第{question_no}题选择题只能有一个答题区域")

        sub_keys = [str(region.get("subquestion_no") or "").strip() for region in question_regions]
        required_regions = [region for region in question_regions if region.get("required", True)]
        try:
            required = required_answer_keys(spec, question_regions)
        except ValueError:
            conflicts.append(f"第{question_no}题作答位合同无效")
            continue
        allowed = set(required)
        if "" in sub_keys and any(sub_keys):
            conflicts.append(f"第{question_no}题整题区域与小问区域不能同时存在")
        for key in sub_keys:
            if key not in allowed:
                label = f"({key})" if key else ""
                conflicts.append(f"第{question_no}题{label}小问号不在试卷模板中")
        covered = {
            str(region.get("subquestion_no") or "").strip() for region in required_regions
        }
        for key in required:
            if key not in covered:
                missing.append(question_no if not key else f"{question_no}({key})")

    return {
        "ok": not missing and not unknown_regions and not conflicts,
        "missing": missing,
        "unknown_regions": unknown_regions,
        "conflicts": list(dict.fromkeys(conflicts)),
    }


def join_error_message(report: dict[str, Any]) -> str:
    parts = []
    if report.get("missing"):
        parts.append("必答题缺少答题区域：" + "、".join(report["missing"]))
    if report.get("unknown_regions"):
        parts.append("区域题号不在试卷模板中：" + "、".join(report["unknown_regions"]))
    if report.get("conflicts"):
        parts.append("题号/小问冲突：" + "；".join(report["conflicts"]))
    return "；".join(parts) or "答题卡与试卷模板按题号绑定失败"
