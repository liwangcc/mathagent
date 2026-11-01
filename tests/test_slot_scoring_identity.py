"""Frozen physical slot identities must survive OCR -> grading -> details."""
import copy

import pytest

from services.scoring_engine import aggregate_grading_result, build_grading_units


def contract(qtype="fill", ids=None):
    ids = ids or ["7[1]", "7[2]", "7[3]"]
    return {"7": {
        "question_type": qtype, "question_text": "___ ___ ___",
        "standard_answer": "A；B；C", "answer_slots": ["A", "B", "C"][:len(ids)],
        "slot_ids": ids, "region_kinds": ["answer"] * len(ids),
        "regions": [[10, 10 + i * 100, 100, 90 + i * 100] for i in range(len(ids))],
        "blank_count": len(ids), "subquestion_count": len(ids),
        "max_score": 9, "subquestion_scores": [3] * len(ids),
    }}


def row(slots=None):
    return {"question_no": "7", "student_answer": "display must not be scored",
            "answer_state": "visible", "student_slots": slots if slots is not None else [
                {"slot_id": "7[3]", "student_answer": "C", "answer_state": "visible"},
                {"slot_id": "7[1]", "student_answer": "A\nstep;still first", "answer_state": "visible"},
                {"slot_id": "7[2]", "student_answer": "", "answer_state": "blank"},
            ]}


def test_fill_uses_exact_slot_ids_and_keeps_middle_blank_and_multiline():
    units = build_grading_units([row()], contract(), "exam")
    assert [(u["key"], u["student_answer"], u["answer_state"]) for u in units] == [
        ("7[1]", "A\nstep;still first", "visible"),
        ("7[2]", "", "blank"), ("7[3]", "C", "visible"),
    ]
    assert [u["slot_id"] for u in units] == ["7[1]", "7[2]", "7[3]"]
    assert [u["standard_answer"] for u in units] == ["A", "B", "C"]


@pytest.mark.parametrize("category", ["daily", "exam"])
def test_solve_sparse_ids_do_not_renumber_and_states_are_local(category):
    slots = [
        {"slot_id": "7[5]", "student_answer": "?", "answer_state": "unreadable"},
        {"slot_id": "7[2]", "student_answer": "A\nB;C", "answer_state": "visible"},
    ]
    units = build_grading_units([row(slots)], contract("solve", ["7[2]", "7[5]"]), category)
    assert [(u["key"], u["slot_id"], u["student_answer"], u["answer_state"]) for u in units] == [
        ("7(2)", "7[2]", "A\nB;C", "visible"),
        ("7(5)", "7[5]", "[无法识别]", "unreadable"),
    ]
    assert [u["standard_answer"] for u in units] == ["A", "B"]
    assert [u["max_score"] for u in units] == ([3, 3] if category == "exam" else [None, None])


@pytest.mark.parametrize("ids, key", [(["7[1]"], "7"), (["7[5]"], "7(5)")])
def test_single_solve_slot_keeps_legacy_key_only_for_original_first_slot(ids, key):
    units = build_grading_units([row([{"slot_id": ids[0], "student_answer": "A;B\nC", "answer_state": "visible"}])], contract("solve", ids), "daily")
    assert len(units) == 1
    assert units[0]["key"] == key
    assert units[0]["slot_id"] == ids[0]
    assert units[0]["student_answer"] == "A;B\nC"


@pytest.mark.parametrize("fault", ["duplicate", "unknown", "missing", "scalar", "duplicate_parent", "child", "parent_and_child"])
def test_invalid_slot_identity_cannot_be_overridden_by_correct_model(fault):
    from services.scoring_engine import normalize_ocr_questions
    question = row()
    questions = [question]
    if fault == "duplicate":
        question["student_slots"].append(copy.deepcopy(question["student_slots"][0]))
    elif fault == "unknown":
        question["student_slots"][0]["slot_id"] = "7[8]"
    elif fault == "missing":
        question["student_slots"].pop()
    elif fault == "scalar":
        question.pop("student_slots")
    elif fault == "duplicate_parent":
        questions.append(copy.deepcopy(question))
    elif fault == "child":
        question["question_no"] = "7[1]"
    else:
        child = copy.deepcopy(question)
        child["question_no"] = "7(1)"
        questions.append(child)
    normalized = normalize_ocr_questions(questions, contract())
    if fault in {"duplicate_parent", "child", "parent_and_child"}:
        assert normalized[0].get("slot_identity_error")
    units = build_grading_units(questions, contract(), "exam")
    assert all(u.get("slot_identity_error") for u in units)
    result = aggregate_grading_result(units, {u["key"]: {"result": "correct"} for u in units}, "exam", "test")
    assert result["score"] is None
    assert result["needs_review_count"] == 1
    assert result["details"][0]["slot_identity_error"]
    assert all(s["result"] == "needs_review" for s in result["details"][0]["blank_results"])


def test_details_and_blank_results_preserve_slot_evidence():
    units = build_grading_units([row()], contract(), "exam")
    result = aggregate_grading_result(units, {u["key"]: {"result": "correct"} for u in units}, "exam", "test")
    detail = result["details"][0]
    assert detail["student_slots"] == units[0]["student_slots"]
    assert detail["slot_ids"] == ["7[1]", "7[2]", "7[3]"]
    assert [b["slot_id"] for b in detail["blank_results"]] == detail["slot_ids"]
    assert [b["student_answer"] for b in detail["blank_results"]] == ["A\nstep;still first", "", "C"]
    assert [b["answer_state"] for b in detail["blank_results"]] == ["visible", "blank", "visible"]
    assert [u["slot_id"] for u in detail["grading_units"]] == detail["slot_ids"]


@pytest.mark.parametrize("qtype", ["fill", "solve"])
def test_unified_grader_only_sends_visible_slots_and_locally_gates_others(monkeypatch, qtype):
    import asyncio
    from services import grader
    sent = []

    class ImmediateQueue:
        async def submit(self, coro_factory, **kwargs):
            return await coro_factory()

    async def judge(questions, **kwargs):
        sent.extend(questions)
        return {q["question_no"]: {"result": "correct"} for q in questions}

    monkeypatch.setattr(grader, "get_grading_queue", ImmediateQueue)
    monkeypatch.setattr(grader, "judge_paper", judge)
    question = row()
    question["student_slots"][0].update(student_answer="?", answer_state="unreadable")
    result = asyncio.run(grader.grade_paper_unified({"questions": [question]}, contract(qtype), "test", 1, category="exam"))
    assert len(sent) == 1
    assert sent[0]["slot_id"] == "7[1]"
    assert sent[0]["student_answer"] == "A\nstep;still first"
    details = result["details"][0]["blank_results"] if qtype == "fill" else result["details"]
    assert [d["result"] for d in details] == ["correct", "wrong", "needs_review"]
    assert result["score"] is None


def test_unified_identity_error_never_dispatches_even_with_valid_visible_slots(monkeypatch):
    import asyncio
    from services import grader

    def forbidden():
        raise AssertionError("identity error must not dispatch")

    monkeypatch.setattr(grader, "get_grading_queue", forbidden)
    question = row()
    question["slot_identity_error"] = "upstream ownership rejected"
    result = asyncio.run(grader.grade_paper_unified({"questions": [question]}, contract(), "test", 1, category="exam"))
    assert result["needs_review_count"] == 1
    assert result["score"] is None


@pytest.mark.parametrize("kind", ["drawing", "mixed"])
@pytest.mark.parametrize("contracted", [False, True])
def test_typed_graphic_slots_remain_one_whole_question(kind, contracted):
    answer_map = contract("solve", ["7[2]", "7[5]"])
    entry = answer_map["7"]
    entry.update(answer_kind=kind, region_kinds=["drawing", "answer" if kind == "mixed" else "drawing"],
                 answer_slots=["", "B" if kind == "mixed" else ""], subquestion_count=1, subquestion_scores=[])
    question = row([{"slot_id": "7[5]", "student_answer": "B\nwork;more", "answer_state": "visible"}] if kind == "mixed" else [])
    if not contracted:
        entry.pop("slot_ids")
        question.pop("student_slots")
    units = build_grading_units([question], answer_map, "exam")
    assert len(units) == 1
    assert units[0]["key"] == "7"
    assert units[0]["max_score"] == 9
    assert units[0]["answer_slots"] == entry["answer_slots"]
    if contracted:
        assert units[0]["student_slots"] == question["student_slots"]
        assert "7[2]" in units[0]["scope_hint"]
