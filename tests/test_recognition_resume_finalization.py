"""Recovery must reuse terminal OCR checkpoints, not mistake them for failures.

Synthetic persisted batches + real HTTP/SSE and pipeline; provider boundaries
are stubbed by the existing calibration fixture. No production data is used.
"""
import asyncio
import copy
import json
import os

import pytest

from models_user import User
from services.quick_grader import QuickBatch
from tests.test_batch_calibration_flow import _confirmed_template, _events, _setup
from routers import grade_availability
from routers import grade_report
from routers import grade_papers


@pytest.fixture(autouse=True)
def isolated_queue(monkeypatch):
    from services import task_queue
    import routers.grade as grade
    from routers import grade_pipeline

    monkeypatch.setattr(task_queue, "_grading_queue", None)

    async def available(_batch):
        return {"status": "available", "message": "synthetic scoring readiness"}

    async def no_report(*_args):
        return None

    monkeypatch.setattr(grade_availability, "_batch_grade_scoring_availability", available)
    monkeypatch.setattr(grade_pipeline, "_start_batch_report_generation", no_report)
    monkeypatch.setattr(grade_report, "_start_batch_report_generation", no_report)


def checkpoint(tmp_path, *, status="recognition_interrupted", count=3):
    batch = QuickBatch("resume-finalization", str(tmp_path))
    batch.teacher_id = 1
    batch.class_id = 1
    batch.identity_mode = "anonymous"
    batch.grade_mode = "auto"
    batch.category = "daily"
    batch.source_mode = "template"
    batch.status = status
    batch.error = "识别连接中断，等待恢复"
    template = _confirmed_template(tmp_path, "finalization-template")
    batch.template_id = template.template_id
    directory = tmp_path / f"quick_batch_{batch.batch_id}"
    directory.mkdir()
    for index in range(count):
        image = directory / f"paper-{index}.png"
        image.write_bytes(f"synthetic-image-{index}".encode())
        upload = {
            "paper_id": f"paper-{index}", "filename": image.name,
            "image_path": str(image), "source_sha256": f"source-{index}",
            "working_sha256": f"working-{index}",
            "orientation_status": "auto_passed", "orientation_reason": "normal_angle",
            "orientation_confidence": 1.0, "detected_tilt_degrees": 0.0,
        }
        batch.uploads.append(upload)
        batch.papers.append({
            **upload, "student_name": "", "student_no": "",
            "identity": {"status": "anonymous"},
            "questions": [{"question_no": "1", "question_type": "fill", "student_answer": "2"}],
        })
    batch.questions = [{"question_no": "1", "question_text": "1+1=?", "question_type": "fill"}]
    batch.ensure_ocr_jobs(batch.uploads)
    for job in batch.ocr_jobs:
        job.update(status="succeeded", attempts=1)
    batch.save_meta()
    return batch


def forbid_ocr(monkeypatch):
    import routers.grade as grade
    from routers import grade_recognition

    async def forbidden(*_args, **_kwargs):
        pytest.fail("Already checkpointed OCR must not be requested again")

    monkeypatch.setattr(grade_papers, "recognize_paper_with_cache", forbidden)
    # Per-upload OCR and the batch inspections both read grade_recognition's binding.
    monkeypatch.setattr(grade_recognition, "_ensure_batch_upload_inspected", forbidden)


@pytest.mark.parametrize("status", ["recognition_interrupted", "ocr_partial_failed", "recognizing_remaining"])
def test_terminal_ocr_checkpoint_finishes_without_recognizing_again(monkeypatch, tmp_path, status):
    client, _Session = _setup(monkeypatch, tmp_path)

    batch = checkpoint(tmp_path, status=status, count=23)
    before_papers = copy.deepcopy(batch.papers)
    before_jobs = copy.deepcopy(batch.ocr_jobs)
    forbid_ocr(monkeypatch)

    response = client.post(f"/api/batch-grade/{batch.batch_id}/recognize/resume")
    assert response.status_code == 200, response.text
    events = _events(response)
    assert not [data for event, data in events if event == "error"], events
    done = [data for event, data in events if event == "done"]
    assert len(done) == 1 and done[0]["can_grade"] is True, events
    assert done[0]["succeeded_papers"] == 23
    assert len(done[0]["questions"]) == 1
    assert len([data for event, data in events if event == "paper"]) == 23
    # Observe raw persistence, not just the potentially healed load_meta view.
    saved = json.loads((tmp_path / f"quick_batch_{batch.batch_id}" / "meta.json").read_text(encoding="utf-8"))
    assert saved["status"] == "ocr_done"
    assert saved["error"] is None and saved["pending_uploads"] == []
    assert saved["ocr_jobs"] == before_jobs
    assert len(saved["papers"]) == len(before_papers)
    assert [p["questions"] for p in saved["papers"]] == [p["questions"] for p in before_papers]
    assert saved["confirmation_questions"] == done[0]["questions"]


@pytest.mark.parametrize("pending_ocr", [False, True])
def test_resume_grades_saved_ungraded_papers_and_preserves_valid_result(monkeypatch, tmp_path, pending_ocr):
    client, Session = _setup(monkeypatch, tmp_path)
    import routers.grade as grade
    from services import grader

    batch = checkpoint(tmp_path)
    calls = []

    async def judge(questions, detail_level="detailed", *, teacher_material=None):
        calls.append([q["question_no"] for q in questions])
        return {str(q["question_no"]): {
            "result": "correct", "explanation": "", "hint": "", "model_answer": "",
            "standard_answer": q.get("standard_answer", ""), "error_type": "",
        } for q in questions}

    monkeypatch.setattr(grader, "judge_paper", judge)
    template = grade._load_owned_template_session(batch.template_id, 1)

    async def seed_one_result():
        with Session() as db:
            grade._initialize_batch_answer_map(batch, 1, template)
            grade._pin_batch_rubric(batch, template)
            pipeline = grade._PerPaperGradingPipeline(batch, db, 1, answer_map=grade._pipeline_grading_answer_map(batch))
            assert pipeline.dispatch(batch.papers[0])
            await asyncio.gather(*pipeline.tasks)
            await pipeline.aclose()

    asyncio.run(seed_one_result())
    assert len(batch.results) == len(batch.review_results) == len(calls) == 1
    saved_result = copy.deepcopy(batch.results[0])
    saved_review = copy.deepcopy(batch.review_results[0])
    if pending_ocr:
        batch.ocr_jobs[-1].update(status="queued", attempts=0)
        batch.papers.pop()

        async def recognize(path, *_args, **_kwargs):
            assert os.path.basename(path) == "paper-2.png"
            return {"student_name": "", "student_no": "", "questions": [{
                "question_number": "1", "question_text": "1+1=?", "question_type": "fill", "student_answer": "2",
            }]}

        monkeypatch.setattr(grade_papers, "recognize_paper_with_cache", recognize)
    else:
        forbid_ocr(monkeypatch)
    batch.save_meta()

    response = client.post(f"/api/batch-grade/{batch.batch_id}/recognize/resume")
    events = _events(response)
    assert events[-1][0] == "done", events
    restored = QuickBatch.load_meta(batch.batch_id, str(tmp_path))
    assert restored.status == "ocr_done"
    assert len(calls) == 3, "Saved but ungraded OCR must resume; valid grades must not run twice"
    assert [row["paper_id"] for row in restored.results] == ["paper-0", "paper-1", "paper-2"]
    assert restored.results[0] == saved_result and restored.review_results[0] == saved_review
    assert len([data for event, data in events if event == "result"]) == 2
    assert [job["attempts"] for job in restored.ocr_jobs] == [1, 1, 1]

    confirm = client.post("/api/batch-grade/confirm", json={
        "batch_id": batch.batch_id, "mode": "auto", "category": "daily", "exam_name": "",
        "confirmed_answers": [], "identity_resolutions": [],
    })
    assert confirm.status_code == 200 and _events(confirm)[-1][0] == "done", confirm.text
    assert len(calls) == 3, "Confirmation must replay the same durable pipeline results"


def test_finalization_still_leaves_unconfirmed_identity_for_teacher(monkeypatch, tmp_path):
    client, _Session = _setup(monkeypatch, tmp_path)
    batch = checkpoint(tmp_path)
    batch.identity_mode = "identified"
    for paper in batch.papers:
        paper["identity"] = {"status": "unmatched"}
    batch.save_meta()
    forbid_ocr(monkeypatch)
    response = client.post(f"/api/batch-grade/{batch.batch_id}/recognize/resume")
    assert _events(response)[-1][0] == "done", response.text
    restored = QuickBatch.load_meta(batch.batch_id, str(tmp_path))
    assert restored.results == restored.review_results == []
    assert restored.status == "ocr_done"


def test_incomplete_terminal_checkpoint_cannot_masquerade_as_done(monkeypatch, tmp_path):
    client, _Session = _setup(monkeypatch, tmp_path)
    batch = checkpoint(tmp_path)
    batch.papers.pop()
    batch.save_meta()
    forbid_ocr(monkeypatch)
    response = client.post(f"/api/batch-grade/{batch.batch_id}/recognize/resume")
    events = _events(response)
    assert [data["code"] for event, data in events if event == "error"] == ["OCR_NO_RETRYABLE_FAILURES"]
    assert not [data for event, data in events if event == "done"]


def test_finalization_done_is_durable_even_if_consumer_closes_immediately(monkeypatch, tmp_path):
    _client, _Session = _setup(monkeypatch, tmp_path)
    batch = checkpoint(tmp_path)
    forbid_ocr(monkeypatch)
    import routers.grade as grade

    async def run():
        response = await grade.batch_grade_resume_recognition(batch.batch_id, User(id=1, role="teacher"))
        async for chunk in response.body_iterator:
            if chunk.startswith("event: done\n"):
                await response.body_iterator.aclose()
                return
        pytest.fail("Finalization must emit done")

    asyncio.run(run())
    restored = QuickBatch.load_meta(batch.batch_id, str(tmp_path))
    assert restored.status == "ocr_done" and restored.error is None
    assert [job["status"] for job in restored.ocr_jobs] == ["succeeded"] * 3
