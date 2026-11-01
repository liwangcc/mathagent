"""The production directory contract is pure; these tests never load app config."""

from copy import deepcopy
import json
import math

import pytest

from services.question_directory import (
    COORDINATE_SPACE,
    DIRECTORY_VERSION,
    bind_directory_answers,
    bind_drawing_locations,
    parse_question_directory,
)


def leaf(label="1", question_type="fill", *, kind="question", **kwargs):
    return {"printed_label": label, "kind": kind, "children": [],
            "number_bbox": [10, 10, 20, 20], "question_type": question_type,
            "choice_mode": "single" if question_type == "choice" else "unknown",
            "regions": [[100, 100, 200, 150]], "exemptions": [],
            "context_summary": "印刷条件", **kwargs}


def group(label, children, kind="group", **kwargs):
    return leaf(label, None, kind=kind, children=children, regions=[], **kwargs)


def tree(nodes, **kwargs):
    return {"coordinate_space": COORDINATE_SPACE, "nodes": nodes, "needs_review": [], **kwargs}


def primary_tree():
    return tree([
        group("一", [leaf("1", "choice"), leaf("2", "choice")], kind="section"),
        group("二", [group("1", [leaf(mark, kind="answer_item") for mark in "①②③"], kind="question"),
                     leaf("2", "solve"), leaf("3", "solve")], context_summary="共享网格条件"),
        leaf("三", "solve", regions=[[135, 770, 530, 880], [545, 760, 950, 885]]),
    ])


def answers(directory):
    return {"answers": [{"q_id": row["q_id"], "question_text": "含父级条件的实际题干",
                         "standard_answer": "答案" + row["q_id"], "solution_steps": "步骤1\n步骤2",
                         "answer_status": "ready"} for row in directory["leaves"]], "directory_issues": []}


def drawing_payload(directory):
    return {"coordinate_space": COORDINATE_SPACE, "questions": [
        {"question_number": row["question_number"], "answer_kind": "non_graphic",
         "drawing_regions": [], "label_anchor": None} for row in directory["leaves"]], "needs_review": []}


def test_primary_restarted_numbers_and_three_slots_have_authoritative_identity():
    payload = primary_tree()
    snapshot = deepcopy(payload)
    directory = parse_question_directory(payload)
    assert directory["version"] == DIRECTORY_VERSION
    assert not directory["needs_review"]
    assert [row["q_id"] for row in directory["leaves"]] == [
        "Q1.1", "Q1.2", "Q2.1.1", "Q2.1.2", "Q2.1.3", "Q2.2", "Q2.3", "Q3"]
    slots = directory["leaves"][2:5]
    assert len({row["parent_id"] for row in slots}) == 1
    assert slots[0]["printed_path"] == ["二", "1", "①"]
    assert "共享网格条件" in slots[0]["shared_context"]
    assert payload == snapshot
    directory["leaves"][0]["regions"][0][0] = 50
    assert payload == snapshot
    assert directory["nodes"][1]["regions"][0][0] == 100


def test_senior_continuous_25_questions_keep_existing_flat_ids():
    payload = tree([group("一", [leaf(str(n), "choice") for n in range(1, 16)], kind="section"),
                    group("二", [leaf(str(n)) for n in range(16, 26)], kind="section")])
    directory = parse_question_directory(payload)
    assert directory["flat_compatible"]
    assert not directory["needs_review"]
    assert [row["question_number"] for row in directory["leaves"]] == list(map(str, range(1, 26)))
    assert directory["leaves"][15]["printed_path"] == ["二", "16"]


def test_junior_unique_question_with_subquestions_omits_unnecessary_section_prefix():
    directory = parse_question_directory(tree([
        group("一", [leaf("1"), leaf("2")], kind="section"),
        group("二", [group("3", [leaf("(1)", "solve"), leaf("(2)", "solve")], kind="question")], kind="section"),
    ]))
    assert [row["q_id"] for row in directory["leaves"]] == ["Q1", "Q2", "Q3.1", "Q3.2"]
    assert directory["leaves"][-1]["printed_path"] == ["二", "3", "(2)"]


def test_restarted_sections_cannot_collapse_to_same_leaf():
    directory = parse_question_directory(tree([
        group("一", [leaf("1"), leaf("2")], kind="section"),
        group("二", [leaf("1"), leaf("2")], kind="section"),
    ]))
    assert [row["question_number"] for row in directory["leaves"]] == ["1.1", "1.2", "2.1", "2.2"]


def test_unprinted_comprehensive_question_is_one_provisional_leaf_not_actions():
    directory = parse_question_directory(tree([leaf(None, "solve", number_bbox=None,
                                                     regions=[[100, 100, 200, 200], [300, 100, 400, 200]])]))
    assert len(directory["leaves"]) == 1
    assert directory["leaves"][0]["q_id"] == "Q1"
    assert directory["leaves"][0]["printed_path"] == [None]
    assert directory["leaves"][0]["synthetic_label"]
    assert {issue["reason"] for issue in directory["needs_review"]} == {"unresolved_printed_label", "solve_printed_number_missing"}


def test_unprinted_independent_answer_slots_keep_parent_and_do_not_split_coordinates():
    directory = parse_question_directory(tree([group("2", [leaf(None, kind="answer_item"), leaf(None, kind="answer_item")], kind="question")]))
    assert [row["q_id"] for row in directory["leaves"]] == ["Q2.1", "Q2.2"]
    assert not directory["needs_review"]
    coordinate = parse_question_directory(tree([leaf("1", context_summary="填写一组坐标(x,y)")]))
    assert len(coordinate["leaves"]) == 1


@pytest.mark.parametrize("label,number", [("①", "1"), ("⑳", "20"), ("（２）", "2"), ("十一", "11"), ("二十五", "25"), ("第九题", "9")])
def test_number_normalization_keeps_printed_label_display_exact(label, number):
    item = parse_question_directory(tree([leaf(label)]))["leaves"][0]
    assert item["question_number"] == number
    assert item["printed_path"] == [label]


@pytest.mark.parametrize("change", [
    {"kind": "heading"}, {"kind": []}, {"children": None}, {"question_type": "drawing"},
    {"question_type": []}, {"printed_label": 1}, {"printed_label": ""}, {"context_summary": {}},
    {"regions": None}, {"exemptions": {}}, {"choice_mode": []},
])
def test_malformed_nodes_fail_closed(change):
    with pytest.raises(ValueError):
        parse_question_directory(tree([leaf(**change)]))


@pytest.mark.parametrize("bad_box", [[0, 0, 1001, 10], [20, 10, 1, 100], [False, 0, 10, 10],
                                     [0, 0, math.nan, 10], [0, 0, 10, math.inf], [0, 0, 10, 0], ["0", 0, 10, 10],
                                     [0, 0, 10**400, 10]])
def test_invalid_coordinates_fail_closed_in_all_box_fields(bad_box):
    for field in ("regions", "exemptions", "drawing_regions", "number_bbox", "question_number_bbox"):
        value = bad_box if field.endswith("bbox") else [bad_box]
        with pytest.raises(ValueError):
            parse_question_directory(tree([leaf(**{field: value})]))


def test_rejects_duplicate_siblings_heading_leaf_nested_answer_item_and_cycles():
    invalid = [tree([leaf("1"), leaf("1")]), tree([leaf("一", kind="section")]),
               tree([leaf("1", kind="answer_item", children=[leaf("2")])])]
    cyclic = group("1", [])
    cyclic["children"] = [cyclic]
    invalid.append(tree([cyclic]))
    for payload in invalid:
        with pytest.raises(ValueError):
            parse_question_directory(payload)


def test_rejects_unknown_coordinate_system_and_duplicate_json_keys():
    with pytest.raises(ValueError):
        parse_question_directory(tree([leaf()], coordinate_space="pixels"))
    with pytest.raises(ValueError, match="Duplicate JSON"):
        parse_question_directory('{"coordinate_space":"pixels","coordinate_space":"normalized_1000_v1"}')
    raw = "```json\n" + json.dumps(tree([leaf()]), ensure_ascii=False) + "\n```"
    assert parse_question_directory(raw)["leaves"][0]["q_id"] == "Q1"


def test_directory_rejects_excessive_depth_or_node_count():
    deep = leaf("1")
    for _ in range(10):
        deep = group("1", [deep])
    with pytest.raises(ValueError, match="depth"):
        parse_question_directory(tree([deep]))
    with pytest.raises(ValueError, match="Excessive"):
        parse_question_directory(tree([leaf(str(n)) for n in range(1, 182)]))


def test_bbox_aliases_are_normalized_without_rounding_or_silent_conflict():
    value = [10.125, 20.25, 30.5, 40.75]
    directory = parse_question_directory(tree([leaf(number_bbox=value, question_number_bbox=value)]))
    assert directory["leaves"][0]["question_number_bbox"] == value
    assert directory["leaves"][0]["number_bbox"] == value
    with pytest.raises(ValueError, match="Conflicting"):
        parse_question_directory(tree([leaf(number_bbox=value, question_number_bbox=[1, 2, 3, 4])]))


def test_old_structure_has_no_drawing_requirement_but_unknown_choice_needs_teacher():
    ordinary = parse_question_directory(tree([leaf("1", "solve")]))
    assert ordinary["leaves"][0]["answer_kind"] == "non_graphic"
    assert not ordinary["needs_review"]
    old_choice = leaf("1", "choice")
    del old_choice["choice_mode"]
    result = parse_question_directory(tree([old_choice]))
    assert result["leaves"][0]["choice_mode"] == "unknown"
    assert result["needs_review"][0]["reason"] == "choice_mode_unknown"


def test_mixed_drawing_keeps_single_leaf_and_exact_geometry():
    node = leaf("三", "solve", answer_kind="mixed", drawing_regions=[[135, 770, 530, 880]],
                label_anchor=[130, 825], regions=[[135, 770, 530, 880], [545, 760, 950, 885]])
    directory = parse_question_directory(tree([node]))
    assert len(directory["leaves"]) == 1
    row = directory["leaves"][0]
    assert row["q_id"] == "Q3"
    assert row["label_anchor"] == [130, 825]
    assert row["drawing_regions"] == [[135, 770, 530, 880]]
    assert row["regions"] == node["regions"]
    assert not directory["needs_review"]


def test_solver_binds_only_by_q_then_outputs_directory_order_and_leaf_counts():
    directory = parse_question_directory(primary_tree())
    payload = answers(directory)
    payload["answers"].reverse()
    snapshots = deepcopy(directory), deepcopy(payload)
    bound = bind_directory_answers(directory, payload)
    assert [row["question_number"] for row in bound] == [row["question_number"] for row in directory["leaves"]]
    for item, metadata in zip(bound, directory["leaves"]):
        assert item["standard_answer"] == "答案" + item["q_id"]
        assert item["question_no"] == metadata["question_number"]
        assert item["directory_node_id"] == metadata["node_id"]
        assert item["directory_parent_id"] == metadata["parent_id"]
        assert item["blank_count"] == item["subquestion_count"] == 1
        assert item["directory_metadata"] == metadata
        assert item["solution_steps"] == ["步骤1", "步骤2"]
        assert item["answer_status"] == "ready"
    assert (directory, payload) == snapshots


@pytest.mark.parametrize("key,value", [
    ("q_id", "Q9"), ("q_id", "Q1 "), ("q_id", 1), ("q_id", "1"),
    ("parent_id", "n001"), ("printed_path", ["一", "1"]), ("question_number", "1"),
    ("question_no", "1"), ("question_type", "fill"), ("choice_mode", "single"),
    ("blank_count", 3), ("subquestion_count", 3), ("regions", [[1, 1, 2, 2]]),
    ("answer_kind", "drawing"), ("label_anchor", [10, 10]), ("children", []),
])
def test_solver_cannot_change_any_structural_field_or_repair_id(key, value):
    directory = parse_question_directory(tree([leaf()]))
    payload = answers(directory)
    payload["answers"][0][key] = value
    with pytest.raises(ValueError):
        bind_directory_answers(directory, payload)


def test_solver_duplicate_missing_empty_and_uncertain_answers_fail_closed():
    directory = parse_question_directory(tree([leaf("1"), leaf("2")]))
    for mutation in (lambda p: p["answers"].pop(), lambda p: p["answers"].append(deepcopy(p["answers"][0])),
                     lambda p: p["answers"][0].update(standard_answer="")):
        payload = answers(directory)
        mutation(payload)
        with pytest.raises(ValueError):
            bind_directory_answers(directory, payload)
    payload = answers(directory)
    payload["answers"][0].update(answer_status="needs_review", standard_answer="")
    result = bind_directory_answers(directory, payload)
    assert result[0]["needs_review"] and result[0]["standard_answer"] == ""
    assert not result[1]["needs_review"]
    payload["directory_issues"] = ["共享条件需要教师确认"]
    assert all(row["needs_review"] for row in bind_directory_answers(directory, payload))


def test_directory_uncertainty_cannot_be_overridden_by_ready_solver():
    directory = parse_question_directory(tree([leaf(None)]))
    bound = bind_directory_answers(directory, answers(directory))
    assert bound[0]["answer_status"] == "needs_review"
    assert bound[0]["directory_issues"]


def test_leaf_does_not_inherit_or_reexpand_parent_scoring_counts():
    directory = parse_question_directory(tree([group("2", [leaf("1"), leaf("2"), leaf("3")],
                                                       kind="question", blank_count=3, subquestion_count=3)]))
    bound = bind_directory_answers(directory, answers(directory))
    assert len(bound) == 3
    assert [row["question_number"] for row in bound] == ["2.1", "2.2", "2.3"]
    assert all(row["blank_count"] == row["subquestion_count"] == 1 for row in bound)


def test_drawing_location_successful_shape_preserves_identity_and_reserved_regions():
    directory = parse_question_directory(primary_tree())
    original = deepcopy(directory)
    payload = drawing_payload(directory)
    payload["questions"][-2].update(answer_kind="drawing", drawing_regions=[[703, 353, 900, 502]], label_anchor=[700, 350])
    payload["questions"][-1].update(answer_kind="mixed", drawing_regions=[[135, 770, 530, 880]], label_anchor=[130, 825])
    result = bind_drawing_locations(directory, payload)
    assert directory == original
    assert not result["needs_review"]
    assert [row["q_id"] for row in result["leaves"]] == [row["q_id"] for row in original["leaves"]]
    assert result["leaves"][-2]["label_anchor"] == [700, 350]  # centre may be above top edge
    assert result["leaves"][-1]["regions"] == original["leaves"][-1]["regions"]
    nodes = {row["node_id"]: row for row in result["nodes"]}
    for row in result["leaves"]:
        assert nodes[row["node_id"]]["drawing_regions"] == row["drawing_regions"]
        assert nodes[row["node_id"]]["label_anchor"] == row["label_anchor"]


@pytest.mark.parametrize("mutation", [
    lambda p: p["questions"].reverse(), lambda p: p["questions"].pop(),
    lambda p: p["questions"][0].update(question_number="Q1.1"),
    lambda p: p["questions"][0].update(question_number="2.3"),
    lambda p: p["questions"][0].update(regions=[]),
    lambda p: p["questions"][-1].update(answer_kind="mixed", drawing_regions=[[20, 20, 40, 40]], label_anchor=[False, 20]),
])
def test_drawing_locator_cannot_rewrite_directory_or_supply_invalid_coordinates(mutation):
    directory = parse_question_directory(primary_tree())
    payload = drawing_payload(directory)
    mutation(payload)
    with pytest.raises(ValueError):
        bind_drawing_locations(directory, payload)


def test_drawing_kind_for_a_non_solve_item_needs_a_directory_verdict():
    """A non-solve item may be graphic only when the directory allows it.

    The directory answers whether an item wants work on a figure now, so a
    locator cannot silently turn a declined item into a drawing, and a rewrite
    that arrives without any verdict must reach the teacher.
    """
    declined = parse_question_directory(tree([leaf("1", "choice", requires_drawing=False)]))
    payload = {**drawing_payload(declined), "questions": [
        {"question_number": "1", "answer_kind": "drawing",
         "drawing_regions": [[20, 20, 40, 40]], "label_anchor": [10, 20]}]}
    with pytest.raises(ValueError):
        bind_drawing_locations(declined, payload)

    allowed = parse_question_directory(tree([leaf("1", "choice", requires_drawing=True)]))
    bound = bind_drawing_locations(allowed, {**drawing_payload(allowed), "questions": [
        {"question_number": "1", "answer_kind": "drawing",
         "drawing_regions": [[20, 20, 40, 40]], "label_anchor": [10, 20]}]})
    assert bound["leaves"][0]["answer_kind"] == "drawing"
    assert not bound["needs_review"]

    unstated = parse_question_directory(tree([leaf("1", "choice")]))
    bound = bind_drawing_locations(unstated, {**drawing_payload(unstated), "questions": [
        {"question_number": "1", "answer_kind": "drawing",
         "drawing_regions": [[20, 20, 40, 40]], "label_anchor": [10, 20]}]})
    assert bound["leaves"][0]["answer_kind"] == "drawing"
    assert [issue["reason"] for issue in bound["needs_review"]] == ["drawing_kind_without_directory_verdict"]


def test_drawing_missing_or_inside_anchor_requires_teacher_not_silent_non_graphic():
    directory = parse_question_directory(tree([leaf("1", "solve")]))
    payload = drawing_payload(directory)
    payload["questions"][0].update(answer_kind="drawing", drawing_regions=[[20, 20, 40, 40]], label_anchor=[30, 30])
    assert bind_drawing_locations(directory, payload)["needs_review"] == []
    payload["questions"][0].update(drawing_regions=[], label_anchor=None)
    reasons = {row["reason"] for row in bind_drawing_locations(directory, payload)["needs_review"]}
    assert reasons == {"drawing_region_missing", "drawing_label_anchor_missing"}


@pytest.mark.parametrize("question_type", ["choice", "fill", "solve"])
def test_positive_directory_requirement_cannot_silently_disappear(question_type):
    directory = parse_question_directory(tree([leaf("1", question_type, requires_drawing=True)]))
    before = deepcopy(directory)
    result = bind_drawing_locations(directory, drawing_payload(directory))
    assert directory == before
    assert result["leaves"][0]["requires_drawing"] is True
    assert result["leaves"][0]["question_type"] == question_type
    assert [issue["reason"] for issue in result["needs_review"]] == ["drawing_requirement_conflict"]
