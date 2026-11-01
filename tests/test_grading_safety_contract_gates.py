import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from routers import grade
from services.quick_grader import QuickBatch


def _complete_batch(tmp_path, *, status: str = "ocr_done") -> QuickBatch:
    batch = QuickBatch("contract-gate-batch", str(tmp_path))
    batch.teacher_id = 7
    batch.class_id = 9
    batch.category = "daily"
    batch.grade_mode = "precise"
    batch.status = status
    batch.uploads = [{"paper_id": "paper-1", "image_path": "paper.png"}]
    batch.ocr_jobs = [{
        "paper_id": "paper-1",
        "image_path": "paper.png",
        "status": "succeeded",
    }]
    batch.papers = [{
        "paper_id": "paper-1",
        "image_path": "paper.png",
        "orientation_status": "auto_passed",
        "questions": [{
            "question_no": "1",
            "question_number": "1",
            "question_text": "1+1=？",
            "question_type": "fill",
            "student_answer": "2",
            "answer_state": "visible",
        }],
    }]
    return batch


def test_complete_ocr_does_not_override_failed_batch_status(tmp_path):
    batch = _complete_batch(tmp_path, status="failed")

    with pytest.raises(HTTPException) as exc:
        grade._require_complete_batch_ocr(batch)

    assert exc.value.status_code == 409
    assert "失败" in str(exc.value.detail)


def test_confirm_rejects_incomplete_frozen_answer_contract_before_side_effects(
    tmp_path, monkeypatch
):
    batch = _complete_batch(tmp_path)
    batch.source_mode = "template"
    batch.template_id = "contract-gate-template"
    # The frozen answers cover question 1 only.
    batch.answer_map = {"1": {
        "question_text": "1+1=？",
        "question_type": "fill",
        "standard_answer": "2",
    }}
    batch.questions = [
        {"question_no": "1", "question_text": "1+1=？", "question_type": "fill"},
        {"question_no": "2", "question_text": "2+2=？", "question_type": "fill"},
    ]
    batch.papers[0]["questions"].append({
        "question_no": "2",
        "question_number": "2",
        "question_text": "2+2=？",
        "question_type": "fill",
        "student_answer": "4",
        "answer_state": "visible",
    })
    monkeypatch.setattr(grade.QuickBatch, "load_meta", lambda *_args, **_kwargs: batch)
    monkeypatch.setattr(batch, "save_meta", lambda: None)

    class _DB:
        def query(self, *_args, **_kwargs):
            raise AssertionError("database must not be touched for an incomplete answer contract")

    request = grade.ConfirmReq(
        batch_id=batch.batch_id,
        category="daily",
        identity_resolutions=[],
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(grade.batch_grade_confirm(
            request,
            user=SimpleNamespace(id=7),
            db=_DB(),
        ))

    assert exc.value.status_code == 409
    assert "缺失题目" in str(exc.value.detail)
