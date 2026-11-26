"""The teacher decides a multi-blank question answer box by answer box.

Same synthetic eight-blank question as tests/test_daily_answer_box_verdicts.py:
the grader judges seven blanks correct and one wrong. Each box is decided by
its own id ("1[1]" … "1[8]", shown as 1.1 … 1.8 on the review image); the
question's verdict, counts, marks, exam points and audit follow those verdicts.
"""
import pytest

from models import GradingCorrection, GradingResult
from services.quick_grader import QuickBatch
from tests.test_batch_review_lifecycle import _setup_review_batch, _setup_review_image_batch
from tests.test_daily_answer_box_verdicts import (
    SEVEN_RIGHT_ONE_WRONG, box_decisions, box_verdicts, counts, graded,
)

ALL_RIGHT = ["correct"] * 8


def review_batch(monkeypatch, tmp_path, category="daily"):
    client, Session, upload_dir = _setup_review_batch(monkeypatch, tmp_path)
    batch = QuickBatch.load_meta("review-batch", str(upload_dir))
    batch.review_results[0].update(category=category, result=graded(category))
    batch.review_results[1]["disposition"] = batch.results[1]["disposition"] = "voided"
    batch.save_meta()
    return client, Session, upload_dir


def decide(client, revision, decisions):
    return client.post("/api/batch-grade/review/decision", json={
        "batch_id": "review-batch", "paper_id": "p1", "review_revision": revision, "decisions": decisions,
    })


def test_each_answer_box_takes_its_own_teacher_verdict(monkeypatch, tmp_path):
    client, _session, _upload_dir = review_batch(monkeypatch, tmp_path)
    saved = decide(client, 0, box_decisions(ALL_RIGHT))
    assert saved.status_code == 200, saved.text
    result = saved.json()["review"]["result"]
    detail, = result["details"]
    assert (detail["result"], box_verdicts(detail), counts(result)) == ("correct", ALL_RIGHT, (8, 0, 0, 0, 8, 1.0))
    # The grader's own per-blank verdicts stay as the grader returned them.
    assert [item["result"] for item in detail["blank_results"]] == SEVEN_RIGHT_ONE_WRONG

    first_box_wrong = ["wrong"] + ["correct"] * 7
    saved = decide(client, 1, box_decisions(first_box_wrong))
    assert saved.status_code == 200, saved.text
    review = saved.json()["review"]
    detail, = review["result"]["details"]
    assert (detail["result"], box_verdicts(detail), counts(review["result"])) == (
        "partial", first_box_wrong, (7, 0, 1, 0, 8, 0.875))
    # Only the boxes the teacher changed are audited, each under its own id.
    assert [(item["question_no"], item["old_result"], item["new_result"]) for item in review["manual_corrections"]] == [
        ("1[8]", "wrong", "correct"), ("1[1]", "correct", "wrong")]


@pytest.mark.parametrize("decisions", [
    [{"question_no": "1", "result": "correct"}],
    box_decisions(SEVEN_RIGHT_ONE_WRONG)[:7],
    box_decisions(SEVEN_RIGHT_ONE_WRONG) + [{"question_no": "1[9]", "result": "correct"}],
], ids=["whole question", "a box left out", "a box that does not exist"])
def test_a_decision_names_every_answer_box(monkeypatch, tmp_path, decisions):
    client, _session, upload_dir = review_batch(monkeypatch, tmp_path)
    refused = decide(client, 0, decisions)
    assert refused.status_code == 400, refused.text
    batch = QuickBatch.load_meta("review-batch", str(upload_dir))
    assert (batch.review_revision, batch.review_results[0].get("manual_corrections") or []) == (0, [])
    assert box_verdicts(batch.review_results[0]["result"]["details"][0]) == SEVEN_RIGHT_ONE_WRONG


def test_exam_points_follow_the_teachers_boxes(monkeypatch, tmp_path):
    client, _session, _upload_dir = review_batch(monkeypatch, tmp_path, category="exam")
    saved = decide(client, 0, box_decisions(ALL_RIGHT))
    assert saved.status_code == 200, saved.text
    assert (saved.json()["review"]["result"]["score"], saved.json()["review"]["result"]["max_score"]) == (8.0, 8.0)

    saved = decide(client, 1, box_decisions(["wrong", "wrong"] + ["correct"] * 6))
    assert saved.status_code == 200, saved.text
    result = saved.json()["review"]["result"]
    # One point per blank, given by the teacher's boxes rather than the grader's seven.
    assert (result["score"], result["max_score"], counts(result)) == (6.0, 8.0, (6, 0, 2, 0, 8, 0.75))


def test_preview_marks_one_answer_box_as_saving_does(monkeypatch, tmp_path):
    client, upload_dir, _batch = _setup_review_image_batch(monkeypatch, tmp_path)
    batch = QuickBatch.load_meta("review-batch", str(upload_dir))
    result = graded("daily")
    for index, unit in enumerate(result["details"][0]["grading_units"]):
        unit["answer_bboxes"] = [[60 + index * 110, 200, 150 + index * 110, 240]]
    batch.review_results[0]["result"] = result
    batch.save_meta()
    url = "/api/batch-grade/review-batch/paper/p1/image?variant=review"
    before = client.get(url)
    assert before.status_code == 200, before.text

    def preview(decisions):
        return client.post("/api/batch-grade/review/image-preview", json={
            "batch_id": "review-batch", "paper_id": "p1", "decisions": decisions,
        })

    changed = preview([{"question_no": "1[8]", "result": "correct"}])
    assert changed.status_code == 200, changed.text
    assert changed.content != before.content
    assert decide(client, 0, box_decisions(ALL_RIGHT)).status_code == 200
    assert client.get(url).content == changed.content
    assert preview([{"question_no": "1", "result": "correct"}]).status_code == 400


def test_archive_audits_the_box_and_marks_its_question(monkeypatch, tmp_path):
    client, Session, _upload_dir = review_batch(monkeypatch, tmp_path)
    assert decide(client, 0, box_decisions(ALL_RIGHT)).status_code == 200
    archived = client.post("/api/batch-grade/review/archive-all", json={"batch_id": "review-batch"})
    assert archived.status_code == 200, archived.text
    db = Session()
    try:
        correction = db.query(GradingCorrection).one()
        assert (correction.question_no, correction.old_result, correction.new_result) == ("1[8]", "wrong", "correct")
        grading = db.query(GradingResult).one()
        detail, = grading.get_details()
        assert detail["teacher_regraded"] is True
        assert (grading.total_questions, grading.correct_count, grading.wrong_count, grading.correct_rate) == (
            8, 8, 0, 1.0)
    finally:
        db.close()
