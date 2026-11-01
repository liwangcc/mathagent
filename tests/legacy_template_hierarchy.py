"""Retired pre-v3 Flash hierarchy receivers, kept only as test fixtures.

Production receives hierarchies exclusively through
``template_one_three.receive_hierarchy`` (contract v3/v4).  Consumer tests
that still build their inputs from the older unversioned, identity-v2 or
nested-tree payloads use these functions, moved here verbatim from
``services.template_directory_pipeline``.
"""

import copy
import re

from services import template_one_three as one_three
from services.template_directory_pipeline import _answer_slot_count, _drawing_area_count


def _printed_question_label(value: object) -> str:
    """Keep only a short visible source label for audit/provenance."""
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > 64 or any(ord(char) < 32 for char in value):
        raise ValueError("Flash 原题号文字无效")
    return value.strip()


def _is_recognizable_question_label(value: str) -> bool:
    # Arabic and Chinese ordinal labels are enough to distinguish printed
    # questions from section headings.  The displayed ID is still rebuilt from
    # reading order below.
    return bool(re.fullmatch(
        r"(?:"
        r"[1-9][0-9]*[.)、]?"
        r"|[一二三四五六七八九十百千万]+[、.)]?"
        r"|[（(]\s*[1-9][0-9]*\s*[）)]"
        r"|第\s*(?:[1-9][0-9]*|[一二三四五六七八九十百千万]+)\s*题?"
        r"|题\s*[1-9][0-9]*"
        r")",
        value,
    ))


def _validate_flash_question_groups(payload: object) -> dict:
    """Validate Flash's direct two-level question-group contract.

    The active Flash response contains no parent/child tree.  A compatibility
    branch accepts the former tree shape for old fixtures and persisted replay
    data. Active count/typed contracts never infer groups from printed labels.
    """
    if isinstance(payload, dict) and payload.get("contract_version") in {3, 4}:
        return one_three.receive_hierarchy(payload)
    identity_v2 = isinstance(payload, dict) and "contract_version" in payload
    if identity_v2:
        if type(payload["contract_version"]) is not int or payload["contract_version"] != 2:
            raise ValueError("Flash 题目身份合同版本无效")
        rows = payload.get("questions")
        if set(payload) != {"contract_version", "questions"} or not isinstance(rows, list) or not rows:
            raise ValueError("Flash v2 目录字段无效")
        anchors = set()
        for index, row in enumerate(rows, 1):
            if not isinstance(row, dict) or set(row) != {"question_number", "printed_label", "cue", "answer_regions"}:
                raise ValueError("Flash v2 题目身份字段不完整")
            if row["question_number"] != str(index):
                raise ValueError("Flash v2 题号必须连续且不得重排")
            label = _printed_question_label(row["printed_label"])
            cue = row["cue"]
            if not isinstance(cue, str) or not cue.strip():
                raise ValueError("Flash v2 缺少可见题目线索")
            if (label, cue.strip()) in anchors:
                raise ValueError("Flash v2 题目视觉身份重复")
            anchors.add((label, cue.strip()))
        payload = {"questions": rows}
    if isinstance(payload, dict) and "questions" not in payload and "nodes" in payload:
        legacy = {key: payload[key] for key in ("nodes", "needs_review") if key in payload}
        return _canonicalize_question_groups(_validate_hierarchy(legacy))
    if not isinstance(payload, dict) or set(payload) - {"questions", "needs_review"}:
        raise ValueError("Flash 题组返回字段无效")
    rows = payload.get("questions")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Flash 未返回题组")
    review = payload.get("needs_review", [])
    if not isinstance(review, list) or any(not isinstance(item, str) for item in review):
        raise ValueError("Flash needs_review 必须为字符串数组")

    if any(isinstance(row, dict) and "answer_region_count" in row for row in rows):
        nodes = []
        for index, row in enumerate(rows, 1):
            if not isinstance(row, dict) or set(row) != {"question_number", "answer_region_count"}:
                raise ValueError("Flash 题组必须只含题号和作答区域数")
            if row["question_number"] != str(index):
                raise ValueError("Flash 题号必须按阅读顺序从 1 连续编号")
            count = row["answer_region_count"]
            if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 20:
                raise ValueError("Flash 作答区域数量必须是 1 到 20 的整数")
            nodes.append({"id": str(index), "parent_id": None, "raw_label": "", "cue": "",
                          "source_path": [], "answer_slot_count": count, "answer_area_count": count})
        return {"nodes": nodes, "leaf_ids": [node["id"] for node in nodes],
                "needs_review": copy.deepcopy(review), "canonicalized": True,
                "region_count_contract": True, "typed_region_contract": False,
                "source_hierarchy": copy.deepcopy(payload)}

    # v2 uses explicit visible identity; old typed replay lacks those fields.
    # Neither contract may be regrouped by label heuristics.
    direct_typed = identity_v2 or all(
        isinstance(row, dict) and set(row) == {"question_number", "answer_regions"} for row in rows)
    parsed: list[dict] = []
    seen_numbers: set[str] = set()
    for index, raw in enumerate(rows, 1):
        if not isinstance(raw, dict) or set(raw) - {"question_number", "printed_label", "answer_regions", "cue"}:
            raise ValueError("Flash 题组字段无效")
        number = raw.get("question_number")
        if direct_typed and number != str(index):
            raise ValueError("Flash 题号必须按阅读顺序从 1 连续编号")
        if not isinstance(number, str) or not re.fullmatch(r"[1-9][0-9]*", number):
            raise ValueError("Flash 题组必须使用自然数题号")
        if number in seen_numbers:
            raise ValueError("Flash 题组题号重复")
        seen_numbers.add(number)
        printed_label = _printed_question_label(raw.get("printed_label", ""))
        cue = raw.get("cue", "")
        if not isinstance(cue, str) or len(cue) > 256 or any(ord(char) < 32 for char in cue):
            raise ValueError("Flash 题组说明无效")
        kinds = raw.get("answer_regions")
        if not isinstance(kinds, list) or not 1 <= len(kinds) <= 20:
            raise ValueError("Flash 作答区域数量无效")
        if any(kind not in {"answer", "drawing"} for kind in kinds):
            raise ValueError("Flash 作答区域类型无效")
        parsed.append({"raw_number": number, "printed_label": printed_label,
                       "cue": cue, "area_kinds": list(kinds)})

    # A page with no visible question label at all has no reliable grouping
    # signal.  Do not let a model hallucinated list of unlabeled items become
    # multiple questions.  If at least one label is visible, retain the model's
    # independently bounded groups and send only unlabeled rows to review.
    has_any_label = any(_is_recognizable_question_label(row["printed_label"]) for row in parsed)
    if not direct_typed and not has_any_label:
        merged = {
            "id": "1", "parent_id": None, "raw_label": "", "cue": "",
            "area_kinds": [kind for row in parsed for kind in row["area_kinds"]],
            "source_ids": [row["raw_number"] for row in parsed],
            "source_path": [],
        }
        merged["answer_slot_count"] = sum(kind == "answer" for kind in merged["area_kinds"]) or 1
        merged["answer_area_count"] = merged["answer_slot_count"]
        merged["drawing_area_count"] = sum(kind == "drawing" for kind in merged["area_kinds"])
        return {"nodes": [merged], "needs_review": copy.deepcopy(review),
                "leaf_ids": ["1"], "canonicalized": True, "region_count_contract": True,
                "typed_region_contract": True,
                "source_hierarchy": copy.deepcopy(payload)}

    if not direct_typed and any(not _is_recognizable_question_label(row["printed_label"]) for row in parsed):
        review = list(review)
        review.append("部分题组没有可识别的印刷题号，已保留为独立 Q 并需教师复核")

    nodes = []
    for index, row in enumerate(parsed, 1):
        node = {
            "id": str(index), "parent_id": None,
            "raw_label": row["printed_label"], "cue": row["cue"],
            "area_kinds": row["area_kinds"],
            "source_id": row["raw_number"],
            "source_parent_id": None,
            "source_path": [row["printed_label"]] if row["printed_label"] else [],
        }
        node["answer_slot_count"] = sum(kind == "answer" for kind in row["area_kinds"]) or 1
        node["answer_area_count"] = node["answer_slot_count"]
        node["drawing_area_count"] = sum(kind == "drawing" for kind in row["area_kinds"])
        nodes.append(node)
    return {"nodes": nodes, "needs_review": copy.deepcopy(review),
            "leaf_ids": [node["id"] for node in nodes], "canonicalized": True, "region_count_contract": True,
            "typed_region_contract": True,
            "question_identity_version": 2 if identity_v2 else 1,
            "source_hierarchy": {"contract_version": 2, **copy.deepcopy(payload)} if identity_v2 else copy.deepcopy(payload)}


def _validate_hierarchy(payload: object) -> dict:
    if not isinstance(payload, dict) or set(payload) - {"nodes", "needs_review"}:
        raise ValueError("Flash 题号层级返回字段无效")
    rows = payload.get("nodes")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Flash 未返回题号层级")
    ids, parents = [], {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Flash 层级节点必须为对象")
        node_id = row.get("id")
        if not isinstance(node_id, str) or not re.fullmatch(r"[1-9][0-9]*(?:\.[1-9][0-9]*)*", node_id):
            raise ValueError("Flash 层级号无效")
        if node_id in ids:
            raise ValueError("Flash 层级号重复")
        parent = row.get("parent_id")
        if parent is not None and (not isinstance(parent, str) or not re.fullmatch(r"[1-9][0-9]*(?:\.[1-9][0-9]*)*", parent)):
            raise ValueError("Flash 父层级号无效")
        if not isinstance(row.get("raw_label", ""), str) or not isinstance(row.get("cue", ""), str):
            raise ValueError("Flash 层级文字无效")
        parts = row.get("parts", [])
        if not isinstance(parts, list) or any(not isinstance(part, str) for part in parts):
            raise ValueError("Flash 多空说明无效")
        kinds = row.get("area_kinds")
        if kinds is not None and (not isinstance(kinds, list) or any(kind not in {"answer", "drawing"} for kind in kinds)):
            raise ValueError("Flash 作答区域类型无效")
        _answer_slot_count(row)
        _drawing_area_count(row)
        ids.append(node_id)
        parents[node_id] = parent
    # Small-school worksheets often use a flat printed number under a named
    # section (for example ``id=2,parent_id=1``).  Convert that unambiguous
    # shape to the canonical dotted path before validating, while preserving
    # the model's reading order and raw labels.
    normalized_ids: dict[str, str] = {}
    child_indexes: dict[str, int] = {}
    for node_id in ids:
        parent = parents[node_id]
        if parent is None or node_id.startswith(parent + "."):
            normalized_ids[node_id] = node_id
            continue
        child_indexes[parent] = child_indexes.get(parent, 0) + 1
        normalized_ids[node_id] = f"{normalized_ids.get(parent, parent)}.{child_indexes[parent]}"
    flat_child_numbering = any(
        parents[node_id] is not None and not node_id.startswith(parents[node_id] + ".")
        for node_id in ids
    )
    if flat_child_numbering:
        # Rebase roots as well: a model may number section nodes by its
        # position in the whole node list (1,5,7), which is not a real printed
        # hierarchy.  Root order is the only stable signal in that response.
        normalized_ids = {}
        root_index = 0
        child_indexes = {}
        for node_id in ids:
            parent = parents[node_id]
            if parent is None:
                root_index += 1
                normalized_ids[node_id] = str(root_index)
            else:
                child_indexes[parent] = child_indexes.get(parent, 0) + 1
                normalized_ids[node_id] = f"{normalized_ids[parent]}.{child_indexes[parent]}"
    if any(normalized_ids[node_id] != node_id for node_id in ids):
        normalized_rows = []
        for row in rows:
            item = copy.deepcopy(row)
            old_id = item["id"]
            item["id"] = normalized_ids[old_id]
            if item.get("parent_id") is not None:
                item["parent_id"] = normalized_ids.get(item["parent_id"], item["parent_id"])
            normalized_rows.append(item)
        rows = normalized_rows
        ids = [row["id"] for row in rows]
        parents = {row["id"]: row.get("parent_id") for row in rows}
    known = set(ids)
    for node_id, parent in parents.items():
        if parent is not None and parent not in known:
            raise ValueError("Flash 层级父节点不存在")
        if parent is not None and not node_id.startswith(parent + "."):
            raise ValueError("Flash 层级父子关系不一致")
    review = payload.get("needs_review", [])
    if not isinstance(review, list):
        raise ValueError("Flash needs_review 必须为数组")
    children = {parent for parent in parents.values() if parent is not None}
    leaves = [node_id for node_id in ids if node_id not in children]
    if not leaves:
        raise ValueError("Flash 层级没有末级题目")
    normalized_rows = copy.deepcopy(rows)
    for row in normalized_rows:
        row["answer_slot_count"] = _answer_slot_count(row)
        row["answer_area_count"] = row["answer_slot_count"]
        row["drawing_area_count"] = _drawing_area_count(row)
        if "area_kinds" not in row:
            row["area_kinds"] = []
    return {"nodes": normalized_rows, "needs_review": copy.deepcopy(review), "leaf_ids": leaves}


def _canonicalize_question_groups(hierarchy: dict) -> dict:
    """Collapse model hierarchy into the public two-level Q -> region contract.

    Flash may use sections and dotted leaf paths to preserve the page's printed
    structure.  Those paths are provenance only.  The persisted/displayed
    question number is always a natural number.  When Flash finds no parent
    relationships at all (common on handwritten worksheets), every region is
    intentionally grouped under one Q1.
    """
    nodes = list(hierarchy.get("nodes") or [])
    by_id = {row["id"]: row for row in nodes}
    leaves = [by_id[node_id] for node_id in hierarchy.get("leaf_ids", []) if node_id in by_id]
    has_hierarchy = any(row.get("parent_id") is not None for row in nodes)
    # Flat handwritten pages often have no labels at all; those collapse to
    # Q1. A flat list of explicit numeric labels is still a recognizable
    # question sequence (legacy worksheets commonly use this shape), so keep
    # those as separate natural-number groups.
    flat_numbered = (
        not has_hierarchy and len(nodes) > 1
        and all(re.fullmatch(r"[1-9][0-9]*[.)]?", str(row.get("raw_label") or "").strip()) for row in nodes)
    )
    groups = leaves if has_hierarchy else nodes
    if not groups:
        raise ValueError("Flash 层级没有可重构的作答区域")

    canonical = []
    if not has_hierarchy and not flat_numbered:
        source_rows = groups
        groups = [{"id": "1", "parent_id": None, "raw_label": "", "cue": "",
                   "area_kinds": [kind for row in source_rows for kind in (row.get("area_kinds") or [])],
                   "answer_slot_count": sum(_answer_slot_count(row) for row in source_rows),
                   "drawing_area_count": sum(_drawing_area_count(row) for row in source_rows),
                   "source_ids": [row.get("id") for row in source_rows],
                   "source_paths": [[row.get("raw_label", "")] for row in source_rows]}]
    for index, row in enumerate(groups, 1):
        source_id = row.get("id")
        source_path = []
        cursor = source_id
        while cursor in by_id:
            source_path.append(by_id[cursor].get("raw_label", ""))
            cursor = by_id[cursor].get("parent_id")
        source_path.reverse()
        if row.get("source_paths") is not None:
            source_path = [path for paths in row["source_paths"] for path in paths]
        source_path = [value for value in source_path if isinstance(value, str) and value.strip()]
        item = copy.deepcopy(row)
        item.update({
            "id": str(index), "parent_id": None,
            "source_id": row.get("source_ids", source_id) if row.get("source_ids") is not None else source_id,
            "source_parent_id": row.get("parent_id"),
            "source_path": source_path,
        })
        item["answer_slot_count"] = _answer_slot_count(row)
        item["answer_area_count"] = item["answer_slot_count"]
        item["drawing_area_count"] = _drawing_area_count(row)
        canonical.append(item)
    return {"nodes": canonical, "needs_review": copy.deepcopy(hierarchy.get("needs_review") or []),
            "leaf_ids": [row["id"] for row in canonical], "canonicalized": True,
            "source_hierarchy": copy.deepcopy(nodes)}
