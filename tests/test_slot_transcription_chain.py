"""Slot transcriptions from the OCR model's raw output to the grading request, by slot ID.

A recorded transcript differed from the expected answers in slots 3 and 4
(expected 4/5 and 2/5, recorded 1/5 and 1/5). The original OCR images and raw
response are gone, so this traces the deterministic part of the chain only:
the exact OCR prompt, a synthetic raw response, slot binding at OCR and at
grading, and the grading request. Each slot's text must arrive unchanged under
its own ID, with no stem or answer key in the OCR input, and a recorded wrong
value must be graded as written, never replaced from the answer key.
"""
import asyncio
import json

import pytest
from PIL import Image

from routers import grade_papers
from services import grader
from services.paper_cache import PaperCache
from services.quick_grader import QuickBatch
from services.template_session import TemplateSession
from tests.test_fill_slot_answer_contract import (
    EXPECTED, RESTATED_STEM, SLOTS, STEM, STEM_MARK, _grading_map, _install_grader,
)

RECORDED = ["2/5", "2/5", "1/5", "1/5", "2", "4/5"]
# Raw response order differs from the contract on purpose: identity is the slot ID.
RAW_ORDER = [3, 2, 5, 0, 4, 1]


def _confirmed_template(tmp_path):
    question = {"question_no": "1", "question_type": "fill", "question_text": STEM,
                "standard_answer": RESTATED_STEM, "answer_slots": EXPECTED, "slot_ids": SLOTS,
                "region_kinds": ["answer"] * 6, "blank_count": 6, "answer_kind": "non_graphic"}
    sess = TemplateSession("slot-chain", str(tmp_path), teacher_id=1)
    sess.add_page(0, [question])
    sess.status, sess.category = "confirmed", "daily"
    sess.confirm_current_contract(1)
    sess.save_meta()
    return TemplateSession.load_meta(sess.template_id, str(tmp_path))


def _transcribe(monkeypatch, template, evidence, transcript):
    """The production OCR acceptance point with the model's raw response stubbed."""
    cache = PaperCache.from_confirmed_template_pages(template.pages)
    raw = {"questions": [{"question_number": "1", "student_slots": [
        {"slot_id": SLOTS[index], "student_answer": transcript[index], "answer_state": "visible"}
        for index in RAW_ORDER]}]}
    sent = {}

    async def vision(paths, prompt, **request):
        sent.update(paths=list(paths), prompt=prompt)
        return json.dumps(raw, ensure_ascii=False)

    async def local_evidence(*_args, **_kwargs):
        return await cache._recognize_answers_only(
            str(evidence), "page_1", evidence_mode=True,
            evidence_strategy="full_column_difference", evidence_paths=(str(evidence),))

    monkeypatch.setattr("services.ocr_service.call_vision_images", vision)
    cache._template_evidence_matcher = object()
    cache._require_local_evidence = True
    cache._recognize_with_local_evidence = local_evidence
    return asyncio.run(cache.recognize_page(str(evidence))), sent


@pytest.mark.parametrize("transcript", [EXPECTED, RECORDED], ids=["expected", "recorded"])
def test_each_slot_reaches_the_grading_request_unchanged_under_its_own_id(monkeypatch, tmp_path, transcript):
    template = _confirmed_template(tmp_path)
    evidence = tmp_path / "text-column-1.png"
    Image.new("RGB", (40, 90), "white").save(evidence)

    recognized, sent = _transcribe(monkeypatch, template, evidence, transcript)
    # OCR input: the evidence image and the slot contract, never the stem or a teacher answer.
    assert sent["paths"] == [str(evidence)]
    assert all(slot in sent["prompt"] for slot in SLOTS)
    assert STEM_MARK not in sent["prompt"] and RESTATED_STEM not in sent["prompt"]
    assert not any(value in sent["prompt"] for value in ("2/5", "4/5"))
    # Slot binding at the OCR boundary restores contract order by ID.
    slots = recognized["questions"][0]["student_slots"]
    assert [(slot["slot_id"], slot["student_answer"]) for slot in slots] == list(zip(SLOTS, transcript))

    batch = QuickBatch("0123456789ab", str(tmp_path))
    batch.identity_mode = "anonymous"
    batch.uploads = [{"paper_id": "a1b2c3d4e5f6", "filename": "page.png", "image_path": str(evidence)}]
    assert grade_papers._ingest_recognized_paper(batch, None, 1, "page.png", str(evidence), recognized) is None
    paper = batch.papers[-1]

    captured = _install_grader(monkeypatch)
    result = asyncio.run(grader.grade_paper_unified(paper, _grading_map(template, tmp_path), "匿名页 1", 1))
    sent_answers = json.loads(captured[0][1]["content"])["answers"]
    assert sent_answers == [{"question_no": slot, "student_answer": value} for slot, value in zip(SLOTS, transcript)]
    blanks = result["details"][0]["blank_results"]
    assert [row["slot_id"] for row in blanks] == SLOTS
    assert [row["student_answer"] for row in blanks] == transcript
    if transcript is EXPECTED:
        assert [row["result"] for row in blanks] == ["correct"] * 6
    else:
        # Graded as written: slots 3 and 4 lose credit, nothing is filled from the key.
        assert [row["result"] for row in blanks] == ["correct", "correct", "wrong", "wrong", "correct", "correct"]
        assert result["details"][0]["result"] == "partial" and result["needs_review_count"] == 0
