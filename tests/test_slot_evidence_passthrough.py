"""Slot/identity evidence must survive every question-dict rebuild.

OCR-failed placeholders must stay fail-closed but attribute the gate to OCR
failure instead of showing a misleading slot-identity message.
"""
import asyncio
import json
from types import SimpleNamespace

from services.template_slots import make_slot_ids


def test_ocr_failed_placeholder_stays_fail_closed_with_truthful_message(monkeypatch):
    import routers.grade as grade
    from services import grader
    from services.scoring_engine import build_grading_units

    def build_answer_map():
        return {"2": {
            "question_type": "solve", "standard_answer": "101；102",
            "slot_ids": make_slot_ids("2", 2), "answer_slots": ["101", "102"],
        }}

    session = SimpleNamespace(
        pages=[{"questions": [{"question_no": "2", "question_text": "题干"}]}],
        build_answer_map=build_answer_map,
    )
    placeholders = grade._single_page_template_failure_questions(session)
    assert placeholders
    assert all(q.get("slot_identity_error") == "ocr_failed" for q in placeholders)

    answer_map = build_answer_map()
    units = build_grading_units(placeholders, answer_map, "daily")
    assert units
    assert all(u.get("slot_identity_error") == "ocr_failed" for u in units)
    # Fail-closed evidence only: never invented readable text.
    assert all(
        slot["answer_state"] == "unreadable" and slot["student_answer"] == "[无法识别]"
        for unit in units for slot in unit["student_slots"]
    )

    sent = []

    class ImmediateQueue:
        async def submit(self, coro_factory, **kwargs):
            return await coro_factory()

    async def judge(questions, **kwargs):
        sent.extend(questions)
        return {}

    monkeypatch.setattr(grader, "get_grading_queue", lambda: ImmediateQueue())
    monkeypatch.setattr(grader, "judge_paper", judge)
    final = asyncio.run(grader.grade_paper_unified(
        {"questions": placeholders}, answer_map, "test", 1, category="daily",
    ))
    assert sent == []  # placeholders never reach the model judge
    assert "本题所在页 OCR 未完成" in json.dumps(final, ensure_ascii=False)
    assert "作答位身份无法确认" not in json.dumps(final, ensure_ascii=False)
