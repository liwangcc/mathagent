"""V3 Max-owned answer slots bind to printed keys only, never by position."""
import copy
import json

import pytest

from services import template_max_owned as owned
from services import template_one_three as one_three
from services import template_directory_pipeline as pipeline


def hierarchy(count=1):
    return one_three.receive_hierarchy({"contract_version": 3, "questions": [
        {"question_number": str(index), "printed_path": ["基础练习", str(index)]}
        for index in range(1, count + 1)]})


def manifest(count=1):
    return one_three.question_contract(hierarchy(count))


def key(part="", anchor="", blank_index=0):
    return {"part": part, "anchor": anchor, "blank_index": blank_index}


def slot(bbox, *, part="", anchor="", blank_index=0):
    return {"kind": "answer", "bbox": list(bbox), "answer_key": key(part, anchor, blank_index)}


def drawing(bbox):
    return {"kind": "drawing", "bbox": list(bbox), "answer_key": None}


def locator_row(number, regions, *, question_type="fill", choice_mode="unknown",
                label_anchor=None, bbox=(1, 1, 60, 60)):
    return {"question_number": number, "question_number_bbox": list(bbox),
            "question_type": question_type, "choice_mode": choice_mode,
            "regions": list(regions), "label_anchor": label_anchor}


def locator_payload(rows, needs_review=None):
    return {"coordinate_space": "normalized_1000_v1", "questions": list(rows),
            "needs_review": list(needs_review or [])}


def solver_payload(one_manifest, entries):
    answers = []
    for row in one_manifest:
        standard, items = entries.get(row["q_id"], ("答案", []))
        answers.append({"q_id": row["q_id"], "standard_answer": standard,
                        "answer_items": [{"answer_key": item_key, "answer": answer} for item_key, answer in items],
                        "solution_steps": [], "visual_facts": [], "uncertain": []})
    return {"answers": answers}


def locate(one_manifest, rows, needs_review=None):
    return owned.receive_locator(locator_payload(rows, needs_review), one_manifest)


def solve(one_manifest, entries):
    return owned.receive_solver(solver_payload(one_manifest, entries), one_manifest)


ANCHOR = "□+□=8"


def test_max_parts_are_read_from_image_not_flash_directory():
    m = [{"q_id": "Q1", "printed_path": ["计算", "2."]}]
    locator = locate(m, [locator_row("1", [
        slot((10, 10, 60, 30), part="(1)"),
        slot((10, 40, 60, 60), part="(2)")])])
    solver = solve(m, {"Q1": ("两个小问", [
        (key(part="(2)"), "5"), (key(part="(1)"), "3")])})
    bound = owned.bind_answers(locator, solver)["answers"][0]
    assert bound["answer_slots"] == ["3", "5"]
    assert bound["answer_status"] == "ready"
    assert bound["slot_binding"]["region_keys"] == [key(part="(1)"), key(part="(2)")]


def test_multi_slot_reverse_order_solver_binds_by_printed_key():
    m = manifest()
    locator = locate(m, [locator_row("1", [
        slot((10, 10, 60, 30), anchor=ANCHOR, blank_index=1),
        slot((10, 40, 60, 60), anchor=ANCHOR, blank_index=2)])])
    solver = solve(m, {"Q1": ("8的两个加数", [
        (key(anchor=ANCHOR, blank_index=2), "5"),
        (key(anchor=ANCHOR, blank_index=1), "3")])})
    bound = owned.bind_answers(locator, solver)["answers"][0]
    assert locator["slot_source"] == owned.SLOT_SOURCE == "max_locator_v3"
    assert locator["questions"][0]["slot_ids"] == ["1[1]", "1[2]"]
    assert bound["answer_slots"] == ["3", "5"]
    assert bound["slot_ids"] == ["1[1]", "1[2]"]
    assert bound["slot_binding"]["mapping"] == [
        {"slot_id": "1[1]", "solver_index": 1, "method": "printed_key"},
        {"slot_id": "1[2]", "solver_index": 0, "method": "printed_key"}]
    assert bound["slot_binding"]["solver_items"] == solver["answers"][0]["answer_items"]
    assert bound["slot_binding"]["region_keys"] == [
        key(anchor=ANCHOR, blank_index=1), key(anchor=ANCHOR, blank_index=2)]
    assert bound["slot_binding"]["unbound_items"] == []
    assert bound["answer_status"] == "ready" and bound["answer_review_reasons"] == []
    # binding mutates a copy: the solver draft keeps its own order and items
    assert "answer_items" in solver["answers"][0]
    assert [item["answer"] for item in solver["answers"][0]["answer_items"]] == ["5", "3"]


def test_key_formatting_equivalence_binds_but_semantic_rewrites_stay_unbound():
    m = manifest()
    equivalent = owned.bind_answers(
        locate(m, [locator_row("1", [slot((10, 10, 60, 30), anchor="x＋□＝8", blank_index=1)])]),
        solve(m, {"Q1": ("x=3", [(key(anchor="x + □ = 8", blank_index=1), "3")])}))["answers"][0]
    assert equivalent["answer_slots"] == ["3"]
    assert equivalent["slot_binding"]["mapping"][0]["method"] == "printed_key"
    for other in ("y+□=8", "x-□=8", "x+□=9", "x+□=8abc"):
        draft = owned.bind_answers(
            locate(m, [locator_row("1", [slot((10, 10, 60, 30), anchor="x+□=8", blank_index=1)])]),
            solve(m, {"Q1": ("x=3", [(key(anchor=other, blank_index=1), "3")])}))["answers"][0]
        assert draft["answer_slots"] == [""]
        assert draft["answer_status"] == "needs_review"
        assert [item["answer_key"]["anchor"] for item in draft["slot_binding"]["unbound_items"]] == [other]


def test_duplicate_key_rejected_on_both_branches():
    m = manifest()
    with pytest.raises(ValueError, match="重复"):
        locate(m, [locator_row("1", [
            slot((10, 10, 60, 30), anchor="□＋□＝8", blank_index=1),
            slot((10, 40, 60, 60), anchor="□ + □ = 8", blank_index=1)])])
    with pytest.raises(ValueError, match="重复"):
        solve(m, {"Q1": ("8", [
            (key(anchor="□＋□＝8", blank_index=1), "3"),
            (key(anchor="□ + □ = 8", blank_index=1), "5")])})
    with pytest.raises(ValueError, match="重复"):
        solve(m, {"Q1": ("x=3", [(key(), "3"), (key(), "3")])})


def test_missing_middle_slot_stays_empty_and_needs_review():
    m = manifest()
    regions = [slot((10, 10, 60, 25), anchor="□+□+□=12", blank_index=index) for index in (1, 2, 3)]
    locator = locate(m, [locator_row("1", regions)])
    assert locator["questions"][0]["answer_slot_count"] == 3
    bound = owned.bind_answers(locator, solve(m, {"Q1": ("三个加数", [
        (key(anchor="□+□+□=12", blank_index=1), "2"),
        (key(anchor="□+□+□=12", blank_index=3), "7")])}))["answers"][0]
    assert bound["answer_slots"] == ["2", "", "7"]
    assert bound["slot_binding"]["mapping"][1] == {"slot_id": "1[2]", "solver_index": None, "method": "unbound"}
    assert bound["slot_binding"]["unbound_items"] == []
    assert bound["answer_slot_count"] == 3
    assert bound["answer_status"] == "needs_review"
    assert bound["answer_review_reasons"] == ["作答位 1[2] 的印刷位置键未匹配到标准答案，请核对"]


def test_unknown_answer_item_remains_unbound_draft():
    m = manifest()
    locator = locate(m, [locator_row("1", [slot((10, 10, 60, 30), anchor=ANCHOR, blank_index=1)])])
    solver = solve(m, {"Q1": ("x=3", [
        (key(anchor=ANCHOR, blank_index=1), "3"),
        (key(anchor="□×4", blank_index=1), "12")])})
    bound = owned.bind_answers(locator, solver)["answers"][0]
    assert bound["answer_slots"] == ["3"]
    assert bound["answer_slot_count"] == 1
    assert bound["slot_binding"]["unbound_items"] == [
        {"answer_key": key(anchor="□×4", blank_index=1), "answer": "12"}]
    assert bound["answer_status"] == "needs_review"
    assert bound["answer_review_reasons"] == ["解题存在未绑定的答案项，请核对作答框和答案"]


def test_drawing_and_mixed_slots_are_empty_non_text_slots():
    m = manifest()
    locator = locate(m, [locator_row("1", [
        drawing((100, 100, 300, 300)),
        slot((400, 100, 460, 130), anchor="", blank_index=0)], label_anchor=[50, 110])])
    row = locator["questions"][0]
    assert row["requires_drawing"] is True and row["answer_kind"] == "mixed"
    assert row["region_kinds"] == ["drawing", "answer"]
    assert row["drawing_regions"] == [[100.0, 100.0, 300.0, 300.0]]
    assert row["answer_slot_count"] == 2 and row["answer_area_count"] == 1
    bound = owned.bind_answers(locator, solve(m, {"Q1": ("x=3；作图见标准答案", [
        (key(), "3")])}))["answers"][0]
    assert bound["answer_slots"] == ["", "3"]
    assert bound["slot_binding"]["mapping"] == [
        {"slot_id": "1[1]", "solver_index": None, "method": "drawing"},
        {"slot_id": "1[2]", "solver_index": 0, "method": "printed_key"}]
    assert bound["slot_binding"]["unbound_items"] == []
    assert bound["answer_status"] == "ready" and bound["answer_review_reasons"] == []


def test_drawing_region_contract_rejects_text_keys_and_missing_anchor():
    m = manifest()
    with pytest.raises(ValueError, match="作图区不绑定文字答案键"):
        locate(m, [locator_row("1", [{"kind": "drawing", "bbox": [100, 100, 300, 300],
                                      "answer_key": key()}], label_anchor=[50, 110])])
    with pytest.raises(ValueError, match="作图题号标记坐标无效"):
        locate(m, [locator_row("1", [drawing((100, 100, 300, 300))])])
    # A valid auxiliary label point is accepted even when it is inside the
    # drawing bbox; the bbox, not the label point, owns answer geometry.
    located = locate(m, [locator_row("1", [drawing((100, 100, 300, 300))],
                                    label_anchor=[100, 110])])
    assert located["questions"][0]["label_anchor"] == [100, 110]
    with pytest.raises(ValueError, match="普通文字题的作图标记必须为空"):
        locate(m, [locator_row("1", [slot((10, 10, 60, 30))], label_anchor=[10, 10])])
    with pytest.raises(ValueError, match="空位序号无效"):
        locate(m, [locator_row("1", [slot((10, 10, 60, 30), anchor="□+□=8", blank_index=0)])])
    with pytest.raises(ValueError, match="模型文本字段无效"):
        locate(m, [locator_row("1", [slot((10, 10, 60, 30), part=2)])])


def test_all_drawing_question_needs_no_answer_items():
    m = manifest()
    locator = locate(m, [locator_row("1", [drawing((100, 100, 300, 300)),
                                           drawing((320, 100, 500, 300))], label_anchor=[50, 110])])
    row = locator["questions"][0]
    assert row["requires_drawing"] is True and row["answer_kind"] == "drawing"
    assert row["answer_area_count"] == 0 and row["answer_slot_count"] == 2
    bound = owned.bind_answers(locator, solve(m, {"Q1": ("作图：画出两个三角形", [])}))["answers"][0]
    assert bound["answer_slots"] == ["", ""]
    assert [entry["method"] for entry in bound["slot_binding"]["mapping"]] == ["drawing", "drawing"]
    assert bound["slot_binding"]["solver_items"] == []
    assert bound["slot_binding"]["unbound_items"] == []
    assert bound["answer_status"] == "ready" and bound["answer_review_reasons"] == []


def test_choice_question_has_exactly_one_answer_slot():
    m = manifest()
    locator = locate(m, [locator_row("1", [slot((10, 10, 60, 30))],
                                     question_type="choice", choice_mode="single")])
    row = locator["questions"][0]
    assert row["question_type"] == "choice" and row["answer_kind"] == "non_graphic"
    assert row["requires_drawing"] is False and row["label_anchor"] is None
    assert row["slot_ids"] == ["1[1]"] and row["answer_slot_count"] == 1
    bound = owned.bind_answers(locator, solve(m, {"Q1": ("B", [(key(), "B")])}))["answers"][0]
    assert bound["answer_slots"] == ["B"] and bound["answer_status"] == "ready"
    two_slots = [slot((10, 10, 60, 30), anchor="A□", blank_index=1),
                 slot((10, 40, 60, 60), anchor="B□", blank_index=2)]
    with pytest.raises(ValueError, match="每道独立选择题对应一个文字答案槽"):
        locate(m, [locator_row("1", two_slots, question_type="choice", choice_mode="single")])
    with pytest.raises(ValueError, match="每道独立选择题对应一个文字答案槽"):
        locate(m, [locator_row("1", [drawing((100, 100, 300, 300))], question_type="choice",
                               choice_mode="single", label_anchor=[50, 110])])


def test_v3_choice_mode_unknown_is_flagged_by_merge_branches():
    h = hierarchy()
    m = one_three.question_contract(h)
    locator = one_three.max_owned.receive_locator(locator_payload(
        [locator_row("1", [slot((10, 10, 60, 30))], question_type="choice", choice_mode="unknown")]), m)
    solver = one_three.receive_solver(solver_payload(m, {"Q1": ("C", [(key(), "C")])}), h)
    assert solver["answers"][0]["answer_items"] == [{"answer_key": key(), "answer": "C"}]
    merged = one_three.merge_branches(
        locator, solver, one_three.receive_transcription(
            {"questions": [{"q_id": "Q1", "question_text": "", "uncertain": []}]}, h))
    answer = merged["answers"][0]
    assert answer["question_text"] == ""
    assert answer["answer_slots"] == ["C"]
    assert answer["answer_review_reasons"] == ["选择题单选/多选类型待教师确认"]
    assert answer["answer_status"] == "needs_review"


def test_manifest_carries_no_flash_slot_counts_in_any_branch():
    forbidden = ["answer_slot_count", "answer_area_count", "slot_ids", "area_kinds",
                 "answer_kind", "requires_drawing", "answer_regions", "region_cues", "cue", "subquestions"]
    variants = [hierarchy(), hierarchy(2), hierarchy(25)]
    for one_hierarchy in variants:
        rows = one_three.question_contract(one_hierarchy)
        assert all(set(row) == {"q_id", "printed_path"} for row in rows)
        blob = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        assert not any(token in blob for token in forbidden)
        columns = [{"column_index": 0, "x1_1000": 0, "x2_1000": 1000}]
        locator_prompt = owned.locator_prompt(rows, width=1000, height=1000, columns=columns)
        solve_prompt, text_prompt = owned.build_prompts(rows, education_stage="junior")
        for prompt in (locator_prompt, solve_prompt, text_prompt):
            assert blob in prompt
            assert "slot_ids" not in prompt and "answer_slot_count" not in prompt
        assert '"answer_key"' in locator_prompt and '"regions"' in locator_prompt
        assert '"answer_key"' in solve_prompt


def test_v3_seams_delegate_to_max_owned():
    h = hierarchy()
    m = one_three.question_contract(h)
    raw = locator_payload([locator_row("1", [slot((10, 10, 60, 30))])])
    assert pipeline._validate_one_three_locator(raw, h) == owned.receive_locator(raw, m)
    columns = [{"column_index": 0, "x1_1000": 0, "x2_1000": 1000}]
    assert pipeline._parallel_template_prompts(h, width=1000, height=1000, columns=columns) == (
        owned.locator_prompt(m, width=1000, height=1000, columns=columns), "")
    assert one_three.build_prompts(h, education_stage="junior") == owned.build_prompts(m, education_stage="junior")
    payload = solver_payload(m, {"Q1": ("x=3", [(key(), "3")])})
    solver = one_three.receive_solver(copy.deepcopy(payload), h)
    assert solver == owned.receive_solver(payload, m)
    locator = one_three.max_owned.receive_locator(locator_payload([locator_row("1", [slot((10, 10, 60, 30))])]), m)
    merged = one_three.merge_branches(locator, solver, {"questions": [
        {"q_id": "Q1", "question_text": "计算 x+□=8", "uncertain": []}]})
    bound = owned.bind_answers(locator, solver)
    assert merged["answers"][0]["answer_slots"] == bound["answers"][0]["answer_slots"] == ["3"]
    assert "answer_items" not in merged["answers"][0]
    assert merged["answers"][0]["question_text"] == "计算 x+□=8"


def test_v3_transcription_requires_every_q_id():
    h = hierarchy(3)
    full = {"questions": [{"q_id": q_id, "question_text": "正文", "uncertain": []}
                          for q_id in ("Q1", "Q2", "Q3")]}
    assert one_three.receive_transcription(copy.deepcopy(full), h) == full
    with pytest.raises(ValueError, match="缺失"):
        one_three.receive_transcription({"questions": full["questions"][:2]}, h)
    with pytest.raises(ValueError, match="未知或重复"):
        one_three.receive_transcription({"questions": full["questions"] + [
            {"q_id": "Q4", "question_text": "正文", "uncertain": []}]}, h)
    text_prompt = one_three.build_prompts(h, education_stage="junior")[1]
    assert "逐项返回全部q_id" in text_prompt
    assert "单选、多选整题省略" not in text_prompt


def test_transcription_asks_for_plain_text_math_not_latex():
    # Teachers edit question_text as raw text; LaTeX shows up as source there.
    text_prompt = owned.build_prompts(manifest(), education_stage="primary")[1]
    assert "分数写a/b" in text_prompt
    assert "不用LaTeX命令或$定界符" in text_prompt


def test_legacy_v2_slot_contract_is_not_mixed_with_v3():
    legacy = one_three.receive_hierarchy({"contract_version": 3, "questions": [
        {"question_number": "1", "printed_path": ["基础练习", "1"]}]})
    legacy["question_identity_version"] = 2
    legacy["nodes"][0].update({"raw_label": "1", "cue": "计算下列各题",
                               "answer_slot_count": 2, "area_kinds": ["answer", "drawing"]})
    assert one_three.slot_contract(legacy) == [{"q_id": "Q1", "printed_label": "1",
                                                "cue": "计算下列各题", "slots": [
                                                    {"slot_id": "1[1]", "answer_type": "answer"},
                                                    {"slot_id": "1[2]", "answer_type": "drawing"}]}]
    legacy_solve, legacy_text = one_three.build_prompts(legacy, education_stage="junior")
    assert '"slot_id":"1[1]"' in legacy_solve
    assert "answer_key" not in legacy_solve and "answer_key" not in legacy_text
    assert '"单选题省略' not in legacy_text and "单选、多选整题省略" not in legacy_text
    legacy_answer = {"q_id": "Q1", "standard_answer": "x=3",
                     "answer_slots": [{"slot_id": "1[1]", "answer": "3"},
                                      {"slot_id": "1[2]", "answer": ""}],
                     "solution_steps": [], "visual_facts": [], "uncertain": []}
    assert one_three.receive_solver({"answers": [copy.deepcopy(legacy_answer)]}, legacy)["answers"][0]["answer_slots"] == ["3", ""]

    v3_hierarchy = hierarchy()
    v3_solve, v3_text = one_three.build_prompts(v3_hierarchy, education_stage="junior")
    assert "答案位置用answer_key标识" in v3_solve and '"answer_key"' in v3_solve
    assert "slot_id" not in v3_solve and "slot_id" not in v3_text
    assert "逐项返回全部q_id" in v3_text and "answer_key" not in v3_text
    with pytest.raises(ValueError, match="独立解题字段无效"):
        one_three.receive_solver({"answers": [copy.deepcopy(legacy_answer)]}, v3_hierarchy)
    with pytest.raises(ValueError, match="不接收完整题干或题型"):
        one_three.receive_solver(solver_payload(one_three.question_contract(v3_hierarchy),
                                                {"Q1": ("x=3", [(key(), "3")])}), legacy)
    answered = one_three.receive_solver(solver_payload(one_three.question_contract(v3_hierarchy),
                                                      {"Q1": ("x=3", [(key(), "3")])}), v3_hierarchy)
    assert answered["answers"][0]["answer_items"] == [{"answer_key": key(), "answer": "3"}]
    assert answered["slot_source"] == owned.SLOT_SOURCE


def test_continuous_solve_region_keeps_multiple_quantities_in_one_value():
    m = manifest()
    locator = locate(m, [locator_row("1", [slot((10, 10, 300, 300), anchor="", blank_index=0)])])
    row = locator["questions"][0]
    assert row["slot_ids"] == ["1[1]"] and row["answer_area_count"] == 1
    bound = owned.bind_answers(locator, solve(m, {"Q1": ("x=3，y=-1", [(key(), "x=3，y=-1")])}))["answers"][0]
    assert bound["answer_slots"] == ["x=3，y=-1"]
    assert bound["answer_slot_count"] == 1 and bound["slot_binding"]["unbound_items"] == []
    assert bound["answer_status"] == "ready"
    with pytest.raises(ValueError, match="重复"):
        solve(m, {"Q1": ("x=3，y=-1", [(key(), "3"), (key(), "-1")])})


def test_bind_rejects_mismatched_contract_and_identity():
    m = manifest()
    locator = locate(m, [locator_row("1", [slot((10, 10, 60, 30))])])
    solver = solve(m, {"Q1": ("x=3", [(key(), "3")])})
    without_locator_version = copy.deepcopy(locator)
    without_locator_version.pop("slot_source")
    with pytest.raises(ValueError, match="合同版本不一致"):
        owned.bind_answers(without_locator_version, solver)
    without_solver_version = copy.deepcopy(solver)
    without_solver_version.pop("slot_source")
    with pytest.raises(ValueError, match="合同版本不一致"):
        owned.bind_answers(locator, without_solver_version)
    two = manifest(2)
    with pytest.raises(ValueError, match="身份不一致"):
        owned.bind_answers(locator, solve(two, {"Q1": ("x=3", [(key(), "3")]),
                                               "Q2": ("x=4", [(key(), "4")])}))


def test_locator_rejects_flash_supplied_slot_fields():
    m = manifest()
    extra_field = locator_payload([locator_row("1", [slot((10, 10, 60, 30))])])
    extra_field["answer_slot_count"] = 1
    with pytest.raises(ValueError, match="字段或坐标系无效"):
        owned.receive_locator(extra_field, m)
    with pytest.raises(ValueError, match="题目字段无效"):
        locate(m, [{**locator_row("1", [slot((10, 10, 60, 30))]), "slot_ids": ["1[1]"]}])
    with pytest.raises(ValueError, match="作答区域字段或类型无效"):
        locate(m, [locator_row("1", [{**slot((10, 10, 60, 30)), "slot_id": "1[1]"}])])
    located = locate(m, [locator_row("1", [slot((10, 10, 60, 30))])])
    assert located["questions"][0]["slot_ids"] == ["1[1]"]
    assert located["questions"][0]["answer_slot_count"] == 1
