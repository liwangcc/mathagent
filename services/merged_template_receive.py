"""合并模板识别输出的接收侧适配层（单次输出 → 三段既有接收器）。

状态：历史单次输出接收器，仅供显式回放和兼容测试。生产模板使用 services/template_max_owned.py 的并行合同。
以下为历史回放调用示例，不是生产回退路线：

    result = await _request(
        [image_path, grid_path],
        build_merged_template_prompt(width, height, education_stage=stage),
        "template-merged-v1",
        receive_merged_template_output,
    )

返回 {"question_directory": …, "questions": …}，与三段流程的产物等价（含逐题
作图属性回填）。

与三段契约的已知差异及本层处理（依据 2026-09-11 v4 实测输出）：
- 合并输出不再输出 exemptions（统一作答框方向），而目录解析器要求该键为列表：
  本层对缺失节点补 []；模型若仍给出则原样保留。
- 作答/作图字段内嵌在末级节点上，而三个接收器各有严格字段白名单：本层按段
  拆分——目录段只保留结构字段（drawing_regions/label_anchor 移出，供定位段
  重新注入），standard_answer/solution_steps 只进入解题载荷。
- 合并输出可直接给出 answer_kind（non_graphic|drawing|mixed）。旧结果没有该字段时，
  仍按 requires_drawing 和定位字段作保守兼容映射。
- question_text 只取自节点的 question_text（完整题干，v5 起由提示词强制输出）；
  context_summary 仅作目录结构摘要，不得再作为题干回填（缺失时如实标记
  “待教师核对题干”，不拿摘要顶替）。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from services.question_directory import (
    COORDINATE_SPACE,
    _object,
    bind_directory_answers,
    bind_drawing_locations,
    parse_question_directory,
)

__all__ = ["GRAPHIC_ANSWER_KIND", "receive_merged_template_output"]

# requires_drawing=true 的含义是"题目要求作图"；经典定位段还会区分 drawing
# （纯作图）与 mixed（作图+文字/计算），合并输出没有这一位信息。接收侧默认
# 按更保守的一侧映射为 mixed：文字/计算仍走既有管线（scope_hint=text_only），
# 宁可多一次可见的教师复核，也不静默跳过文字/计算部分。若口径改为纯作图，
# 只需把本常量改为 "drawing"（接收测试中有对应断言）。
GRAPHIC_ANSWER_KIND = "mixed"

_LEAF_KINDS = {"question", "answer_item"}


def _split_merged_tree(payload: dict) -> tuple[dict, list[dict]]:
    """Split one merged tree into the directory-stage tree plus captured leaf data.

    The copy handed to the directory parser keeps structural fields only; each
    leaf's answer and drawing values are captured beforehand, because the staged
    binders accept them solely through their own dedicated payloads. Drawing
    fields are removed from every node; missing exemptions become [].
    """
    tree = deepcopy(payload)
    captured: list[dict] = []

    def visit(siblings: list) -> None:
        for node in siblings:
            if not isinstance(node, dict):
                continue
            node.setdefault("exemptions", [])
            children = node.get("children") or []
            if not children and node.get("kind") in _LEAF_KINDS:
                captured.append({
                    "printed_label": node.get("printed_label"),
                    "question_text": node.get("question_text"),
                    "standard_answer": node.get("standard_answer"),
                    "solution_steps": node.get("solution_steps"),
                    "drawing_regions": node.get("drawing_regions"),
                    "label_anchor": node.get("label_anchor"),
                    "requires_drawing": node.get("requires_drawing"),
                    "answer_kind": node.get("answer_kind"),
                })
            # Drawing fields belong to the location stage: they are re-injected
            # through its payload, never through the structural tree.
            node.pop("drawing_regions", None)
            node.pop("label_anchor", None)
            node.pop("answer_kind", None)
            node.pop("drawing_reference", None)
            if isinstance(children, list):
                visit(children)

    nodes = tree.get("nodes")
    if isinstance(nodes, list):
        visit(nodes)
    return tree, captured


def _answer_kind(captured: dict) -> str:
    """Validate explicit answer_kind and retain a conservative legacy fallback."""
    verdict = captured.get("requires_drawing")
    if verdict is not None and not isinstance(verdict, bool):
        raise ValueError("Invalid requires_drawing")
    localized = bool(captured.get("drawing_regions")) or captured.get("label_anchor") is not None
    explicit = captured.get("answer_kind")
    if explicit is not None:
        if explicit not in {"non_graphic", "drawing", "mixed"}:
            raise ValueError("Invalid answer_kind")
        if explicit == "non_graphic" and (verdict is True or localized):
            raise ValueError("non_graphic cannot define drawing regions")
        if explicit in {"drawing", "mixed"} and verdict is False:
            raise ValueError("graphic answer_kind conflicts with requires_drawing=false")
        return explicit
    if verdict is True or (verdict is None and localized):
        return GRAPHIC_ANSWER_KIND
    return "non_graphic"


def _answer_row(captured: dict, directory_leaf: dict) -> dict:
    """One classic solver answer row; structural fields always come from the directory."""
    text = captured.get("question_text")
    text = text.strip() if isinstance(text, str) else ""
    standard = captured.get("standard_answer")
    if standard is None:
        standard = ""
    steps = captured.get("solution_steps")
    if steps is None:
        steps = []
    ready = bool(text) and isinstance(standard, str) and bool(standard.strip())
    return {"q_id": directory_leaf["q_id"],
            "question_text": text or "待教师核对题干",
            "standard_answer": standard,
            "solution_steps": steps,
            "answer_status": "ready" if ready else "needs_review"}


def _drawing_row(captured: dict, directory_leaf: dict) -> dict:
    """One classic locator row; exactly the four keys the locator binder accepts."""
    regions = captured.get("drawing_regions")
    if regions is None:
        regions = []
    return {"question_number": directory_leaf["question_number"],
            "answer_kind": _answer_kind(captured),
            "drawing_regions": regions,
            "label_anchor": captured.get("label_anchor")}


def receive_merged_template_output(payload: Any) -> dict:
    """Receive one merged template response through the three staged binders.

    Input: the model's single JSON reply (parsed object or JSON text) in the
    merged schema. Output mirrors the classic pipeline result: the finished
    directory plus solved questions enriched with the bound drawing attributes.
    Raises ValueError on any contract violation; nothing is silently repaired.
    """
    payload = _object(payload)
    tree, captured = _split_merged_tree(payload)
    directory = parse_question_directory(tree)
    leaves = directory["leaves"]
    if len(captured) != len(leaves):
        raise ValueError("Merged leaf count differs from parsed directory")
    for row, leaf in zip(captured, leaves):
        if row["printed_label"] != leaf["printed_label"]:
            raise ValueError("Merged leaf order differs from parsed directory")
    directory = bind_drawing_locations(directory, {
        "coordinate_space": COORDINATE_SPACE,
        "questions": [_drawing_row(item, leaf) for item, leaf in zip(captured, leaves)],
        "needs_review": [],
    })
    # Resolve the graphic kind before answer readiness: a pure drawing has no
    # text-answer region and must not poison every answer as a directory error.
    questions = bind_directory_answers(directory, {
        "answers": [_answer_row(item, leaf) for item, leaf in zip(captured, directory["leaves"])],
        "directory_issues": [],
    })
    # The drawing binder returns the authoritative leaf objects with the
    # answer_kind/regions fields injected.
    leaves = directory["leaves"]
    for question, leaf in zip(questions, leaves):
        question.update({key: deepcopy(leaf[key]) for key in
                         ("answer_kind", "drawing_regions", "label_anchor") if key in leaf})
        question["directory_metadata"] = deepcopy(leaf)
    return {"question_directory": directory, "questions": questions}
