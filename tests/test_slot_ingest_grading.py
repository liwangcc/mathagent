"""OCR student_slots must survive batch ingest into the unified grader.

Regression: batch ingest used to persist only scalar answer fields, so every
contracted question lost its slot evidence and the grader fail-closed the whole
paper into "作答位身份无法确认" / needs_review.
"""
import asyncio
import json
from unittest.mock import MagicMock

from services.quick_grader import QuickBatch
from services.template_slots import make_slot_ids
from tests.test_slot_cache_contract import contract, response, run_cache


def test_ingested_student_slots_reach_grader_without_slot_identity_error(monkeypatch, tmp_path):
    import routers.grade as grade
    from services import grader
    from services.scoring_engine import build_grading_units

    result, _captured = run_cache(monkeypatch, response())
    assert "error" not in result, result

    image_path = str(tmp_path / "paper.png")
    batch = QuickBatch("slot-ingest", str(tmp_path))
    batch.identity_mode = "anonymous"
    batch.uploads = [{
        "paper_id": "paper00000001",
        "filename": "paper.png",
        "image_path": image_path,
        "original_image_path": image_path,
    }]
    error = grade._ingest_recognized_paper(
        batch,
        MagicMock(),
        1,
        "paper.png",
        image_path,
        {"questions": result["questions"]},
    )
    assert error is None
    stored = batch.papers[0]["questions"]
    assert len(stored) == 1
    assert [slot["slot_id"] for slot in stored[0]["student_slots"]] == make_slot_ids("2", 8)

    units = build_grading_units(stored, {"2": contract()}, "daily")
    assert [unit["student_answer"] for unit in units] == [
        "101", "", "103", "104", "105", "106", "107", "108",
    ]

    class ImmediateQueue:
        async def submit(self, coro_factory, **kwargs):
            return await coro_factory()

    async def judge(questions, **kwargs):
        return {q["question_no"]: {"result": "correct"} for q in questions}

    monkeypatch.setattr(grader, "get_grading_queue", lambda: ImmediateQueue())
    monkeypatch.setattr(grader, "judge_paper", judge)
    final = asyncio.run(
        grader.grade_paper_unified(
            {"questions": stored}, {"2": contract()}, "test", 1, category="exam"
        )
    )
    dumped = json.dumps(final, ensure_ascii=False)
    assert "slot_identity_error" not in dumped
    assert "作答位身份无法确认" not in dumped
    assert final["needs_review_count"] == 0
