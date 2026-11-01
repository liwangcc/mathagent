import asyncio

import pytest

from services.grading_contract import (
    AnswerSpec,
    finalize_answer_spec_for_category,
    normalize_choice_answer,
)
from services.grader import _detect_answer_alignment_shift, grade_paper_unified
from services.analyzer import _normalize_judged
from services.flash_grading import build_text_messages


class ImmediateQueue:
    async def submit(self, coro_factory, **_kwargs):
        return await coro_factory()


@pytest.mark.parametrize("slots", [["2", "", "2"], '2\n\n2', '["2", "", "2"]'])
def test_template_answer_positions_survive_save_and_reopen(tmp_path, slots):
    from services.template_session import TemplateSession

    question = {"question_no": "1", "directory_node_id": "n001", "question_type": "fill",
                "blank_count": 3, "answer_slots": slots, "standard_answer": "2；；2"}
    template = TemplateSession("slot-positions", str(tmp_path), teacher_id=1)
    template.add_page(0, [question])
    template.save_meta()
    restored = TemplateSession.load_meta("slot-positions", str(tmp_path)).pages[0]["questions"][0]
    assert restored["answer_slots"] == ["2", "", "2"]
    with pytest.raises(ValueError, match="答案位"):
        finalize_answer_spec_for_category(restored, "daily")
    restored["answer_slots"][1] = "3"
    assert finalize_answer_spec_for_category(restored, "daily").answer_slots == ["2", "3", "2"]


def test_grading_prompt_keeps_ocr_tolerance_but_delegates_equivalence_to_model():
    prompt = build_text_messages(
        [{
            "question_no": "17",
            "question_text": "解答题",
            "standard_answer": "2",
        }],
        "detailed",
    )[0]["content"]

    assert "同一所求答案的多步解答" in prompt
    assert "学生写出的最终结果正确" in prompt
    assert "只有中间极少量孤立的数字或符号偏差" in prompt
    assert "系统数值归一" not in prompt
    assert "等值核对未命中" not in prompt
    assert "partial 只适用于" not in prompt
    assert "wrong/partial填写具体判分理由、简短提示、完整正确解答和最终答案" in prompt

    normalized = _normalize_judged({
        "result": "correct",
        "explanation": "冗余解析",
        "hint": "冗余提示",
        "model_answer": "冗余过程",
        "standard_answer": "2",
    })
    assert normalized == {
        "result": "correct",
        "explanation": "",
        "hint": "",
        "model_answer": "",
        "standard_answer": "",
    }


def _judged(result: str, *, standard_answer: str = "") -> dict:
    return {
        "result": result,
        "explanation": "",
        "hint": "",
        "model_answer": "",
        "standard_answer": standard_answer,
    }


def test_answer_spec_preserves_exam_scoring_and_fill_structure():
    fill = AnswerSpec.model_validate({
        "question_no": "8",
        "question_text": "x=___，y=___",
        "question_type": "fill",
        "standard_answer": "2；3",
        "max_score": 5,
        "blank_count": 2,
        "answer_slots": ["2", "3"],
    })
    solve = AnswerSpec.model_validate({
        "question_no": "17",
        "question_text": "解答题",
        "question_type": "solve",
        "standard_answer": "略",
        "subquestion_count": 2,
        "subquestion_scores": [5, 7, 0],
    })

    fill_entry = fill.to_answer_map_entry()
    solve_entry = solve.to_answer_map_entry()
    assert fill_entry["max_score"] == 5.0
    assert fill_entry["blank_count"] == 2
    assert fill_entry["answer_slots"] == ["2", "3"]
    assert solve_entry["subquestion_count"] == 2
    assert solve_entry["subquestion_scores"] == [5.0, 7.0, 0.0]


def test_answer_spec_rejects_invalid_scoring_values():
    with pytest.raises(ValueError):
        AnswerSpec.model_validate({
            "question_no": "1", "question_type": "choice", "max_score": -1,
        })
    with pytest.raises(ValueError):
        AnswerSpec.model_validate({
            "question_no": "1", "question_type": "fill", "blank_count": 0,
        })
    with pytest.raises(ValueError):
        AnswerSpec.model_validate({
            "question_no": "1", "question_type": "solve", "subquestion_scores": [5, float("nan")],
        })


def test_exam_confirmation_applies_defaults_and_rejects_all_zero_solve():
    choice = finalize_answer_spec_for_category({
        "question_no": "1", "question_type": "choice", "choice_mode": "single", "standard_answer": "A",
    }, "exam")
    solve = finalize_answer_spec_for_category({
        "question_no": "17", "question_type": "solve", "subquestion_count": 3,
    }, "exam")

    assert choice.max_score == 5.0
    assert solve.subquestion_scores == [5.0, 7.0, 8.0]
    with pytest.raises(ValueError, match="至少要有一个分值大于 0 的小问"):
        finalize_answer_spec_for_category({
            "question_no": "18", "question_type": "solve",
            "subquestion_scores": [0, 0, 0],
        }, "exam")


def test_choice_confirmation_canonicalizes_wrapped_option_and_rejects_non_option():
    canonical = finalize_answer_spec_for_category({
        "question_no": "1", "question_type": "choice", "choice_mode": "single", "standard_answer": "答案：d",
    }, "daily")
    assert canonical.standard_answer == "D"


def test_removed_review_category_is_rejected_by_contract_and_grader():
    with pytest.raises(ValueError, match="daily.*exam"):
        finalize_answer_spec_for_category({
            "question_no": "1",
            "question_type": "choice",
            "choice_mode": "single",
            "standard_answer": "A",
        }, "review")

    with pytest.raises(ValueError, match="daily.*exam"):
        asyncio.run(grade_paper_unified(
            paper={"questions": []},
            answer_map={},
            student_name="学生",
            teacher_id=1,
            category="review",
        ))


def test_unreadable_ocr_answer_requires_teacher_review_without_model_call(monkeypatch):
    from services import grader

    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("unreadable OCR must not be graded automatically")

    monkeypatch.setattr(grader, "judge_paper", must_not_run)
    result = asyncio.run(grade_paper_unified(
        paper={
            "image_path": "",
            "questions": [{
                "question_no": "1",
                "student_answer": "[无法识别]",
            }],
        },
        answer_map={
            "1": {
                "matched": True,
                "question_type": "choice",
                "question_text": "选择题",
                "standard_answer": "A",
                "max_score": 4,
            },
        },
        student_name="学生",
        teacher_id=1,
        category="exam",
    ))

    assert result["score"] is None
    assert result["needs_review_count"] == 1
    assert result["details"][0]["error_type"] == "ocr_unreadable"

    with pytest.raises(ValueError, match="选择题标准答案"):
        finalize_answer_spec_for_category({
            "question_no": "4", "question_type": "choice", "choice_mode": "single", "standard_answer": "-3",
        }, "daily")


def test_model_failure_placeholder_stops_paper_instead_of_becoming_review(monkeypatch):
    from services import grader
    from services.analyzer import _UncacheableJudgmentResult

    async def unavailable(_questions, detail_level="detailed", *, teacher_material=None):
        return _UncacheableJudgmentResult({
            "1": {
                **_judged("needs_review"),
                "explanation": "provider unavailable",
            },
        })

    monkeypatch.setattr(grader, "judge_paper", unavailable)
    monkeypatch.setattr(grader, "get_grading_queue", lambda: ImmediateQueue())

    with pytest.raises(grader.GradingModelUnavailable):
        asyncio.run(grade_paper_unified(
            paper={
                "image_path": "",
                "questions": [{"question_no": "1", "student_answer": "2"}],
            },
            answer_map={
                "1": {
                    "matched": True,
                    "question_type": "fill",
                    "question_text": "1+1=?",
                    "standard_answer": "2",
                },
            },
            student_name="学生",
            teacher_id=1,
        ))


def test_whole_paper_model_failure_gets_exactly_one_complete_retry(monkeypatch):
    from services import grader, task_queue
    from services.analyzer import _UncacheableJudgmentResult

    calls = 0

    async def malformed_then_valid(_questions, detail_level="detailed", *, teacher_material=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _UncacheableJudgmentResult({
                "1": {**_judged("needs_review"), "explanation": "bad JSON"},
            })
        return {"1": _judged("correct", standard_answer="2")}

    queue = task_queue.TaskQueue(
        max_concurrent=1,
        per_teacher_limit=1,
        retry_policy=task_queue.RetryPolicy(
            max_retries=1,
            server_error_base_delay=0.001,
            server_error_max_delay=0.001,
        ),
    )
    monkeypatch.setattr(grader, "judge_paper", malformed_then_valid)
    monkeypatch.setattr(grader, "get_grading_queue", lambda: queue)

    result = asyncio.run(grade_paper_unified(
        paper={"image_path": "", "questions": [{"question_no": "1", "student_answer": "2"}]},
        answer_map={"1": {
            "matched": True,
            "question_type": "fill",
            "question_text": "1+1=?",
            "standard_answer": "2",
        }},
        student_name="学生",
        teacher_id=1,
    ))

    assert calls == 2
    assert result["details"][0]["result"] == "correct"
    assert queue.stats()["total_retries"] == 1


def test_whole_paper_model_failure_after_retry_stays_unavailable(monkeypatch):
    from services import grader, task_queue
    from services.analyzer import _UncacheableJudgmentResult

    calls = 0

    async def always_malformed(_questions, detail_level="detailed", *, teacher_material=None):
        nonlocal calls
        calls += 1
        return _UncacheableJudgmentResult({
            "1": {**_judged("needs_review"), "explanation": "bad JSON"},
        })

    queue = task_queue.TaskQueue(
        max_concurrent=1,
        per_teacher_limit=1,
        retry_policy=task_queue.RetryPolicy(
            max_retries=1,
            server_error_base_delay=0.001,
            server_error_max_delay=0.001,
        ),
    )
    monkeypatch.setattr(grader, "judge_paper", always_malformed)
    monkeypatch.setattr(grader, "get_grading_queue", lambda: queue)

    with pytest.raises(grader.GradingModelUnavailable):
        asyncio.run(grade_paper_unified(
            paper={"image_path": "", "questions": [{"question_no": "1", "student_answer": "2"}]},
            answer_map={"1": {
                "matched": True,
                "question_type": "fill",
                "question_text": "1+1=?",
                "standard_answer": "2",
            }},
            student_name="学生",
            teacher_id=1,
        ))

    assert calls == 2
    assert queue.stats()["total_retries"] == 1


def test_multi_choice_contract_normalizes_sets_and_rejects_ambiguous_or_invalid_answers():
    assert normalize_choice_answer("c, a") == "AC"
    assert normalize_choice_answer("A / C") == "AC"
    assert normalize_choice_answer("AA") == ""
    assert normalize_choice_answer("AE") == ""

    multiple = finalize_answer_spec_for_category({
        "question_no": "2",
        "question_type": "choice",
        "choice_mode": "multiple",
        "standard_answer": "C, A",
    }, "daily")
    assert multiple.standard_answer == "AC"
    assert multiple.to_answer_map_entry()["choice_mode"] == "multiple"

    with pytest.raises(ValueError, match="教师确认"):
        finalize_answer_spec_for_category({
            "question_no": "3", "question_type": "choice",
            "choice_mode": "unknown", "standard_answer": "A",
        }, "daily")
    with pytest.raises(ValueError, match="至少包含两个"):
        finalize_answer_spec_for_category({
            "question_no": "4", "question_type": "choice",
            "choice_mode": "multiple", "standard_answer": "A",
        }, "daily")


def test_multi_choice_grading_uses_unordered_exact_set_without_partial_credit():
    answer_map = {
        "1": {
            "matched": True,
            "question_type": "choice",
            "choice_mode": "multiple",
            "question_text": "多选题",
            "standard_answer": "AC",
            "max_score": 5,
        },
    }

    for answer in ("AC", "CA", "a, c"):
        result = asyncio.run(grade_paper_unified(
            paper={"questions": [{"question_no": "1", "student_answer": answer}]},
            answer_map=answer_map,
            student_name="学生",
            teacher_id=1,
            category="exam",
        ))
        assert result["score"] == 5
        assert result["details"][0]["result"] == "correct"
        assert result["details"][0]["choice_mode"] == "multiple"

    for answer in ("A", "ABC", "AD", "", "AA", "AE"):
        result = asyncio.run(grade_paper_unified(
            paper={"questions": [{"question_no": "1", "student_answer": answer}]},
            answer_map=answer_map,
            student_name="学生",
            teacher_id=1,
            category="exam",
        ))
        assert result["score"] == 0
        assert result["details"][0]["result"] == "wrong"


def test_model_standard_answer_cannot_override_teacher_contract(monkeypatch):
    from services import grader

    async def fake_judge(questions, detail_level="detailed", *, teacher_material=None):
        assert questions[0]["standard_answer"] == "teacher-confirmed"
        return {
            "1": _judged("wrong", standard_answer="model-replacement"),
        }

    monkeypatch.setattr(grader, "judge_paper", fake_judge)
    monkeypatch.setattr(grader, "get_grading_queue", lambda: ImmediateQueue())

    result = asyncio.run(grade_paper_unified(
        paper={
            "image_path": "",
            "questions": [{"question_no": "1", "student_answer": "student-work"}],
        },
        answer_map={
            "1": {
                "matched": True,
                "question_type": "solve",
                "question_text": "problem",
                "standard_answer": "teacher-confirmed",
            },
        },
        student_name="student",
        teacher_id=1,
    ))

    assert result["details"][0]["standard_answer"] == "teacher-confirmed"
    assert result["details"][0]["correct_answer"] == "teacher-confirmed"


def test_severe_neighbor_answer_shift_reviews_only_score_disputes():
    options = ["A", "B", "C", "D"]
    answer_map = {
        str(index): {
            "matched": True,
            "question_type": "choice",
            "question_text": f"第 {index} 题",
            "standard_answer": options[(index - 1) % len(options)],
            "max_score": 4,
        }
        for index in range(1, 21)
    }
    questions = [
        {
            "question_no": str(index),
            "question_type": "choice",
            "student_answer": (
                answer_map[str(index + 1)]["standard_answer"]
                if index < 20 else ""
            ),
        }
        for index in range(1, 21)
    ]
    result = asyncio.run(grade_paper_unified(
        paper={"ocr_answers_only": True, "image_path": "", "questions": questions},
        answer_map=answer_map,
        student_name="学生",
        teacher_id=1,
        category="exam",
    ))

    assert result["score"] is None
    assert result["needs_review_count"] == 19
    assert result["alignment_review_count"] == 19
    assert result["details"][-1]["result"] == "wrong"
    assert "error_type" not in result["details"][-1]
    assert all(
        detail["error_type"] == "ocr_answer_alignment_suspected"
        and detail["match_method"] == "alignment_gate"
        for detail in result["details"][:-1]
    )


def test_independently_verified_alignment_does_not_create_manual_work():
    options = ["A", "B", "C", "D"]
    answer_map = {
        str(index): {
            "matched": True,
            "question_type": "choice",
            "question_text": f"第 {index} 题",
            "standard_answer": options[(index - 1) % len(options)],
            "max_score": 4,
        }
        for index in range(1, 21)
    }
    questions = [
        {
            "question_no": str(index),
            "question_type": "choice",
            "student_answer": (
                answer_map[str(index + 1)]["standard_answer"]
                if index < 20 else ""
            ),
        }
        for index in range(1, 21)
    ]

    result = asyncio.run(grade_paper_unified(
        paper={
            "ocr_answers_only": True,
            "ocr_alignment_verified": True,
            "image_path": "",
            "questions": questions,
        },
        answer_map=answer_map,
        student_name="学生",
        teacher_id=1,
        category="exam",
    ))

    assert result["needs_review_count"] == 0
    assert result["score"] == 0
    assert all(detail["match_method"] == "choice" for detail in result["details"])


def test_alignment_detector_checks_every_candidate_before_selecting_best():
    standards = list("BBCADDBDCB")
    student_answers = ["", *list("BBBAADBBC")]
    answer_map = {
        str(index): {
            "matched": True,
            "question_type": "choice",
            "standard_answer": standard,
        }
        for index, standard in enumerate(standards, 1)
    }
    questions = [
        {
            "question_no": str(index),
            "question_type": "choice",
            "student_answer": student_answer,
        }
        for index, student_answer in enumerate(student_answers, 1)
    ]

    shift = _detect_answer_alignment_shift(questions, answer_map)

    assert shift is not None
    assert shift["offset"] == -1
    assert shift["scope"] == "whole"


def test_alignment_detector_finds_shift_after_mid_paper_blank():
    options = ["A", "B", "C", "D"]
    answer_map = {
        str(index): {
            "matched": True,
            "question_type": "choice",
            "standard_answer": options[(index - 1) % len(options)],
        }
        for index in range(1, 21)
    }
    questions = [
        {
            "question_no": str(index),
            "question_type": "choice",
            "student_answer": (
                answer_map[str(index)]["standard_answer"]
                if index <= 10
                else answer_map[str(index + 1)]["standard_answer"]
                if index < 20
                else ""
            ),
        }
        for index in range(1, 21)
    ]

    shift = _detect_answer_alignment_shift(questions, answer_map)

    assert shift is not None
    assert shift["offset"] == 1
    assert shift["scope"] == "suffix"
    assert shift["window_start"] == 10


def test_alignment_detector_ignores_scattered_local_errors():
    options = ["A", "B", "C", "D"]
    answer_map = {
        str(index): {
            "matched": True,
            "question_type": "choice",
            "standard_answer": options[(index - 1) % len(options)],
        }
        for index in range(1, 21)
    }
    random_wrong_answers = {
        3: "D",
        6: "A",
        9: "D",
        12: "A",
        15: "B",
        18: "C",
    }
    questions = [
        {
            "question_no": str(index),
            "question_type": "choice",
            "student_answer": random_wrong_answers.get(
                index,
                answer_map[str(index)]["standard_answer"],
            ),
        }
        for index in range(1, 21)
    ]

    assert _detect_answer_alignment_shift(questions, answer_map) is None


def test_alignment_detector_ignores_short_local_shift_pattern():
    options = ["A", "B", "C", "D"]
    answer_map = {
        str(index): {
            "matched": True,
            "question_type": "choice",
            "standard_answer": options[(index - 1) % len(options)],
        }
        for index in range(1, 21)
    }
    questions = []
    for index in range(1, 21):
        answer_index = index + 1 if 5 <= index <= 10 else index
        questions.append({
            "question_no": str(index),
            "question_type": "choice",
            "student_answer": answer_map[str(answer_index)]["standard_answer"],
        })

    assert _detect_answer_alignment_shift(questions, answer_map) is None


def test_low_scoring_unshifted_answers_are_not_flagged_as_alignment_error():
    options = ["A", "B", "C", "D"]
    answer_map = {
        str(index): {
            "matched": True,
            "question_type": "choice",
            "question_text": f"第 {index} 题",
            "standard_answer": options[(index - 1) % len(options)],
            "max_score": 4,
        }
        for index in range(1, 21)
    }
    questions = [
        {
            "question_no": str(index),
            "question_type": "choice",
            "student_answer": "A",
        }
        for index in range(1, 21)
    ]

    result = asyncio.run(grade_paper_unified(
        paper={"ocr_answers_only": True, "image_path": "", "questions": questions},
        answer_map=answer_map,
        student_name="学生",
        teacher_id=1,
        category="exam",
    ))

    assert result["needs_review_count"] == 0
    assert result["score"] == 20
    assert all(detail["match_method"] == "choice" for detail in result["details"])


def test_multi_blank_confirmation_requires_one_answer_per_blank():
    with pytest.raises(ValueError, match="答案位数量必须与空数一致"):
        finalize_answer_spec_for_category({
            "question_no": "8", "question_type": "fill", "blank_count": 2,
            "answer_slots": ["2"], "standard_answer": "2",
        }, "daily")


def test_custom_fill_scores_are_frozen_and_must_match_total():
    custom = finalize_answer_spec_for_category({
        "question_no": "8", "question_type": "fill", "blank_count": 2,
        "answer_slots": ["2", "3"], "fill_scoring_mode": "custom",
        "blank_scores": [2, 3], "max_score": 5,
    }, "exam")
    assert custom.fill_scoring_mode == "custom"
    assert custom.blank_scores == [2.0, 3.0]
    assert custom.to_answer_map_entry()["blank_scores"] == [2.0, 3.0]

    with pytest.raises(ValueError, match="之和必须等于本题总分"):
        finalize_answer_spec_for_category({
            "question_no": "8", "question_type": "fill", "blank_count": 2,
            "answer_slots": ["2", "3"], "fill_scoring_mode": "custom",
            "blank_scores": [2, 3], "max_score": 6,
        }, "exam")


def test_exam_scores_solve_subquestions_independently(monkeypatch):
    from services import grader

    captured = []

    async def fake_judge(questions, detail_level="detailed", *, teacher_material=None):
        captured.extend(questions)
        return {
            "17(1)": _judged("correct"),
            "17(2)": _judged("wrong"),
        }

    monkeypatch.setattr(grader, "judge_paper", fake_judge)
    monkeypatch.setattr(grader, "get_grading_queue", lambda: ImmediateQueue())

    result = asyncio.run(grade_paper_unified(
        paper={
            "image_path": "",
            "questions": [{"question_no": "17", "student_answer": "(1)正确过程；(2)错误过程"}],
        },
        answer_map={
            "17": {
                "matched": True,
                "question_type": "solve",
                "question_text": "(1)求定义域；(2)求单调区间",
                "standard_answer": "(1)R；(2)略",
                "subquestion_count": 2,
                "subquestion_scores": [5, 7, 0],
            }
        },
        student_name="学生",
        teacher_id=1,
        category="exam",
    ))

    assert [q["question_no"] for q in captured] == ["17(1)", "17(2)"]
    assert result["score"] == 5.0
    assert result["max_score"] == 12.0
    assert result["correct_rate"] == 0.5
    assert result["correct_count"] == 1
    assert result["wrong_count"] == 1
    assert [d["question_no"] for d in result["details"]] == ["17(1)", "17(2)"]
    assert [d["max_score"] for d in result["details"]] == [5.0, 7.0]
    assert [d["awarded_score"] for d in result["details"]] == [5.0, 0.0]


def test_exam_multi_blank_fill_is_one_question_with_proportional_points(monkeypatch):
    from services import grader

    async def fake_judge(questions, detail_level="detailed", *, teacher_material=None):
        assert [q["question_no"] for q in questions] == ["8[1]", "8[2]"]
        assert [q["standard_answer"] for q in questions] == ["2", "3"]
        return {"8[1]": _judged("correct", standard_answer="2"), "8[2]": _judged("wrong", standard_answer="3")}

    monkeypatch.setattr(grader, "judge_paper", fake_judge)
    monkeypatch.setattr(grader, "get_grading_queue", lambda: ImmediateQueue())

    result = asyncio.run(grade_paper_unified(
        paper={"image_path": "", "questions": [{"question_no": "8", "student_answer": "2；4"}]},
        answer_map={
            "8": {
                "matched": True,
                "question_type": "fill",
                "question_text": "x=___，y=___",
                "standard_answer": "2；3",
                "max_score": 5,
                "blank_count": 2,
                "answer_slots": ["2", "3"],
            }
        },
        student_name="学生",
        teacher_id=1,
        category="exam",
    ))

    assert result["total"] == 1
    assert result["partial_count"] == 1
    assert result["score"] == 2.5
    assert result["max_score"] == 5.0
    assert result["correct_rate"] == 0.0
    detail = result["details"][0]
    assert detail["question_no"] == "8"
    assert detail["result"] == "partial"
    assert detail["awarded_score"] == 2.5
    assert [slot["result"] for slot in detail["blank_results"]] == ["correct", "wrong"]


@pytest.mark.parametrize(
    ("mode", "blank_scores", "expected_score"),
    [
        ("all_or_nothing", [], 0.0),
        ("custom", [2.0, 3.0], 2.0),
    ],
)
def test_exam_multi_blank_fill_honors_confirmed_scoring_mode(monkeypatch, mode, blank_scores, expected_score):
    from services import grader

    async def fake_judge(questions, detail_level="detailed", *, teacher_material=None):
        return {"8[1]": _judged("correct"), "8[2]": _judged("wrong")}

    monkeypatch.setattr(grader, "judge_paper", fake_judge)
    monkeypatch.setattr(grader, "get_grading_queue", lambda: ImmediateQueue())

    result = asyncio.run(grade_paper_unified(
        paper={"image_path": "", "questions": [{"question_no": "8", "student_answer": "2；4"}]},
        answer_map={"8": {
            "matched": True, "question_type": "fill", "question_text": "x=___，y=___",
            "standard_answer": "2；3", "max_score": 5, "blank_count": 2,
            "answer_slots": ["2", "3"], "fill_scoring_mode": mode,
            "blank_scores": blank_scores,
        }},
        student_name="学生", teacher_id=1, category="exam",
    ))

    detail = result["details"][0]
    assert result["total"] == 1
    assert result["score"] == expected_score
    assert result["max_score"] == 5.0
    assert detail["fill_scoring_mode"] == mode
    assert detail["awarded_score"] == expected_score


def test_teacher_recalculation_preserves_fill_scoring_modes():
    from services.scoring_engine import recalculate_result_from_details

    all_or_nothing = {
        "details": [{
            "question_no": "8", "question_type": "fill", "fill_scoring_mode": "all_or_nothing",
            "result": "partial", "max_score": 5,
        }],
    }
    recalculate_result_from_details(all_or_nothing, "exam")
    assert (all_or_nothing["score"], all_or_nothing["max_score"]) == (0.0, 5.0)

    custom = {
        "details": [{
            "question_no": "8", "question_type": "fill", "fill_scoring_mode": "custom",
            "result": "partial", "max_score": 5,
            "blank_results": [
                {"result": "correct", "max_score": 2},
                {"result": "wrong", "max_score": 3},
            ],
        }],
    }
    recalculate_result_from_details(custom, "exam")
    assert (custom["score"], custom["max_score"]) == (2.0, 5.0)

    equal = {
        "details": [{
            "question_no": "9", "question_type": "fill", "fill_scoring_mode": "equal",
            "result": "partial", "max_score": 6,
            "blank_results": [
                {"result": "correct", "max_score": 2},
                {"result": "correct", "max_score": 2},
                {"result": "wrong", "max_score": 2},
            ],
        }],
    }
    recalculate_result_from_details(equal, "exam")
    assert (equal["score"], equal["max_score"]) == (4.0, 6.0)


def test_daily_has_no_score_and_counts_each_solve_subquestion(monkeypatch):
    from services import grader

    async def fake_judge(questions, detail_level="detailed", *, teacher_material=None):
        return {
            "17(1)": _judged("correct"),
            "17(2)": _judged("wrong"),
        }

    monkeypatch.setattr(grader, "judge_paper", fake_judge)
    monkeypatch.setattr(grader, "get_grading_queue", lambda: ImmediateQueue())

    result = asyncio.run(grade_paper_unified(
        paper={"image_path": "", "questions": [{"question_no": "17", "student_answer": "作答"}]},
        answer_map={
            "17": {
                "matched": True,
                "question_type": "solve",
                "question_text": "(1)第一问；(2)第二问",
                "standard_answer": "答案",
                "subquestion_count": 2,
                "subquestion_scores": [5, 7, 8],
            }
        },
        student_name="学生",
        teacher_id=1,
        category="daily",
    ))

    assert result["score"] is None
    assert result["max_score"] is None
    assert result["correct_rate"] == 0.5
    assert result["total"] == 2
    assert [d["question_no"] for d in result["details"]] == ["17(1)", "17(2)"]
    assert all(d["max_score"] is None and d["awarded_score"] is None for d in result["details"])


def test_daily_multi_blank_fill_counts_as_one_question_and_partial_is_not_correct(monkeypatch):
    from services import grader

    async def fake_judge(questions, detail_level="detailed", *, teacher_material=None):
        return {"8[1]": _judged("correct"), "8[2]": _judged("wrong")}

    monkeypatch.setattr(grader, "judge_paper", fake_judge)
    monkeypatch.setattr(grader, "get_grading_queue", lambda: ImmediateQueue())

    result = asyncio.run(grade_paper_unified(
        paper={"image_path": "", "questions": [{"question_no": "8", "student_answer": "2；4"}]},
        answer_map={
            "8": {
                "matched": True,
                "question_type": "fill",
                "question_text": "x=___，y=___",
                "standard_answer": "2；3",
                "blank_count": 2,
                "answer_slots": ["2", "3"],
            }
        },
        student_name="学生",
        teacher_id=1,
        category="daily",
    ))

    assert result["score"] is None
    assert result["correct_rate"] == 0.0
    assert result["total"] == 1
    assert result["partial_count"] == 1
    assert result["details"][0]["blank_count"] == 2


def test_duplicate_ocr_rows_for_same_fill_are_merged_before_scoring(monkeypatch):
    from services import grader

    async def fake_judge(questions, detail_level="detailed", *, teacher_material=None):
        assert [q["question_no"] for q in questions] == ["8[1]", "8[2]"]
        assert [q["student_answer"] for q in questions] == ["2", "4"]
        return {"8[1]": _judged("correct"), "8[2]": _judged("wrong")}

    monkeypatch.setattr(grader, "judge_paper", fake_judge)
    monkeypatch.setattr(grader, "get_grading_queue", lambda: ImmediateQueue())

    result = asyncio.run(grade_paper_unified(
        paper={"image_path": "", "questions": [
            {"question_no": "8", "student_answer": "2", "question_type": "fill"},
            {"question_no": "8", "student_answer": "4", "question_type": "fill"},
        ]},
        answer_map={"8": {
            "matched": True, "question_type": "fill", "question_text": "x=___，y=___",
            "standard_answer": "2；3", "blank_count": 2, "answer_slots": ["2", "3"],
            "max_score": 5,
        }},
        student_name="学生", teacher_id=1, category="exam",
    ))

    assert result["total"] == 1
    assert result["score"] == 2.5
    assert result["max_score"] == 5.0


def test_numbered_fill_rows_are_sorted_by_blank_index_before_scoring(monkeypatch):
    from services import grader

    async def fake_judge(questions, detail_level="detailed", *, teacher_material=None):
        assert [q["question_no"] for q in questions] == ["8[1]", "8[2]"]
        assert [q["student_answer"] for q in questions] == ["2", "3"]
        return {"8[1]": _judged("correct"), "8[2]": _judged("correct")}

    monkeypatch.setattr(grader, "judge_paper", fake_judge)
    monkeypatch.setattr(grader, "get_grading_queue", lambda: ImmediateQueue())

    result = asyncio.run(grade_paper_unified(
        paper={"image_path": "", "questions": [
            {"question_no": "8(2)", "student_answer": "3", "question_type": "fill"},
            {"question_no": "8(1)", "student_answer": "2", "question_type": "fill"},
        ]},
        answer_map={"8": {
            "matched": True, "question_type": "fill", "question_text": "x=___，y=___",
            "standard_answer": "2；3", "blank_count": 2, "answer_slots": ["2", "3"],
            "max_score": 5,
        }},
        student_name="学生", teacher_id=1, category="exam",
    ))

    assert result["total"] == 1
    assert result["score"] == 5.0


def test_missing_fill_slots_are_scored_as_blank_not_copied_answer(monkeypatch):
    from services import grader

    async def fake_judge(questions, detail_level="detailed", *, teacher_material=None):
        assert [q["student_answer"] for q in questions] == ["2", ""]
        return {"8[1]": _judged("correct"), "8[2]": _judged("wrong")}

    monkeypatch.setattr(grader, "judge_paper", fake_judge)
    monkeypatch.setattr(grader, "get_grading_queue", lambda: ImmediateQueue())

    result = asyncio.run(grade_paper_unified(
        paper={"image_path": "", "questions": [
            {"question_no": "8", "student_answer": "2", "question_type": "fill"},
        ]},
        answer_map={"8": {
            "matched": True, "question_type": "fill", "question_text": "x=___，y=___",
            "standard_answer": "2；2", "blank_count": 2, "answer_slots": ["2", "2"],
            "max_score": 5,
        }},
        student_name="学生", teacher_id=1, category="exam",
    ))

    assert result["total"] == 1
    assert result["score"] == 2.5


def test_only_later_numbered_fill_row_keeps_its_original_blank_position(monkeypatch):
    from services import grader

    async def fake_judge(questions, detail_level="detailed", *, teacher_material=None):
        assert [q["student_answer"] for q in questions] == ["", "3"]
        return {"8[1]": _judged("wrong"), "8[2]": _judged("correct")}

    monkeypatch.setattr(grader, "judge_paper", fake_judge)
    monkeypatch.setattr(grader, "get_grading_queue", lambda: ImmediateQueue())

    result = asyncio.run(grade_paper_unified(
        paper={"image_path": "", "questions": [
            {"question_no": "8(2)", "student_answer": "3", "question_type": "fill"},
        ]},
        answer_map={"8": {
            "matched": True, "question_type": "fill", "question_text": "x=___，y=___",
            "standard_answer": "2；3", "blank_count": 2, "answer_slots": ["2", "3"],
            "max_score": 5,
        }},
        student_name="学生", teacher_id=1, category="exam",
    ))

    assert result["total"] == 1
    assert result["score"] == 2.5


def test_pending_item_withholds_exam_score_and_correct_rate(monkeypatch):
    from services import grader

    async def fake_judge(questions, detail_level="detailed", *, teacher_material=None):
        return {"1": _judged("needs_review")}

    monkeypatch.setattr(grader, "judge_paper", fake_judge)
    monkeypatch.setattr(grader, "get_grading_queue", lambda: ImmediateQueue())

    result = asyncio.run(grade_paper_unified(
        paper={"image_path": "", "questions": [{"question_no": "1", "student_answer": "无法识别"}]},
        answer_map={
            "1": {"matched": True, "question_type": "fill", "question_text": "填空", "standard_answer": "2", "max_score": 5}
        },
        student_name="学生",
        teacher_id=1,
        category="exam",
    ))

    assert result["score"] is None
    assert result["max_score"] == 5.0
    assert result["correct_rate"] is None
    assert result["needs_review_count"] == 1
