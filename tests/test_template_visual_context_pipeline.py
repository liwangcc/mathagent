"""Offline acceptance of the directory / transcription / image-solving contract."""
import asyncio
import base64
import copy
import io
import json

import pytest
from PIL import Image

from services import analyzer, ocr_service, template_directory_pipeline as pipeline
from services.grading_contract import AnswerSpec
from services.teaching_context import make_teaching_context, use_teaching_context
from services.template_session import TemplateSession
from tests import legacy_template_hierarchy as legacy


@pytest.fixture(autouse=True)
def no_live_model_calls(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Live model calls are forbidden in these contract tests")
    monkeypatch.setattr(analyzer, "_call_api", forbidden)
    monkeypatch.setattr(ocr_service, "_post_vision_request", forbidden)


def hierarchy():
    return legacy._validate_hierarchy({"nodes": [
        {"id": "1", "parent_id": None, "raw_label": "基础练习", "area_kinds": []},
        {"id": "1.1", "parent_id": "1", "raw_label": "1", "area_kinds": ["answer", "answer"]},
    ], "needs_review": []})


def transcription():
    return {"questions": [{"q_id": "Q1.1", "question_text": "求 A、B 处气温",
                            "solve_visual_context": {
                                "facts": ["基准 500m，27℃", "A 700m", "B 1000m"],
                                "relations": ["每升高 100m 降温 0.6℃"],
                                "uncertain": ["图中另一处标记不清"]}}]}


def answer(status="ready"):
    return {"answers": [{"q_id": "Q1.1", "question_text": "求 A、B 处气温",
                          "standard_answer": "25.8℃；24℃", "answer_slots": ["25.8℃", "24℃"],
                          "solution_steps": [], "answer_variants": [], "answer_status": status}],
            "directory_issues": []}


@pytest.mark.parametrize("status", ["ready", "needs_review"])
def test_parallel_original_images_attachment_and_solver_status(tmp_path, monkeypatch, status):
    from services import template_one_three as contract
    from tests.test_template_one_three import branch_payloads, run_pipeline
    source = tmp_path / "paper.png"
    Image.new("RGB", (400, 600), (231, 232, 233)).save(source)
    payloads = branch_payloads(hierarchy())
    answer_row = payloads[contract.SOLVE_PHASE]["answers"][0]
    for slot, value in zip(answer_row["answer_slots"], ["25.8℃", "24℃"]):
        slot["answer"] = value
    answer_row["uncertain"] = ["图中另一处标记不清"] if status == "needs_review" else []
    calls = []
    async def request(images, prompt, phase, validator, **kwargs):
        calls.append((phase, kwargs))
        if phase != "template-directory-v1":
            for path in images:
                with Image.open(path) as picture:
                    assert picture.size == ((200, 600) if phase in {"template-hierarchy-v1", "template-coarse-locator-v1"} else (400, 600))
                    assert picture.getextrema() == ((231, 231), (232, 232), (233, 233))
        if phase == "template-hierarchy-v1":
            return hierarchy()
        if phase == "template-coarse-locator-v1":
            return validator({"coordinate_space": "normalized_1000_v1", "questions": [
                {"question_number": "1", "bbox": [0, 0, 1000, 1000]}]})
        if phase == contract.SOLVE_PHASE:
            assert images[0] == str(source)
            assert len(images) >= 1
            assert "<transcription_prompt>" not in prompt
            assert "图中另一处标记不清" not in prompt
        return validator(payloads[phase])
    monkeypatch.setattr(pipeline, "_request", request)
    result = run_pipeline(source, {"column_layout": {"columns": [
        {"x1_1000": 0, "x2_1000": 500}, {"x1_1000": 500, "x2_1000": 1000}]}})
    assert "error" not in result
    question = result["questions"][0]
    assert question["answer_status"] == status
    assert question["needs_review"] is (status == "needs_review")
    assert question["answer_slots"] == ["25.8℃", "24℃"]
    assert question["solve_visual_context"]["facts"] == ["原图事实"]
    assert [kwargs["reasoning_effort"] for _, kwargs in calls] == ["low", "low", "high", "high", "high"]
    assert result["template_ocr_diagnostics"]["transcription_reasoning_effort"] == "high"
    page = TemplateSession.make_page(0, result["questions"])
    restored = AnswerSpec.model_validate(page["questions"][0]).model_dump()
    assert restored["solve_visual_context"] == question["solve_visual_context"]
    assert restored["answer_slots"] == question["answer_slots"]


@pytest.mark.parametrize("phase,requested,expected", [
    ("template-directory-v1", "high", "high"),
    ("template-merged-v1", "low", "high"),
])
def test_qwen_effort_reaches_wire_without_weakening_fallback(tmp_path, monkeypatch, phase, requested, expected):
    from dataclasses import replace
    from tests.test_ocr_pool_scheduler import _configure_template_scheduler_for_test, _install_chat_transport
    from services.ocr_pool_scheduler import OcrPoolScheduler

    _configure_template_scheduler_for_test(monkeypatch, fallback=True)
    captured = {}
    _install_chat_transport(monkeypatch, [
        {"choices": [{"delta": {"content": '{"ok":true}'}, "finish_reason": "stop"}]},
    ], captured)
    scheduler = OcrPoolScheduler.from_template_config(pool_route="fallback")
    scheduler.template_fallback_pool = replace(scheduler.template_fallback_pool, api_protocol="chat_completions")
    source = tmp_path / "paper.png"
    Image.new("RGB", (100, 100), "white").save(source)

    async def run():
        token = ocr_service.set_vision_phase_key(phase)
        try:
            return await ocr_service._call_multi_pool_vision_request(
                str(source), "template", 16000, scheduler=scheduler, pool_route="fallback", reasoning_effort=requested)
        finally:
            ocr_service.reset_vision_phase_key(token)

    assert asyncio.run(run()) == '{"ok":true}'
    assert captured["body"]["reasoning_effort"] == expected
    assert captured["body"]["enable_thinking"] is True
    assert captured["body"]["max_tokens"] == 16000


@pytest.mark.parametrize("edit_text", [False, True])
def test_template_confirm_reopen_keeps_visual_attachment_and_independent_slots(tmp_path, monkeypatch, edit_text):
    from unittest.mock import AsyncMock
    from tests.test_template_session import _make_app_and_session, _client
    from services.scoring_engine import build_grading_units
    import routers.grade as grade
    from routers import grade_pipeline
    from routers import grade_template

    _engine, session = _make_app_and_session()
    monkeypatch.setattr(grade_pipeline, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_template, "_classify_confirmed_template_knowledge", AsyncMock())
    template = TemplateSession("visual-context", str(tmp_path), teacher_id=1)
    template.status = "ocr_done"
    question = {"question_no": "1.1", "question_text": "求 A、B 处气温", "question_type": "fill",
                "standard_answer": "25.8℃；24℃", "blank_count": 2, "answer_slots": ["25.8℃", "24℃"],
                "directory_node_id": "n002", "directory_parent_id": "n001",
                "solve_visual_context": transcription()["questions"][0]["solve_visual_context"]}
    template.add_page(0, [question])
    template.save_meta()
    sent = {key: value for key, value in question.items() if key not in {
        "solve_visual_context", "directory_node_id", "directory_parent_id"}}
    if edit_text:
        sent["question_text"] = "教师修改了题干"
    response = _client(session, monkeypatch).post("/api/batch-grade/template/confirm", json={
        "template_id": "visual-context", "category": "daily", "pages": [{"page_index": 0, "questions": [sent]}]})
    assert response.status_code == 200, response.text
    saved = TemplateSession.load_meta("visual-context", str(tmp_path))
    reopened = saved.pages[0]["questions"][0]
    if edit_text:
        assert "solve_visual_context" not in reopened
    else:
        assert reopened["solve_visual_context"] == question["solve_visual_context"]
    units = build_grading_units([{"question_no": "1.1", "student_answer": "25.8℃；21℃",
                                  "answer_bboxes": [[10, 10, 20, 20], [30, 10, 40, 20]]}],
                                saved.build_answer_map(), "daily")
    assert [unit["key"] for unit in units] == ["1.1[1]", "1.1[2]"]
    assert [unit["standard_answer"] for unit in units] == ["25.8℃", "24℃"]
    assert [unit["student_answer"] for unit in units] == ["25.8℃", "21℃"]
    assert [unit["answer_bboxes"] for unit in units] == [[[10, 10, 20, 20]], [[30, 10, 40, 20]]]
