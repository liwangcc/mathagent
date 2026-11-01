from types import SimpleNamespace

import pytest

from services import batch_report


def _batch(reviews, mode="precise"):
    return SimpleNamespace(review_results=reviews, results=[], grade_mode=mode)


def _review(details, knowledge_items=None, disposition="active"):
    return {
        "disposition": disposition,
        "result": {"details": details},
        "knowledge_items": knowledge_items or [],
    }


def test_payload_includes_choice_errors_in_knowledge_distribution():
    details = [
        {
            "question_no": "1",
            "question": "函数 f(x) 的单调性判断",
            "question_type": "solve",
            "result": "wrong",
            "error_type": "概念",
            "explanation": "把增减区间性质混淆",
            "student_answer": "这段答案不应发送到报告模型",
        },
        {
            "question_no": "2",
            "question": "选择题题干",
            "question_type": "choice",
            "result": "wrong",
            "error_type": "",
        },
    ]
    payload = batch_report.build_batch_report_payload(_batch([_review(
        details,
        [{"chapter": "函数", "topic": "单调性"}, {"chapter": "代数", "topic": ""}],
    )]))

    stats = payload["stats"]
    assert stats["question_count"] == 2
    assert stats["diagnostic_question_count"] == 1
    assert stats["choice_count"] == 1
    assert stats["choice_wrong_count"] == 1
    assert stats["diagnostic_error_count"] == 1
    assert stats["diagnostic_error_rate"] == 1.0
    assert stats["knowledge_points"][0]["name"] == "单调性"
    assert stats["knowledge_points"][0]["chapter"] == "函数"
    assert {
        (item["chapter"], item["topic"], item["error_count"])
        for item in stats["knowledge_points"]
    } == {("函数", "单调性", 1), ("代数", "未分类", 1)}
    assert "error_types" not in stats
    assert "student_answer" not in str(stats)


def test_unclassified_wrong_questions_stay_visible_under_knowledge_distribution():
    stats = batch_report.build_batch_report_payload(_batch([_review([
        {
            "question_no": "1",
            "question": "题干",
            "question_type": "solve",
            "result": "wrong",
            "error_type": "计算",
        },
    ])]))["stats"]
    assert stats["knowledge_distribution"][0]["topic"] == "未分类"
    assert stats["unclassified_knowledge_count"] == 1


def test_placeholder_knowledge_is_still_treated_as_unclassified():
    review = _review([{
        "question_no": "1",
        "question": "题干",
        "question_type": "solve",
        "result": "wrong",
        "error_type": "方法",
    }])
    review["knowledge_items"] = [{"chapter": "未知", "topic": "综合"}]

    stats = batch_report.build_batch_report_payload(_batch([review]))["stats"]

    assert stats["knowledge_distribution"][0]["topic"] == "未分类"
    assert stats["unclassified_knowledge_count"] == 1


def test_voided_papers_are_excluded_from_report_statistics():
    details = [{
        "question_no": "1",
        "question": "1+1",
        "question_type": "solve",
        "result": "wrong",
        "error_type": "计算",
    }]
    payload = batch_report.build_batch_report_payload(_batch([
        _review(details, [{"chapter": "数与式", "topic": "有理数"}], disposition="voided"),
        _review(details, [{"chapter": "数与式", "topic": "有理数"}]),
    ]))
    assert payload["stats"]["paper_count"] == 1
    assert payload["stats"]["wrong_count"] == 1


def test_aggregate_review_without_details_still_counts_unreadable_answers():
    review = {
        "paper_id": "p1",
        "disposition": "active",
        "result": {
            "total": 4,
            "correct_count": 1,
            "partial_count": 1,
            "wrong_count": 1,
            "needs_review_count": 1,
            "details": [],
        },
        "knowledge_items": [],
    }

    stats = batch_report.build_batch_report_payload(_batch([review]))["stats"]

    assert stats["question_count"] == 4
    assert stats["correct_count"] == 1
    assert stats["partial_count"] == 1
    assert stats["wrong_count"] == 1
    assert stats["needs_review_count"] == 1
    assert stats["error_rate"] == 0.6667
    assert stats["knowledge_points"] == []


def test_legacy_result_row_is_used_when_review_details_are_missing():
    review = {"paper_id": "p1", "disposition": "active", "result": {"details": []}}
    batch = _batch([review])
    batch.results = [{
        "paper_id": "p1",
        "total": 2,
        "correct": 0,
        "partial": 0,
        "wrong": 1,
        "needs_review": 1,
    }]

    stats = batch_report.build_batch_report_payload(batch)["stats"]

    assert stats["question_count"] == 2
    assert stats["wrong_count"] == 1
    assert stats["needs_review_count"] == 1


def test_report_keeps_all_error_distribution_and_limits_top_errors_to_five():
    details = [
        {
            "question_no": str(index),
            "question": f"第{index}题题干",
            "question_type": "solve",
            "result": "wrong",
            "error_type": "计算",
        }
        for index in range(1, 8)
    ]
    stats = batch_report.build_batch_report_payload(
        _batch([_review(details)])
    )["stats"]

    assert len(stats["all_errors"]) == 7
    assert [item["question_no"] for item in stats["all_errors"]] == [
        str(index) for index in range(1, 8)
    ]
    assert len(stats["top_errors"]) == 5
    assert stats["top_errors"] == stats["all_errors"][:5]
    assert stats["all_errors"][0]["attempt_count"] == 1
    assert stats["all_errors"][0]["judged_count"] == 1
    assert stats["all_errors"][0]["error_rate"] == 1.0


def _detail(question_no, result):
    return {"question_no": question_no, "question": f"第{question_no}题", "question_type": "solve", "result": result}


def test_top_errors_rank_by_error_count_then_rate_then_question_number():
    # Paper order deliberately lists question 10 first and question 2 last, so
    # a first-seen tie-break would put 10 ahead of 2.
    reviews = [
        _review([_detail("10", "wrong"), _detail("9", "wrong"), _detail("3", "wrong")]),
        _review([_detail("10", "correct"), _detail("9", "wrong"), _detail("3", "correct")]),
        _review([_detail("10", "correct"), _detail("9", "correct"), _detail("3", "correct"),
                 _detail("2", "wrong"), _detail("2(1)", "partial")]),
    ]
    stats = batch_report.build_batch_report_payload(_batch(reviews))["stats"]

    assert [(item["question_no"], item["error_count"]) for item in stats["all_errors"]] == [
        ("9", 2), ("2", 1), ("2(1)", 1), ("3", 1), ("10", 1),
    ]
    # Same count: the higher error rate (1 of 1) ranks above 1 of 3.
    assert stats["all_errors"][1]["error_rate"] == 1.0
    assert stats["all_errors"][3]["error_rate"] == 0.3333


def test_knowledge_points_rank_by_errors_and_count_unclassified_answers_honestly():
    details = [_detail("1", "correct"), _detail("2", "correct"), _detail("3", "correct"),
               _detail("4", "wrong"), _detail("5", "wrong"), _detail("6", "correct")]
    labelled = {"chapter": "函数", "topic": "单调性"}
    stats = batch_report.build_batch_report_payload(_batch([_review(
        details, [labelled, labelled, labelled, labelled, {}, {}],
    )]))["stats"]

    # Equal error counts: the labelled topic ranks first even though the
    # unclassified bucket has the higher rate. Unclassified correct answers
    # now count as attempts, so its rate is 1/2 rather than a fixed 100%.
    assert [(item["topic"], item["error_count"], item["attempt_count"]) for item in stats["knowledge_points"]] == [
        ("单调性", 1, 4), ("未分类", 1, 2),
    ]
    assert stats["knowledge_points"][1]["error_rate"] == 0.5
    assert stats["unclassified_knowledge_count"] == 1


def test_knowledge_points_with_more_errors_rank_first_even_if_answered_less():
    details = [_detail("1", "correct"), _detail("2", "correct"), _detail("3", "correct"),
               _detail("4", "wrong"), _detail("5", "wrong")]
    common = {"chapter": "函数", "topic": "定义域"}
    rare = {"chapter": "数列", "topic": "等差数列"}
    stats = batch_report.build_batch_report_payload(_batch([_review(
        details, [common, common, common, rare, rare],
    )]))["stats"]

    assert [item["topic"] for item in stats["knowledge_points"]] == ["等差数列", "定义域"]


@pytest.mark.asyncio
async def test_report_falls_back_without_calling_model_for_choice_only(monkeypatch):
    called = False

    async def fail_if_called(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("choice-only reports must not call the model")

    monkeypatch.setattr(batch_report, "generate_report_text", fail_if_called)
    stats = batch_report.build_batch_report_payload(_batch([_review([
        {"question_no": "1", "question": "选择", "question_type": "choice", "result": "wrong"},
    ])]))["stats"]
    text, note = await batch_report.generate_batch_report(stats, teacher_id=1)
    assert not called
    assert "选择题" in text
    assert note


@pytest.mark.asyncio
async def test_report_uses_model_for_non_choice_and_keeps_fallback_on_failure(monkeypatch):
    async def fake_call(*args, **kwargs):
        return "【总体情况】建议先复习函数。"

    monkeypatch.setattr(batch_report, "generate_report_text", fake_call)
    stats = batch_report.build_batch_report_payload(_batch([_review([
        {"question_no": "1", "question": "函数", "question_type": "solve", "result": "wrong", "error_type": "方法"},
    ])]))["stats"]
    text, note = await batch_report.generate_batch_report(stats, teacher_id=1)
    assert text.startswith("【总体情况】")
    assert note == ""

    async def broken_call(*args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(batch_report, "generate_report_text", broken_call)
    fallback, note = await batch_report.generate_batch_report(stats, teacher_id=1)
    assert "方法" not in fallback
    assert "错因" not in fallback
    assert "分类失败" in fallback and "未分类" not in fallback
    assert "统计版" in note


@pytest.mark.asyncio
async def test_report_does_not_call_model_when_non_choice_is_unresolved(monkeypatch):
    called = False

    async def fail_if_called(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("unresolved questions must use the deterministic report")

    monkeypatch.setattr(batch_report, "generate_report_text", fail_if_called)
    stats = batch_report.build_batch_report_payload(_batch([_review([
        {
            "question_no": "1",
            "question": "函数题",
            "question_type": "solve",
            "result": "needs_review",
        },
    ])]))["stats"]

    text, note = await batch_report.generate_batch_report(stats, teacher_id=1)

    assert not called
    assert stats["diagnostic_question_count"] == 1
    assert stats["diagnostic_judged_count"] == 0
    assert "待人工复核" in text
    assert note
