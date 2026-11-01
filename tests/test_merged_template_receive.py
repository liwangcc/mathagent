"""合并输出接收侧适配层测试（services.merged_template_receive）。

形状依据 2026-09-11 v4 实测产物：节点不含 exemptions；
requires_drawing 与 standard_answer/solution_steps/drawing_regions/label_anchor
内嵌在末级节点上；没有定位段的 drawing/mixed 区分。
v5：末级节点必须带 question_text（完整题干）；接收侧不得以 context_summary 摘要顶替。
"""
import json
from copy import deepcopy

import pytest

from services.merged_template_receive import GRAPHIC_ANSWER_KIND, receive_merged_template_output
from services.question_directory import COORDINATE_SPACE


def merged_leaf(label="1", question_type="fill", *, kind="question", summary="印刷条件",
                text="完整题干", answer="答案", steps=("步骤",), requires_drawing=False,
                drawing_regions=None, label_anchor=None, **kwargs):
    node = {"printed_label": label, "kind": kind, "children": [],
            "number_bbox": [10, 10, 20, 20], "question_type": question_type,
            "choice_mode": "single" if question_type == "choice" else "unknown",
            "regions": [[100, 100, 200, 150]], "context_summary": summary,
            "requires_drawing": requires_drawing}
    if text is not None:
        node["question_text"] = text
    if answer is not None:
        node["standard_answer"] = answer
    if steps is not None:
        node["solution_steps"] = list(steps)
    if drawing_regions is not None:
        node["drawing_regions"] = drawing_regions
    if label_anchor is not None:
        node["label_anchor"] = label_anchor
    node.update(kwargs)
    return node


def merged_tree(nodes, *, needs_review=None):
    return {"coordinate_space": COORDINATE_SPACE, "nodes": nodes,
            "needs_review": [] if needs_review is None else needs_review}


def test_receives_one_payload_into_directory_questions_and_drawing():
    payload = merged_tree([
        {"printed_label": "一", "kind": "section", "context_summary": "选择题",
         "number_bbox": [10, 10, 30, 30], "question_type": None, "choice_mode": "unknown",
         "regions": [], "children": [merged_leaf("1", "choice", answer="B")]},
        merged_leaf("2", "fill", answer="42"),
        merged_leaf("3", "solve", answer="④可标在(6,5)处或(0,7)处", requires_drawing=True,
                    drawing_regions=[[700, 353, 900, 500]], label_anchor=[695, 420]),
    ])
    snapshot = deepcopy(payload)
    result = receive_merged_template_output(payload)

    assert payload == snapshot  # 接收不得修改调用方输入
    directory, questions = result["question_directory"], result["questions"]
    assert directory["version"] == "question_directory_v1"
    assert [leaf["question_number"] for leaf in directory["leaves"]] == ["1", "2", "3"]
    assert directory["leaves"][0]["exemptions"] == []  # 缺失的 exemptions 补空
    assert directory["leaves"][0]["answer_kind"] == "non_graphic"
    graphic = directory["leaves"][2]
    # 合并输出没有 drawing/mixed 之分；接收侧按保守口径统一映射（见 GRAPHIC_ANSWER_KIND）。
    assert graphic["answer_kind"] == "mixed" == GRAPHIC_ANSWER_KIND
    assert graphic["drawing_regions"] == [[700, 353, 900, 500]]
    assert graphic["label_anchor"] == [695, 420]
    assert directory["needs_review"] == []

    assert [q["q_id"] for q in questions] == ["Q1", "Q2", "Q3"]
    assert questions[0]["question_text"] == "完整题干"
    assert questions[0]["standard_answer"] == "B"
    assert questions[0]["answer_status"] == "ready"
    assert questions[1]["answer_kind"] == "non_graphic"
    assert questions[2]["answer_kind"] == "mixed"
    assert questions[2]["drawing_regions"] == [[700, 353, 900, 500]]
    assert questions[2]["label_anchor"] == [695, 420]
    assert questions[2]["directory_metadata"]["answer_kind"] == "mixed"
    assert questions[2]["solution_steps"] == ["步骤"]


def test_existing_exemptions_are_preserved_and_missing_filled():
    payload = merged_tree([
        merged_leaf("1", "fill", exemptions=[[300, 170, 320, 190]]),
        merged_leaf("2", "fill"),
    ])
    directory = receive_merged_template_output(payload)["question_directory"]
    assert directory["leaves"][0]["exemptions"] == [[300, 170, 320, 190]]
    assert directory["leaves"][1]["exemptions"] == []


def test_model_inline_drawing_reference_is_not_used_as_a_teacher_standard():
    payload = merged_tree([merged_leaf(
        "1", "solve", requires_drawing=True,
        drawing_regions=[[100, 100, 500, 500]], label_anchor=[90, 300],
        drawing_reference={
            "version": "drawing-reference-v1",
            "coordinate_space": "normalized_1000_v1",
            "comparison_mode": "fixed",
            "variants": [{"strokes": [{"kind": "polyline",
                                        "points": [[200, 200], [300, 300]]}]}],
            "context_regions": [],
        },
    )])
    question = receive_merged_template_output(payload)["questions"][0]
    assert "drawing_reference" not in question


def test_graphic_mapping_covers_true_none_and_false():
    payload = merged_tree([
        merged_leaf("1", "solve", requires_drawing=True,
                    drawing_regions=[[700, 353, 900, 500]], label_anchor=[695, 420]),
        merged_leaf("2", "fill", requires_drawing=None,
                    drawing_regions=[[700, 353, 900, 500]], label_anchor=[695, 420]),
        merged_leaf("3", "fill", requires_drawing=None),
        merged_leaf("4", "fill", requires_drawing=False),
    ])
    directory = receive_merged_template_output(payload)["question_directory"]
    assert [leaf["answer_kind"] for leaf in directory["leaves"]] == \
        ["mixed", "mixed", "non_graphic", "non_graphic"]
    # None 且已有定位数据：进入复核而不是静默改写（既有绑定规则）
    reasons = [issue.get("reason") for issue in directory["needs_review"] if isinstance(issue, dict)]
    assert "drawing_kind_without_directory_verdict" in reasons


def test_non_graphic_with_localized_fields_is_rejected():
    payload = merged_tree([
        merged_leaf("1", "fill", requires_drawing=False, drawing_regions=[[700, 353, 900, 500]]),
    ])
    with pytest.raises(ValueError, match="non_graphic cannot define drawing regions"):
        receive_merged_template_output(payload)


def test_invalid_requires_drawing_is_rejected():
    payload = merged_tree([merged_leaf("1", "fill", requires_drawing="yes")])
    with pytest.raises(ValueError, match="Invalid requires_drawing"):
        receive_merged_template_output(payload)


def test_unresolved_leaf_marks_review_without_inventing_text():
    payload = merged_tree([
        merged_leaf("1", "solve", summary="", text=None, answer=None, steps=None),
        merged_leaf("2", "fill", summary="题干摘要", text=None, answer="", steps=()),
    ])
    questions = receive_merged_template_output(payload)["questions"]
    assert questions[0]["answer_status"] == "needs_review"
    assert questions[0]["question_text"] == "待教师核对题干"
    assert questions[0]["standard_answer"] == ""
    assert questions[1]["answer_status"] == "needs_review"
    # 摘要绝不充当题干：缺 question_text 时如实标记待核对（v5 回归口径）。
    assert questions[1]["question_text"] == "待教师核对题干"


def test_context_summary_is_never_used_as_the_question_text():
    """v5 回归：context_summary 只服务目录结构；不得作为判分模型的题干。"""
    missing = merged_tree([merged_leaf("1", "fill", summary="简短摘要", text=None)])
    assert receive_merged_template_output(missing)["questions"][0]["question_text"] == "待教师核对题干"
    full = merged_tree([merged_leaf("1", "fill", summary="简短摘要", text="完整题干原文（含条件与数值）")])
    question = receive_merged_template_output(full)["questions"][0]
    assert question["question_text"] == "完整题干原文（含条件与数值）"
    assert question["answer_status"] == "ready"


def test_directory_review_propagates_to_every_question():
    payload = merged_tree([merged_leaf("1", "fill"), merged_leaf("2", "fill")],
                          needs_review=["分类无法确定"])
    result = receive_merged_template_output(payload)
    assert result["question_directory"]["needs_review"] == ["分类无法确定"]
    assert all(q["answer_status"] == "needs_review" for q in result["questions"])
    assert all(q["needs_review"] is True for q in result["questions"])


def test_structural_violations_propagate_from_the_directory_parser():
    payload = merged_tree([
        {"printed_label": "一", "kind": "section", "context_summary": "标题",
         "number_bbox": [10, 10, 30, 30], "question_type": None, "choice_mode": "unknown",
         "regions": [], "children": []},
    ])
    with pytest.raises(ValueError, match="not a gradable leaf"):
        receive_merged_template_output(payload)


def test_accepts_json_text_like_the_model_transport():
    payload = merged_tree([merged_leaf("1", "choice", answer="A")])
    result = receive_merged_template_output(json.dumps(payload, ensure_ascii=False))
    assert result["questions"][0]["standard_answer"] == "A"


@pytest.mark.parametrize("answer_kind,needs_review", [("drawing", False), ("mixed", True)])
def test_text_region_is_required_only_when_the_graphic_has_a_text_answer(answer_kind, needs_review):
    payload = merged_tree([merged_leaf(
        "1", "solve", answer_kind=answer_kind, requires_drawing=True, regions=[],
        drawing_regions=[[100, 100, 500, 500]], label_anchor=[90, 300],
    )])
    result = receive_merged_template_output(payload)
    assert bool(result["question_directory"]["needs_review"]) is needs_review
    assert (result["questions"][0]["answer_status"] == "needs_review") is needs_review
