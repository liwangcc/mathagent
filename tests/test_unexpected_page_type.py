import asyncio

import pytest

from services.grader import GradingModelUnavailable, grade_paper_unified


def test_unexpected_page_type_needs_review_without_llm(monkeypatch):
    def should_not_be_called(*args, **kwargs):
        raise AssertionError("unresolved page type must not call the LLM queue")

    import services.grader as grader
    monkeypatch.setattr(grader, "get_grading_queue", should_not_be_called)

    paper = {
        "image_path": "wrong-paper.png",
        "unexpected_page_type": True,
        "page_type_error": "未匹配本批次已确认页型",
        "questions": [
            {"question_no": "1", "student_answer": "A", "question_type": "choice"},
            {"question_no": "2", "student_answer": "x=3", "question_type": "solve"},
        ],
    }
    answer_map = {
        "1": {"matched": True, "standard_answer": "B", "question_text": "正常题 1"},
        "2": {"matched": True, "standard_answer": "x=2", "question_text": "正常题 2"},
    }

    result = asyncio.run(
        grade_paper_unified(paper, answer_map, "张三", teacher_id=1)
    )

    assert result["score"] is None
    assert result["correct_rate"] is None
    assert result["correct_count"] == 0
    assert result["partial_count"] == 0
    assert result["wrong_count"] == 0
    assert result["needs_review_count"] == 2
    assert result["page_type_status"] == "needs_review"
    assert result["page_type_error"] == "未匹配本批次已确认页型"
    assert [item["result"] for item in result["details"]] == ["needs_review", "needs_review"]
    assert all(item["error_type"] == "unexpected_page_type" for item in result["details"])
    assert all(item["awarded_score"] is None for item in result["details"])
    assert all(item["match_method"] == "page_type_gate" for item in result["details"])


def test_unexpected_page_type_exam_score_remains_unresolved_not_zero(monkeypatch):
    def should_not_be_called(*args, **kwargs):
        raise AssertionError("unresolved page type must not call the LLM queue")

    import services.grader as grader
    monkeypatch.setattr(grader, "get_grading_queue", should_not_be_called)

    result = asyncio.run(grade_paper_unified(
        {
            "image_path": "wrong-exam-page.png",
            "unexpected_page_type": True,
            "questions": [{"question_no": "1", "student_answer": "A", "question_type": "choice"}],
        },
        {"1": {
            "matched": True,
            "question_type": "choice",
            "choice_mode": "single",
            "standard_answer": "B",
            "question_text": "正常题 1",
            "max_score": 5,
        }},
        "张三",
        teacher_id=1,
        category="exam",
    ))

    assert result["score"] is None
    assert result["max_score"] == 5
    assert result["correct_rate"] is None
    assert result["wrong_count"] == 0
    assert result["needs_review_count"] == 1
    assert result["details"][0]["max_score"] == 5
    assert result["details"][0]["awarded_score"] is None


def test_unexpected_page_does_not_block_normal_paper_grading_in_same_batch(monkeypatch):
    def should_not_be_called(*args, **kwargs):
        raise AssertionError("choice-only papers must not call the LLM queue")

    import services.grader as grader
    monkeypatch.setattr(grader, "get_grading_queue", should_not_be_called)
    answer_map = {"1": {
        "matched": True,
        "question_type": "choice",
        "choice_mode": "single",
        "standard_answer": "A",
        "question_text": "正常题 1",
        "max_score": 5,
    }}

    async def grade_both():
        return await asyncio.gather(
            grade_paper_unified(
                {
                    "image_path": "foreign.png",
                    "unexpected_page_type": True,
                    "questions": [{"question_no": "1", "student_answer": "A"}],
                },
                answer_map,
                "待确认学生",
                teacher_id=1,
                category="exam",
            ),
            grade_paper_unified(
                {
                    "image_path": "normal.png",
                    "questions": [{"question_no": "1", "student_answer": "A"}],
                },
                answer_map,
                "正常学生",
                teacher_id=1,
                category="exam",
            ),
        )

    unexpected, normal = asyncio.run(grade_both())

    assert unexpected["score"] is None
    assert unexpected["needs_review_count"] == 1
    assert normal["score"] == 5
    assert normal["correct_rate"] == 1.0
    assert normal["correct_count"] == 1


def test_llm_failure_stops_grading_instead_of_creating_a_placeholder(monkeypatch):
    """模型故障必须中断链路，不得伪造成学生待复核结果。"""
    class FailingQueue:
        async def submit(self, **_kwargs):
            raise RuntimeError("model unavailable")

    import services.grader as grader
    monkeypatch.setattr(grader, "get_grading_queue", lambda: FailingQueue())

    with pytest.raises(GradingModelUnavailable):
        asyncio.run(
            grade_paper_unified(
                {
                    "image_path": "needs-review.png",
                    "questions": [{"question_no": "1", "student_answer": "x=2", "question_type": "solve"}],
                },
                {"1": {"matched": True, "standard_answer": "x=2", "question_text": "解方程"}},
                "张三",
                teacher_id=1,
            )
        )
