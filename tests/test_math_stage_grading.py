"""Capture real call boundaries with synthetic inputs; no upstream requests."""
import asyncio
from types import SimpleNamespace

import pytest

from services.teaching_context import (
    current_teaching_context, make_teaching_context, use_teaching_context,
)
from routers import grade_papers


def test_office_profile_gate_precedes_file_read_and_model(monkeypatch):
    from routers import vision_office
    from fastapi import HTTPException

    monkeypatch.setattr(vision_office, "HERMES_ENABLED", True)
    def forbidden(*args, **kwargs):
        raise AssertionError("missing school stage must not consume model or upload capacity")
    monkeypatch.setattr(vision_office, "check_llm_rate", forbidden)
    monkeypatch.setattr(vision_office, "read_upload_capped", forbidden)
    with pytest.raises(HTTPException) as error:
        asyncio.run(vision_office.vision_office(
            file=SimpleNamespace(filename="synthetic.docx"), image=None,
            message="", format="word", user=SimpleNamespace(id=1),
        ))
    assert error.value.status_code == 428
    assert error.value.detail["code"] == "teaching_profile_required"


def test_grading_cache_and_queued_retry_bind_stage(monkeypatch):
    from services import grader

    seen, keys = [], []
    class Queue:
        async def submit(self, *, coro_factory, idempotency_key, teacher_id):
            keys.append(idempotency_key)
            # A queue worker may have no inherited request context.
            with use_teaching_context({}):
                return await coro_factory()
    async def judge(questions, detail_level, *, teacher_material=None):
        seen.append(current_teaching_context())
        assert questions[0]["question_text"] == "题干原文中含高中二字，不可修改"
        return {str(q["question_no"]): {"result": "correct", "standard_answer": "2"} for q in questions}
    monkeypatch.setattr(grader, "get_grading_queue", lambda: Queue())
    monkeypatch.setattr(grader, "judge_paper", judge)
    monkeypatch.setattr(grader, "image_content_hash", lambda path: "synthetic-shared-image")
    async def run():
        for stage in ("junior", "senior", "junior"):
            with use_teaching_context(make_teaching_context(stage)):
                result = await grader.grade_paper_unified(
                    paper={"questions": [{"question_number": "1", "student_answer": "2", "question_type": "solve"}]},
                    answer_map={"1": {"question_text": "题干原文中含高中二字，不可修改", "standard_answer": "2", "question_type": "solve", "matched": True}},
                    student_name="synthetic", teacher_id=1,
                )
                assert result["correct_count"] == 1
    asyncio.run(run())
    assert [item["education_stage"] for item in seen] == ["junior", "senior", "junior"]
    assert keys[0] == keys[2] and keys[0] != keys[1]


@pytest.mark.parametrize("anonymous", [True, False])
def test_student_ocr_uses_batch_snapshot_not_profile_context(monkeypatch, tmp_path, anonymous):
    from routers import grade
    from services.quick_grader import QuickBatch
    batch = QuickBatch("stage-ocr", str(tmp_path), teaching_context=make_teaching_context("junior"))
    batch.identity_mode = "anonymous" if anonymous else "identified"
    calls = []
    async def identity(*args, **kwargs):
        return {"name": "synthetic", "confidence": 0.9}
    async def recognize(*args, **kwargs):
        calls.append(current_teaching_context())
        return {"questions": []}
    monkeypatch.setattr(grade_papers, "recognize_student_info_with_roster", identity)
    monkeypatch.setattr(grade_papers, "recognize_paper_with_cache", recognize)
    with use_teaching_context(make_teaching_context("senior")):
        result = asyncio.run(grade._recognize_batch_upload(
            batch, "synthetic.png", object(), teacher_id="1", paper_id="p1",
        ))
    assert calls == [make_teaching_context("junior")]
    assert ("student_name" in result) is not anonymous


def test_grading_contract_context_changes_hash_but_empty_keeps_legacy():
    from routers.grade import _build_grading_contract, _grading_contract_hash
    from services.grading_contract import AnswerSpec
    args = dict(mode="precise", category="daily", exam_name="", identity_plan={}, confirmed=[
        AnswerSpec(question_no="1", question_text="1+1", standard_answer="2"),
    ])
    legacy = _build_grading_contract(**args)
    assert "teaching_context" not in legacy
    assert legacy == _build_grading_contract(**args, teaching_context={})
    hashes = {_grading_contract_hash(legacy)}
    for stage in ("junior", "senior"):
        hashes.add(_grading_contract_hash(_build_grading_contract(**args, teaching_context=make_teaching_context(stage))))
    assert len(hashes) == 3


def test_student_ocr_prompt_keeps_routing_marker_and_stage(monkeypatch):
    from services import ocr_service
    seen = []
    async def call(image_path, prompt, **kwargs):
        seen.append(prompt)
        return '{"questions":[{"question_number":"1","student_answer":"2","answer_state":"visible","question_type":"fill"}]}'
    monkeypatch.setattr(ocr_service, "call_vision", call)
    # Exercise the actual transcription function without image/file operations.
    with use_teaching_context(make_teaching_context("junior")):
        result = asyncio.run(ocr_service.recognize_paper("synthetic.png"))
    assert result["questions"][0]["student_answer"] == "2"
    assert seen[0].startswith("[task=student_transcription_only]")
    assert "初中数学" in seen[0] and "高中数学" not in seen[0]
