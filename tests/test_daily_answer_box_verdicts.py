"""Per-box verdicts of a multi-blank fill question survive teacher decisions.

Synthetic eight-blank question: the grader judges seven blanks correct and one
wrong. The whole question folds to "partial" (one question, correct_rate 0);
each answer box keeps its own verdict in ``grading_units`` for the review marks
and the results page's answer-box accuracy.
"""
from services.quick_grader import QuickBatch
from services.scoring_engine import (
    aggregate_grading_result,
    apply_teacher_verdict,
    build_grading_units,
    recalculate_result_from_details,
)
from tests.test_batch_review_lifecycle import _setup_review_batch

ANSWERS = [str(value) for value in range(11, 19)]
SEVEN_RIGHT_ONE_WRONG = ["correct"] * 7 + ["wrong"]


def graded(category: str) -> dict:
    entry = {"question_no": "1", "question_type": "fill", "question_text": "合成填空题",
             "standard_answer": "；".join(ANSWERS), "answer_slots": ANSWERS, "blank_count": 8}
    if category == "exam":
        entry["max_score"] = 8
    student = {"question_no": "1", "student_answer": "；".join(ANSWERS[:7] + ["99"])}
    units = build_grading_units([student], {"1": entry}, category)
    judgments = {unit["key"]: {"result": verdict, "explanation": "", "hint": "", "model_answer": ""}
                 for unit, verdict in zip(units, SEVEN_RIGHT_ONE_WRONG)}
    return aggregate_grading_result(units, judgments, category, "合成学生")


def box_verdicts(detail: dict) -> list[str]:
    return [unit["result"] for unit in detail["grading_units"]]


def test_whole_question_status_folds_while_each_box_keeps_its_verdict():
    result = graded("daily")
    detail, = result["details"]
    assert (detail["result"], result["partial_count"], result["total"], result["correct_rate"]) == ("partial", 1, 1, 0.0)
    assert box_verdicts(detail) == SEVEN_RIGHT_ONE_WRONG


def test_teacher_verdicts_reach_the_boxes_and_partial_restores_the_graders_boxes():
    detail = graded("daily")["details"][0]
    apply_teacher_verdict(detail, "wrong")
    assert box_verdicts(detail) == ["wrong"] * 8
    apply_teacher_verdict(detail, "partial")
    assert detail["result"] == "partial"
    assert box_verdicts(detail) == SEVEN_RIGHT_ONE_WRONG
    apply_teacher_verdict(detail, "correct")
    assert box_verdicts(detail) == ["correct"] * 8


def test_partial_without_box_evidence_marks_every_box_partial():
    all_right = graded("daily")["details"][0]
    for item in all_right["blank_results"]:
        item["result"] = "correct"
    apply_teacher_verdict(all_right, "partial")
    assert box_verdicts(all_right) == ["partial"] * 8, "eight right blanks cannot make a partial question"
    legacy = graded("daily")["details"][0]
    del legacy["blank_results"]
    apply_teacher_verdict(legacy, "partial")
    assert box_verdicts(legacy) == ["partial"] * 8


def test_exam_points_stay_with_the_whole_question_rules():
    result = graded("exam")
    detail = result["details"][0]
    assert (result["score"], result["max_score"]) == (7.0, 8.0)
    apply_teacher_verdict(detail, "wrong")
    recalculate_result_from_details(result, "exam")
    assert result["score"] == 0.0
    apply_teacher_verdict(detail, "partial")
    recalculate_result_from_details(result, "exam")
    assert (result["score"], result["max_score"], result["partial_count"]) == (7.0, 8.0, 1)
    assert box_verdicts(detail) == SEVEN_RIGHT_ONE_WRONG


def test_reopened_review_keeps_seven_right_boxes_after_teacher_edits(monkeypatch, tmp_path):
    client, _session, upload_dir = _setup_review_batch(monkeypatch, tmp_path)
    batch = QuickBatch.load_meta("review-batch", str(upload_dir))
    batch.review_results[0]["result"] = graded("daily")
    batch.save_meta()

    # A teacher first marks the whole question wrong, then changes it back to partial.
    for revision, verdict in enumerate(["wrong", "partial"]):
        saved = client.post("/api/batch-grade/review/decision", json={
            "batch_id": "review-batch", "paper_id": "p1", "review_revision": revision,
            "decisions": [{"question_no": "1", "result": verdict, "note": ""}],
        })
        assert saved.status_code == 200, saved.text
    assert box_verdicts(saved.json()["review"]["result"]["details"][0]) == SEVEN_RIGHT_ONE_WRONG

    reopened = client.get("/api/batch-grade/review/review-batch")
    assert reopened.status_code == 200, reopened.text
    row = next(item for item in reopened.json()["results"] if item["paper_id"] == "p1")
    assert box_verdicts(row["details"][0]) == SEVEN_RIGHT_ONE_WRONG
    assert (row["correct"], row["partial"], row["wrong"], row["total"], row["correct_rate"]) == (0, 1, 0, 1, 0.0)
