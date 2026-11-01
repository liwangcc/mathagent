"""Latent grading-flow dead ends found in the 2026-10-04 audit.

Synthetic batches only; provider boundaries are stubbed. Each test pins one
path where a transient or single-paper failure used to strand a whole batch.
"""
import asyncio
import copy
import json

import httpx
import pytest

from services.quick_grader import QuickBatch
from tests.test_batch_calibration_flow import _events, _setup
from tests.test_recognition_resume_finalization import checkpoint, forbid_ocr, isolated_queue  # noqa: F401


# ── OCR failure classification ──────────────────────────────────────────


@pytest.mark.parametrize("error", [
    TimeoutError("timed out waiting for shared model capacity"),
    asyncio.TimeoutError(),
    RuntimeError("KeyPool: 所有Key持续不可用，放弃"),
    RuntimeError("empty content (finish_reason=length, had_reasoning=True)"),
    ValueError("question 3 has invalid answer_state"),
])
def test_transient_vision_failures_keep_their_durable_retry(error):
    from services.model_concurrency import RateBudgetTimeout
    from services.ocr_service import classify_ocr_exception, ocr_failure_result

    retryable, code = classify_ocr_exception(error)
    assert retryable is True, code
    assert classify_ocr_exception(RateBudgetTimeout("budget"))[0] is True
    assert ocr_failure_result(error)["_ocr_retryable"] is True


def test_deterministic_vision_failures_stay_final():
    from services.ocr_service import LocalImagePipelineStalled, classify_ocr_exception

    request = httpx.Request("POST", "https://example.invalid/chat/completions")
    rejected = httpx.HTTPStatusError(
        "bad request", request=request, response=httpx.Response(400, request=request),
    )
    assert classify_ocr_exception(rejected) == (False, "provider_rejected_request")
    assert classify_ocr_exception(LocalImagePipelineStalled("stalled"))[0] is False
    assert classify_ocr_exception(OSError("missing file"))[0] is False


# ── Teacher exclusion of an unrecoverable page ─────────────────────────


def _batch_with_final_failure(tmp_path, *, retryable=False, attempts=2):
    batch = checkpoint(tmp_path, status="ocr_partial_failed", count=3)
    failed = batch.papers.pop()
    batch.ocr_jobs[-1].update(
        status="failed", attempts=attempts, retryable=retryable,
        last_error="本次模板本地配准失败，已停止自动批改该页",
        error_code="ocr_failed",
    )
    batch.save_meta()
    return batch, failed["paper_id"]


def test_excluding_a_final_failure_unblocks_grading_of_the_rest(monkeypatch, tmp_path):
    client, _Session = _setup(monkeypatch, tmp_path)
    from services import grader

    batch, failed_id = _batch_with_final_failure(tmp_path)
    blocked = client.post("/api/batch-grade/confirm", json={
        "batch_id": batch.batch_id, "mode": "auto", "category": "daily",
        "confirmed_answers": [], "identity_resolutions": [],
    })
    assert blocked.status_code == 409, blocked.text

    response = client.post(f"/api/batch-grade/{batch.batch_id}/paper/{failed_id}/exclude")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "ocr_done"
    assert payload["can_grade"] is True
    assert payload["excluded_papers"] == 1
    assert payload["questions"], "the frozen template contract must be ready to grade"
    excluded = next(row for row in payload["papers"] if row["paper_id"] == failed_id)
    assert excluded["ocr_status"] == "excluded"
    assert excluded["requires_manual_review"] is False
    assert excluded["error"]

    # Idempotent: a repeated click returns the same durable state.
    again = client.post(f"/api/batch-grade/{batch.batch_id}/paper/{failed_id}/exclude")
    assert again.status_code == 200
    assert again.json()["excluded_papers"] == 1

    calls = []

    async def judge(questions, detail_level="detailed", *, teacher_material=None):
        calls.append(len(questions))
        return {str(q["question_no"]): {
            "result": "correct", "explanation": "", "hint": "", "model_answer": "",
            "standard_answer": q.get("standard_answer", ""), "error_type": "",
        } for q in questions}

    monkeypatch.setattr(grader, "judge_paper", judge)
    confirm = client.post("/api/batch-grade/confirm", json={
        "batch_id": batch.batch_id, "mode": "auto", "category": "daily",
        "confirmed_answers": [], "identity_resolutions": [],
    })
    events = _events(confirm)
    assert events[-1][0] == "done", events
    assert events[-1][1]["total"] == 2
    restored = QuickBatch.load_meta(batch.batch_id, str(tmp_path))
    assert restored.status == "review_ready"
    assert failed_id not in {row["paper_id"] for row in restored.review_results}


@pytest.mark.parametrize("setup,expected", [
    ("retryable", 409),
    ("succeeded", 409),
    ("grading", 409),
    ("unknown", 404),
])
def test_exclusion_is_only_for_final_failures(monkeypatch, tmp_path, setup, expected):
    client, _Session = _setup(monkeypatch, tmp_path)
    batch, failed_id = _batch_with_final_failure(
        tmp_path, retryable=setup == "retryable", attempts=1 if setup == "retryable" else 2,
    )
    target = failed_id
    if setup == "succeeded":
        target = batch.papers[0]["paper_id"]
    elif setup == "unknown":
        target = "not-a-paper"
    elif setup == "grading":
        batch.status = "grading"
        batch.save_meta()
    before = json.loads(open(batch.meta_path, encoding="utf-8").read())

    response = client.post(f"/api/batch-grade/{batch.batch_id}/paper/{target}/exclude")

    assert response.status_code == expected, response.text
    assert json.loads(open(batch.meta_path, encoding="utf-8").read()) == before


# ── Review mutations require a finished grading pass ──────────────────


@pytest.mark.parametrize("status", ["grading", "grading_interrupted", "ocr_done", "recognition_interrupted"])
@pytest.mark.parametrize("action", ["void", "void-all", "archive-all", "decision"])
def test_review_mutations_reject_a_partially_graded_batch(monkeypatch, tmp_path, status, action):
    client, _Session = _setup(monkeypatch, tmp_path)
    batch = checkpoint(tmp_path, status=status, count=2)
    batch.identity_mode = "identified"
    review = {
        "paper_id": "paper-0", "review_id": "paper-0", "disposition": "active",
        "identity_assignment": {"action": "create", "name": "张三", "student_no": "01"},
        "result": {"details": [{"question_no": "1", "result": "correct"}], "total": 1,
                   "needs_review_count": 0},
    }
    batch.review_results = [review]
    batch.results = [{"paper_id": "paper-0", "review_id": "paper-0", "disposition": "active"}]
    batch.save_meta()
    before = json.loads(open(batch.meta_path, encoding="utf-8").read())

    body = {"batch_id": batch.batch_id}
    if action == "void":
        body.update(paper_id="paper-0", reason="test")
    if action == "decision":
        body.update(paper_id="paper-0", review_revision=0,
                    decisions=[{"question_no": "1", "result": "wrong", "note": ""}])
    response = client.post(f"/api/batch-grade/review/{action}", json=body)

    assert response.status_code == 409, response.text
    assert json.loads(open(batch.meta_path, encoding="utf-8").read()) == before


# ── Rubric pin and checkpoint snapshot ─────────────────────────────────


def test_background_knowledge_point_does_not_invalidate_finished_grades(tmp_path):
    import routers.grade as grade

    batch = QuickBatch("rubric-pin", str(tmp_path))
    batch.answer_map = {"1": {"standard_answer": "2", "question_type": "fill", "knowledge_point_id": ""}}
    before = grade._batch_rubric_hash(batch)
    batch.answer_map["1"]["knowledge_point_id"] = "senior.algebra.quadratic"
    assert grade._batch_rubric_hash(batch) == before
    batch.answer_map["1"]["standard_answer"] = "3"
    assert grade._batch_rubric_hash(batch) != before


def test_batch_checkpoint_is_snapshotted_before_the_writer_thread_runs(monkeypatch, tmp_path):
    import routers.grade as grade

    batch = QuickBatch("snapshot", str(tmp_path))
    batch.papers = [{"paper_id": "paper-0", "questions": []}]
    real_to_thread = asyncio.to_thread

    async def to_thread_after_mutation(function, /, *args, **kwargs):
        # Simulate a pipeline task changing the live batch while the writer
        # thread is scheduled: the file must hold the state captured at save.
        batch.papers[0]["grading_error_info"] = {"code": "late"}
        batch.papers.append({"paper_id": "paper-1"})
        return await real_to_thread(function, *args, **kwargs)

    monkeypatch.setattr(grade.asyncio, "to_thread", to_thread_after_mutation)
    asyncio.run(grade._save_quick_batch_meta(batch))

    saved = json.loads(open(batch.meta_path, encoding="utf-8").read())
    assert saved["papers"] == [{"paper_id": "paper-0", "questions": []}]
    restored = QuickBatch.load_meta(batch.batch_id, str(tmp_path))
    assert restored.papers == saved["papers"]


def test_recognition_done_reports_the_durable_batch_status(monkeypatch, tmp_path):
    client, _Session = _setup(monkeypatch, tmp_path)
    batch = checkpoint(tmp_path, status="recognition_interrupted", count=2)
    forbid_ocr(monkeypatch)

    response = client.post(f"/api/batch-grade/{batch.batch_id}/recognize/resume")

    done = [data for event, data in _events(response) if event == "done"]
    assert done and done[0]["status"] == "ocr_done"
    assert done[0]["excluded_papers"] == 0


def test_page_identified_evidence_failure_carries_review_placeholders():
    from services.paper_cache import PaperCache

    cache = PaperCache.from_confirmed_template_pages([
        {"page_index": 0, "questions": [
            {"question_no": "1", "question_text": "1+1=?", "question_type": "fill"},
            {"question_no": "2", "question_text": "2+2=?", "question_type": "solve"},
        ]},
        {"page_index": 1, "questions": [
            {"question_no": "3", "question_text": "3+3=?", "question_type": "fill"},
        ]},
    ])
    label = next(
        name for name, baseline in cache._baselines.items()
        if [q["question_number"] for q in baseline["ocr_result"]["questions"]] == ["3"]
    )
    failure = cache._with_review_placeholders(
        {"error": "答案识别失败", "_ocr_retryable": False}, label,
    )
    assert [q["question_no"] for q in failure["_fail_closed_questions"]] == ["3"]
    assert all(q["slot_identity_error"] == "ocr_failed" for q in failure["_fail_closed_questions"])
    # Pauses and unknown pages never get placeholders.
    pause = {"error": "等待", "_accelerator_state": "pending"}
    assert cache._with_review_placeholders(copy.deepcopy(pause), label) == pause
    assert "_fail_closed_questions" not in cache._with_review_placeholders({"error": "x"}, "")
