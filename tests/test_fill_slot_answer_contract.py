"""A fill question's stem is context; its slot answers are what the student must write.

Synthetic page only. The solver restates the printed stem in its whole-question
answer. That text must not reach the text grader as teacher-confirmed material,
or the grader may read it as something the student has to copy. Checked on the
actual grading request, after generation, layout save, reopen, confirm and
reopen again, and for a template confirmed before the fix (never rewritten).
"""
import asyncio
import copy
import json
from unittest.mock import AsyncMock

from PIL import Image

import routers.grade as grade
from routers import grade_template_confirm
from services import analyzer, flash_grading as flash, grader
from services import template_directory_pipeline as pipeline
from services import template_one_three as one_three
from services.quick_grader import QuickBatch
from services.template_evidence import build_template_artifact
from services.template_session import TemplateSession
from tests.grade_family import set_grade_global
from tests.test_template_session import _client, _make_app_and_session
from tests.test_template_shared_slots import HIERARCHY_PHASE, LOCATOR_PHASE, run_pipeline
from tests.test_text_grading_context import ImmediateQueue

STEM = ("填空。2÷5=（ ）；把2米长的绳子平均分成5段，每段长（ ）米；4个1/5是（ ）；"
        "（ ）里面有2个1/5；2/5里面有（ ）个1/5；1-1/5=（ ）。")
STEM_MARK = "把2米长的绳子平均分成5段"
# What the solver returned as the whole-question answer: the stem, blanks filled in.
RESTATED_STEM = ("2÷5=2/5；把2米长的绳子平均分成5段，每段长2/5米；4个1/5是4/5；"
                 "2/5里面有2个1/5；2/5里面有2个1/5；1-1/5=4/5")
EXPECTED = ["2/5", "2/5", "4/5", "2/5", "2", "4/5"]
SLOTS = [f"1[{index}]" for index in range(1, 7)]
BOXES = [[60 + 150 * index, 300, 180 + 150 * index, 360] for index in range(6)]
NUMBER_BOX = [20, 300, 50, 330]


def _generate(monkeypatch, tmp_path):
    """One template page through the real generation pipeline, models stubbed."""
    payloads = {
        HIERARCHY_PHASE: {"contract_version": 4, "questions": [{
            "question_number": "1", "printed_path": ["一、填空", "1."],
            "slots": [{"slot_id": slot, "cue": "（ ）"} for slot in SLOTS]}]},
        "template-coarse-locator-v1": {"coordinate_space": "normalized_1000_v1", "questions": [
            {"question_number": "1", "bbox": [0, 0, 1000, 1000]}]},
        LOCATOR_PHASE: {"coordinate_space": "normalized_1000_v1", "needs_review": [], "questions": [{
            "question_number": "1", "question_number_bbox": NUMBER_BOX, "question_type": "fill",
            "choice_mode": "unknown", "label_anchor": None,
            "regions": [{"slot_id": slot, "kind": "answer", "bbox": box} for slot, box in zip(SLOTS, BOXES)]}]},
        one_three.SOLVE_PHASE: {"answers": [{
            "q_id": "Q1", "status": "answered", "standard_answer": RESTATED_STEM,
            "answer_items": [{"slot_id": slot, "answer": value, "status": "answered"}
                             for slot, value in zip(SLOTS, EXPECTED)],
            "solution_steps": ["2÷5=2/5"], "visual_facts": [], "uncertain": []}]},
        one_three.TRANSCRIBE_PHASE: {"questions": [{"q_id": "Q1", "question_text": STEM, "uncertain": []}]},
    }

    async def request(images, prompt, phase, validator, **kwargs):
        return validator(copy.deepcopy(payloads[phase]))

    monkeypatch.setattr(pipeline, "_request", request)
    source = tmp_path / "template.png"
    Image.new("RGB", (1000, 1000), "white").save(source)
    return source, run_pipeline(source)


def _client_for(monkeypatch, tmp_path):
    _, factory = _make_app_and_session()
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_template_confirm, "_classify_confirmed_template_knowledge", AsyncMock())
    return _client(factory, monkeypatch, raise_server_exceptions=True)


def _grading_map(template, tmp_path):
    """The answer map the per-paper pipeline grades with, from the frozen template."""
    batch = QuickBatch("0123456789ab", str(tmp_path))
    batch.source_mode, batch.category, batch.template_id = "template", "daily", template.template_id
    grade._initialize_batch_answer_map(batch, 1, template)
    return grade._pipeline_grading_answer_map(batch)


def _install_grader(monkeypatch) -> list:
    """A stand-in text grader that decides each answer from the request alone."""
    captured = []

    async def transport(messages, **kwargs):
        captured.append(copy.deepcopy(messages))
        material = json.loads(messages[0]["content"].split("教师确认资料：", 1)[1])
        references = {number: ref["standard_answer"] for item in material for number, ref in item["answers"].items()}
        results = {}
        for row in json.loads(messages[1]["content"])["answers"]:
            reference = references[row["question_no"]]
            right = row["student_answer"] == reference
            results[row["question_no"]] = {
                "result": "correct" if right else "wrong", "explanation": "" if right else "与教师答案不一致",
                "hint": "", "model_answer": "" if right else reference, "standard_answer": "" if right else reference}
        content = json.dumps({"results": results}, ensure_ascii=False)
        kwargs["response_validator"](content)
        return {"content": content, "receipt": {}, "model": flash.FLASH_MODEL, "parameters": {}, "usage": {}}

    monkeypatch.setattr(grader, "get_grading_queue", ImmediateQueue)
    monkeypatch.setattr(grader, "judge_paper", analyzer.judge_paper)
    monkeypatch.setattr(flash, "call_flash", transport)
    return captured


def _grade(monkeypatch, answer_map, answers):
    """Grade one paper of slot transcriptions; returns the result and the grading request."""
    captured = _install_grader(monkeypatch)
    paper = {"paper_id": "a1b2c3d4e5f6", "questions": [
        {"question_no": number, "student_slots": [
            {"slot_id": slot, "student_answer": value, "answer_state": "visible"} for slot, value in slots.items()]}
        for number, slots in answers.items()]}
    result = asyncio.run(grader.grade_paper_unified(paper, answer_map, "匿名页 1", 1, category="daily"))
    assert len(captured) == 1
    return result, captured[0]


def _material(messages) -> dict:
    return {item["question_no"]: item
            for item in json.loads(messages[0]["content"].split("教师确认资料：", 1)[1])}


def _assert_fill_request(messages, transcript):
    """Stem only as context; the expected responses are the six slot answers by ID."""
    system, user = messages[0]["content"], messages[1]["content"]
    item = _material(messages)["1"]
    assert item["question_text"] == STEM
    assert "standard_answer" not in item
    assert {slot: ref["standard_answer"] for slot, ref in item["answers"].items()} == dict(zip(SLOTS, EXPECTED))
    assert system.count(STEM_MARK) == 1 and STEM_MARK not in user
    sent = [row for row in json.loads(user)["answers"] if row["question_no"].startswith("1[")]
    assert sent == [{"question_no": slot, "student_answer": value} for slot, value in zip(SLOTS, transcript)]


def _slot_results(result) -> list[str]:
    detail = next(row for row in result["details"] if row["question_no"] == "1")
    assert [row["slot_id"] for row in detail["blank_results"]] == SLOTS
    return [row["result"] for row in detail["blank_results"]]


def test_generated_fill_answers_survive_save_reopen_and_confirm_into_the_grading_request(monkeypatch, tmp_path):
    source, generated = _generate(monkeypatch, tmp_path)
    assert "error" not in generated
    question = generated["questions"][0]
    assert question["question_text"] == STEM
    assert (question["slot_ids"], question["answer_slots"]) == (SLOTS, EXPECTED)
    # The draft's whole answer is the slot answers, not the restated stem.
    assert question["standard_answer"] == "2/5; 2/5; 4/5; 2/5; 2; 4/5"
    assert question["answer_status"] == "ready"

    sess = TemplateSession("fill-answer-contract", str(tmp_path), teacher_id=1)
    regions = generated["template_answer_regions"]
    artifact = build_template_artifact(
        str(source), sess.template_dir, 0, generated["questions"],
        question_anchors=generated["question_anchors"], template_answer_regions=regions,
        template_handwriting_regions=copy.deepcopy(regions), template_mode="marked")
    sess.add_page(0, generated["questions"], artifact=artifact)
    sess.status, sess.category, sess.template_mode = "ocr_done", "daily", "marked"
    sess.save_meta()
    client = _client_for(monkeypatch, tmp_path)
    base = f"/api/batch-grade/template/{sess.template_id}"

    saved = client.put(f"{base}/layout-overrides", json={
        "page_index": 0, "columns": [{"column_index": 1, "x1_1000": 0, "x2_1000": 1000}],
        "question_anchors": [{"question_number": "1", "question_number_bbox": NUMBER_BOX}],
        "template_answer_regions": [{"question_number": "1", "regions": BOXES, "slot_ids": SLOTS,
                                     "region_kinds": ["answer"] * 6}],
        "template_handwriting_regions": [{"question_number": "1", "regions": BOXES}],
        "question_schema": [{"question_number": "1", "question_type": "fill", "slot_ids": SLOTS,
                             "region_kinds": ["answer"] * 6, "answer_kind": "non_graphic",
                             "drawing_regions": [], "label_anchor": None, "blank_count": 6,
                             "answer_slots": EXPECTED}]})
    assert saved.status_code == 200, saved.text
    reopened = client.get(f"{base}/recognition").json()["pages"]
    draft = reopened[0]["questions"][0]
    assert (draft["question_text"], draft["answer_slots"], draft["standard_answer"]) == (
        STEM, EXPECTED, question["standard_answer"])

    confirmed = client.post("/api/batch-grade/template/confirm",
                            json={"template_id": sess.template_id, "category": "daily", "pages": reopened})
    assert confirmed.status_code == 200, confirmed.text
    frozen = TemplateSession.load_meta(sess.template_id, str(tmp_path))
    assert frozen.status == "confirmed"
    row = client.get(f"{base}/recognition").json()["pages"][0]["questions"][0]
    assert (row["question_text"], row["slot_ids"], row["answer_slots"], row["standard_answer"]) == (
        STEM, SLOTS, EXPECTED, question["standard_answer"])

    answer_map = _grading_map(frozen, tmp_path)
    result, messages = _grade(monkeypatch, answer_map, {"1": dict(zip(SLOTS, EXPECTED))})
    _assert_fill_request(messages, EXPECTED)
    assert _slot_results(result) == ["correct"] * 6
    assert result["details"][0]["result"] == "correct"

    # A genuinely wrong slot still loses its credit, and only that slot.
    wrong = [*EXPECTED[:2], "1/5", *EXPECTED[3:]]
    result, messages = _grade(monkeypatch, answer_map, {"1": dict(zip(SLOTS, wrong))})
    _assert_fill_request(messages, wrong)
    assert _slot_results(result) == ["correct", "correct", "wrong", "correct", "correct", "correct"]
    assert result["details"][0]["result"] == "partial"


SOLVE_ANSWER = "解：5×4=20（米），答：一共20米。须写出计算过程。"


def test_old_confirmed_restated_stem_stays_on_disk_and_out_of_the_grading_request(monkeypatch, tmp_path):
    fill = {"question_no": "1", "question_type": "fill", "question_text": STEM,
            "standard_answer": RESTATED_STEM, "answer_slots": EXPECTED, "slot_ids": SLOTS,
            "region_kinds": ["answer"] * 6, "blank_count": 6, "answer_kind": "non_graphic"}
    solve = {"question_no": "2", "question_type": "solve", "question_text": "每段长5米，4段一共多少米？",
             "standard_answer": SOLVE_ANSWER, "answer_slots": ["20米"], "slot_ids": ["2[1]"],
             "region_kinds": ["answer"], "blank_count": 1, "answer_kind": "non_graphic"}
    sess = TemplateSession("old-fill-contract", str(tmp_path), teacher_id=1)
    sess.add_page(0, [fill, solve])
    sess.status, sess.category = "confirmed", "daily"
    sess.confirm_current_contract(1)
    sess.save_meta()
    before = open(sess.meta_path, "rb").read()
    client = _client_for(monkeypatch, tmp_path)

    reopened = client.get(f"/api/batch-grade/template/{sess.template_id}/recognition").json()["pages"][0]
    assert reopened["questions"][0]["standard_answer"] == RESTATED_STEM

    template = TemplateSession.load_meta(sess.template_id, str(tmp_path))
    result, messages = _grade(monkeypatch, _grading_map(template, tmp_path),
                              {"1": dict(zip(SLOTS, EXPECTED)), "2": {"2[1]": "20米"}})
    _assert_fill_request(messages, EXPECTED)
    # A solve question keeps its whole answer, with the requirement to show work.
    assert _material(messages)["2"]["standard_answer"] == SOLVE_ANSWER
    assert _slot_results(result) == ["correct"] * 6
    assert open(sess.meta_path, "rb").read() == before
