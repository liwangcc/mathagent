"""Technical gates are not OCR/Flash verdicts; preserve their attribution."""
import asyncio
import copy
import json
import logging

import pytest

from services import flash_grading as flash, grader
from services.scoring_engine import aggregate_grading_result, build_grading_units
from tests.test_slot_scoring_identity import contract, row


@pytest.mark.parametrize("kind", ["missing_slots", "duplicate_parent", "ocr_failed"])
def test_local_evidence_gate_is_not_attributed_to_flash_or_handwriting(kind, monkeypatch, caplog):
    question = row()
    questions = [question]
    if kind == "missing_slots":
        question.pop("student_slots")
    elif kind == "duplicate_parent":
        questions.append(copy.deepcopy(question))
    else:
        question["slot_identity_error"] = "ocr_failed"
        question.pop("student_slots")

    async def forbidden(*args, **kwargs):
        raise AssertionError("An evidence rejection must not call Flash")

    monkeypatch.setattr(grader, "judge_paper", forbidden)
    with caplog.at_level(logging.WARNING, logger="services.grader"):
        result = asyncio.run(grader.grade_paper_unified(
            {"paper_id": "5b681cbd1134", "questions": questions},
            contract(), "private-student-label", 1, category="exam",
        ))
    # Exercise the same JSON persistence boundary as a review snapshot.
    detail = json.loads(json.dumps(result))["details"][0]
    assert result["needs_review_count"] == 1
    assert result["score"] is None
    assert detail["error_type"] == ("ocr_failed" if kind == "ocr_failed" else "ocr_slot_identity_invalid")
    assert detail["match_method"] == "evidence_gate"
    assert "grading_receipt" not in detail
    assert all(x["result"] == "needs_review" for x in detail["blank_results"])
    event = next(json.loads(r.message) for r in caplog.records if '"event": "grading.evidence_rejected"' in r.message)
    assert event["paper_id"] == "5b681cbd1134"
    assert event["rejected_units_sent_to_grader"] is False
    assert sum(event["source_counts"].values()) == 3
    assert "private-student-label" not in caplog.text
    assert "display must not be scored" not in caplog.text


def test_evidence_gate_overrides_misleading_model_provenance():
    question = row()
    question.pop("student_slots")
    units = build_grading_units([question], contract(), "exam")
    result = aggregate_grading_result(units, {
        u["key"]: {"result": "correct", "error_type": "ocr_unreadable"}
        for u in units
    }, "exam", "synthetic")
    assert result["details"][0]["error_type"] == "ocr_slot_identity_invalid"
    assert result["details"][0]["match_method"] == "evidence_gate"
    assert result["score"] is None


def test_genuine_transcription_unreadable_keeps_ocr_source(monkeypatch):
    question = row()
    for slot in question["student_slots"]:
        slot.update(student_answer="[无法识别]", answer_state="unreadable")

    async def forbidden(*args, **kwargs):
        raise AssertionError("Flash must not redo OCR")

    monkeypatch.setattr(grader, "judge_paper", forbidden)
    result = asyncio.run(grader.grade_paper_unified(
        {"questions": [question]}, contract(), "synthetic", 1, category="exam",
    ))
    detail = result["details"][0]
    assert detail["error_type"] == "ocr_unreadable"
    assert detail["match_method"] == "ocr"
    assert "slot_identity_error" not in detail


@pytest.mark.parametrize("invalid", ["unreadable", "无法识别", "needs_review"])
def test_flash_cannot_turn_an_unreadable_response_into_a_saved_verdict(invalid, monkeypatch):
    class ImmediateQueue:
        async def submit(self, coro_factory, **kwargs):
            return await coro_factory()

    async def transport(messages, **kwargs):
        ids = list(json.loads(messages[1]["content"])["answers"])
        payload = {"results": {x["question_no"]: {
            "result": invalid, "explanation": "invalid model abstention", "hint": "",
            "model_answer": "", "standard_answer": "",
        } for x in ids}}
        return {"content": json.dumps(payload), "receipt": {},
                "model": "qwen3.8-flash", "parameters": {}, "usage": {}}

    monkeypatch.setattr(grader, "get_grading_queue", ImmediateQueue)
    monkeypatch.setattr(flash, "call_flash", transport)
    with pytest.raises(grader.GradingModelUnavailable):
        asyncio.run(grader.grade_paper_unified(
            {"questions": [row()]}, contract(), "synthetic", 1, category="exam",
        ))
