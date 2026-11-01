"""Full-height evidence is transcription-only; grading runs separately on Flash."""
import asyncio
import json

from services.paper_cache import (
    PaperCache,
)


def _confirmed_cache() -> PaperCache:
    return PaperCache.from_confirmed_template_pages([{
        "page_index": 0,
        "questions": [
            {
                "question_no": "1",
                "question_text": "选择正确选项",
                "question_type": "choice",
                "choice_mode": "single",
                "standard_answer": "B",
            },
            {
                "question_no": "2",
                "question_text": "解方程 x+1=3",
                "question_type": "solve",
                "standard_answer": "x=2",
                "answer_variants": ["2"],
                "solution_steps": ["移项", "求解"],
                "grading_method": "process",
            },
        ],
    }])


def _model_response() -> str:
    return json.dumps({
        "questions": [
            {
                "question_number": "1",
                "student_answer": "A",
                "answer_state": "visible",
            },
            {
                "question_number": "2",
                "student_answer": "x=3",
                "answer_state": "visible",
            },
        ],
    }, ensure_ascii=False)


def test_full_height_always_uses_transcription_contract_even_when_legacy_switch_is_false(
    monkeypatch,
):
    from unittest.mock import AsyncMock

    import config

    monkeypatch.setattr(config, "VISUAL_GRADING_SPLIT_TRANSCRIPTION", False, raising=False)
    cache = _confirmed_cache()
    call = AsyncMock(return_value=json.dumps({
        "questions": [
            {"question_number": "1", "student_answer": "A", "answer_state": "visible"},
            {"question_number": "2", "student_answer": "x=3", "answer_state": "visible"},
        ],
    }, ensure_ascii=False))
    monkeypatch.setattr("services.ocr_service.call_vision_images", call)

    result = asyncio.run(cache._recognize_answers_only(
        "column.jpg",
        "page_1",
        evidence_mode=True,
        evidence_strategy="full_height_columns",
        evidence_paths=("column.jpg",),
    ))

    assert "error" not in result
    assert result["_transcription_only"] is True
    assert result["_requires_separate_grading"] is True
    assert "_visual_grading" not in result
    assert call.await_args.kwargs["max_tokens"] == config.OCR_TRANSCRIPTION_MAX_OUTPUT_TOKENS
    assert "rubric_hash" not in call.await_args.args[1]
    assert "judgments" not in call.await_args.args[1]


def test_full_height_visual_grading_never_splices_legacy_local_recovery(
    monkeypatch,
):
    from unittest.mock import AsyncMock

    cache = _confirmed_cache()
    monkeypatch.setattr(
        "services.ocr_service.call_vision_images",
        AsyncMock(return_value=_model_response()),
    )
    legacy_recovery = AsyncMock(
        side_effect=AssertionError("legacy recovery must not run")
    )
    monkeypatch.setattr(
        cache,
        "_recover_unreadable_evidence_answers",
        legacy_recovery,
    )

    result = asyncio.run(cache._recognize_answers_only(
        "left.jpg",
        "page_1",
        evidence_mode=True,
        evidence_strategy="client-evidence-multi-v2",
        evidence_paths=("left.jpg", "right.jpg"),
        evidence_diagnostics={"answer_source_complete": True},
    ))

    assert "error" not in result
    legacy_recovery.assert_not_awaited()
