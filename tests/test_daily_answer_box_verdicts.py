"""Statistics count answer boxes; per-box verdicts survive teacher decisions.

Synthetic eight-blank question: the grader judges seven blanks correct and one
wrong. Its detail row still folds to "partial", but every count, correct_rate
and report statistic counts the eight answer boxes (7 correct · 1 wrong ·
87.5%), as the review marks on the image do.
"""
from types import SimpleNamespace

import pytest

from models import GradingResult, Student, StudentProfile
from models_user import User
from routers.teacher import ConfirmJudgmentRequest, confirm_judgment
from services import batch_report
from services.correction_tracking import audit_grading_result_projection
from services.quick_grader import QuickBatch
from services.scoring_engine import (
    aggregate_grading_result,
    apply_teacher_verdict,
    build_grading_units,
    recalculate_result_from_details,
)
from tests.test_answer_sheet_session import _manual_decision_session, client_env  # noqa: F401
from tests.test_batch_review_lifecycle import _setup_review_batch
from tests.test_teacher_bank_delete import _session_factory

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


def counts(result: dict) -> tuple:
    """(correct, partial, wrong, needs_review, total, correct_rate) of a grading result."""
    return tuple(result[key] for key in (
        "correct_count", "partial_count", "wrong_count", "needs_review_count", "total", "correct_rate"))


def box_decisions(verdicts: list[str]) -> list[dict]:
    """A teacher's review decisions for the eight boxes, by their ids 1[1] … 1[8]."""
    return [{"question_no": f"1[{index}]", "result": verdict, "note": ""} for index, verdict in enumerate(verdicts, 1)]


def test_counts_and_rate_follow_the_answer_boxes_while_the_question_folds():
    result = graded("daily")
    detail, = result["details"]
    assert detail["result"] == "partial"
    assert box_verdicts(detail) == SEVEN_RIGHT_ONE_WRONG
    assert counts(result) == (7, 0, 1, 0, 8, 0.875)


def test_teacher_verdicts_reach_the_boxes_and_partial_restores_the_graders_boxes():
    result = graded("daily")
    detail = result["details"][0]
    for verdict, boxes, expected in [
        ("wrong", ["wrong"] * 8, (0, 0, 8, 0, 8, 0.0)),
        ("partial", SEVEN_RIGHT_ONE_WRONG, (7, 0, 1, 0, 8, 0.875)),
        ("correct", ["correct"] * 8, (8, 0, 0, 0, 8, 1.0)),
    ]:
        apply_teacher_verdict(detail, verdict)
        recalculate_result_from_details(result, "daily")
        assert (detail["result"], box_verdicts(detail), counts(result)) == (verdict, boxes, expected)


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
    assert (result["score"], result["max_score"]) == (7.0, 8.0)
    assert counts(result) == (7, 0, 1, 0, 8, 0.875)
    assert box_verdicts(detail) == SEVEN_RIGHT_ONE_WRONG


def test_reopened_review_keeps_seven_right_boxes_after_teacher_edits(monkeypatch, tmp_path):
    client, _session, upload_dir = _setup_review_batch(monkeypatch, tmp_path)
    batch = QuickBatch.load_meta("review-batch", str(upload_dir))
    batch.review_results[0]["result"] = graded("daily")
    batch.save_meta()

    # A teacher first marks every box wrong, then gives seven boxes back their right verdict.
    for revision, verdicts in enumerate([["wrong"] * 8, SEVEN_RIGHT_ONE_WRONG]):
        saved = client.post("/api/batch-grade/review/decision", json={
            "batch_id": "review-batch", "paper_id": "p1", "review_revision": revision,
            "decisions": box_decisions(verdicts),
        })
        assert saved.status_code == 200, saved.text
    assert box_verdicts(saved.json()["review"]["result"]["details"][0]) == SEVEN_RIGHT_ONE_WRONG

    reopened = client.get("/api/batch-grade/review/review-batch")
    assert reopened.status_code == 200, reopened.text
    row = next(item for item in reopened.json()["results"] if item["paper_id"] == "p1")
    assert box_verdicts(row["details"][0]) == SEVEN_RIGHT_ONE_WRONG
    assert (row["correct"], row["partial"], row["wrong"], row["total"], row["correct_rate"]) == (7, 0, 1, 8, 0.875)


def box_report_stats() -> dict:
    review = {"disposition": "active", "result": graded("daily"), "knowledge_items": []}
    batch = SimpleNamespace(review_results=[review], results=[], grade_mode="precise")
    return batch_report.build_batch_report_payload(batch)["stats"]


def test_batch_report_counts_answer_boxes_and_still_ranks_whole_questions():
    stats = box_report_stats()
    assert [stats[key] for key in (
        "question_count", "correct_count", "partial_count", "wrong_count", "needs_review_count", "error_rate",
    )] == [8, 7, 0, 1, 0, 0.125]
    assert [stats[key] for key in (
        "diagnostic_question_count", "diagnostic_judged_count", "diagnostic_error_count", "diagnostic_error_rate",
    )] == [8, 8, 1, 0.125]
    # The list of questions to go over in class still names the question once per paper.
    assert [(item["question_no"], item["error_count"], item["attempt_count"]) for item in stats["top_errors"]] == [
        ("1", 1, 1)]


@pytest.mark.asyncio
async def test_batch_report_text_counts_answer_boxes(monkeypatch):
    prompts = []

    async def provider_down(messages, **_kwargs):
        prompts.append(messages[0]["content"])
        raise RuntimeError("provider down")

    monkeypatch.setattr(batch_report, "generate_report_text", provider_down)
    text, _note = await batch_report.generate_batch_report(box_report_stats(), teacher_id=1)
    assert "作答框" in prompts[0]
    assert text.startswith("本批次共 1 份试卷、8 个作答框，错误（含部分正确）1 个。")


@pytest.mark.parametrize("saved_counts", ["per answer box", "whole question, saved before this fix"])
def test_multi_page_archive_counts_every_answer_box(monkeypatch, tmp_path, saved_counts):
    client, Session, upload_dir = _setup_review_batch(monkeypatch, tmp_path)
    batch = QuickBatch.load_meta("review-batch", str(upload_dir))
    batch.uploads = [
        {"paper_id": "p1", "filename": "第1页.png"},
        {"paper_id": "p3", "filename": "第2页.png"},
        {"paper_id": "p2", "filename": "其他学生.png"},
    ]
    first_page = graded("daily")
    if saved_counts != "per answer box":
        first_page.update(total=1, correct_count=0, partial_count=1, wrong_count=0, correct_rate=0.0)
    first_page["details"][0].update(paper_id="p1", page_index=0)
    batch.review_results[0].update(filename="第1页.png", image_content_hash="1" * 64, result=first_page)
    batch.review_results.append({
        "review_id": "p3", "paper_id": "p3", "filename": "第2页.png",
        "student_name": "已存在", "student_no": "001",
        "identity_assignment": {"action": "existing", "student_id": 10, "name": "已存在", "student_no": "001"},
        "image_content_hash": "3" * 64,
        "result": {
            "total": 1, "correct_count": 1, "partial_count": 0, "wrong_count": 0,
            "needs_review_count": 0, "correct_rate": 1.0,
            "details": [{"question_no": "2", "question": "题目二", "result": "correct",
                         "paper_id": "p3", "page_index": 1}],
        },
        "knowledge_items": [], "disposition": "active", "manual_corrections": [],
    })
    batch.results.append({"review_id": "p3", "paper_id": "p3", "student_name": "已存在", "disposition": "active"})
    batch.review_results[1]["disposition"] = batch.results[1]["disposition"] = "voided"
    batch.save_meta()

    archived = client.post("/api/batch-grade/review/archive-all", json={"batch_id": "review-batch"})
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived"] == 1
    db = Session()
    grading = db.query(GradingResult).one()
    profile = db.query(StudentProfile).one()
    assert (grading.total_questions, grading.correct_count, grading.partial_count, grading.wrong_count,
            grading.correct_rate) == (9, 8, 0, 1, round(8 / 9, 4))
    assert (profile.total_questions, profile.correct_count, profile.wrong_count) == (9, 8, 1)
    db.close()


def test_teacher_confirm_on_an_archived_result_reaches_every_box():
    Session = _session_factory()
    db = Session()
    try:
        teacher = User(id=1, name="教师", role="teacher")
        db.add(teacher)
        db.commit()
        db.add(Student(id=1, teacher_id=teacher.id, name="学生", student_no="001"))
        db.commit()
        record = GradingResult(student_id=1, total_questions=8, correct_count=7, wrong_count=1, correct_rate=0.875)
        record.set_details(graded("daily")["details"])
        db.add(record)
        db.commit()

        confirm_judgment(record.id, ConfirmJudgmentRequest(action="correct", detail_index=0), db=db, user=teacher)

        saved = db.get(GradingResult, record.id)
        assert box_verdicts(saved.get_details()[0]) == ["correct"] * 8
        assert (saved.total_questions, saved.correct_count, saved.wrong_count, saved.correct_rate) == (8, 8, 0, 1.0)
    finally:
        db.close()


def test_answer_sheet_decision_reaches_every_box(client_env):  # noqa: F811
    make_client, upload_dir = client_env
    detail = graded("daily")["details"][0]
    detail["paper_ids"] = ["student_1"]
    session = _manual_decision_session(upload_dir, details=[detail])
    saved = make_client(1).post(f"/api/answer-sheet/sessions/{session.session_id}/grading/decision", json={
        "student_id": 101, "review_revision": 0,
        "decisions": [{"question_no": "1", "result": "wrong", "note": ""}],
    })
    assert saved.status_code == 200, saved.text
    result = saved.json()["draft"]["result"]
    assert box_verdicts(result["details"][0]) == ["wrong"] * 8
    assert counts(result) == (0, 0, 8, 0, 8, 0.0)


@pytest.mark.parametrize("declared_total", [8, 1], ids=["per answer box", "whole question, saved before this fix"])
def test_projection_audit_accepts_answer_box_totals(declared_total):
    Session = _session_factory()
    db = Session()
    try:
        db.add(User(id=1, name="教师", role="teacher"))
        db.commit()
        db.add(Student(id=1, teacher_id=1, name="学生", student_no="001"))
        db.commit()
        record = GradingResult(student_id=1, total_questions=declared_total, correct_count=7, wrong_count=1)
        record.set_details(graded("daily")["details"])
        db.add(record)
        db.commit()
        audit = audit_grading_result_projection(db, grading_result=record)
        assert (audit["skip_reason"], audit["repairable"]) == (None, True)
    finally:
        db.close()
