import asyncio
import json
from unittest.mock import AsyncMock

from services import grader
from services.grader import grade_paper_unified, validate_transcription_integrity
from services.paper_cache import PaperCache


def _cache() -> PaperCache:
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
                "question_no": "21",
                "question_text": "填写答案",
                "question_type": "fill",
                "standard_answer": "4/5",
            },
            {
                "question_no": "22",
                "question_text": "解答题",
                "question_type": "solve",
                "standard_answer": "x=2",
            },
        ],
    }])


def _transcription_response() -> str:
    return json.dumps({
        "questions": [
            {
                "question_number": "1",
                "student_answer": "A",
                "answer_state": "visible",
                "confidence": "high",
            },
            {
                "question_number": "21",
                "student_answer": r"4\sqrt{5}",
                "answer_state": "visible",
                "confidence": "low",
            },
            {
                "question_number": "22",
                "student_answer": "x=2",
                "answer_state": "visible",
                "confidence": "high",
            },
        ],
    }, ensure_ascii=False)


def test_full_height_split_prompt_has_no_rubric_or_standard_answer(monkeypatch):
    import config

    monkeypatch.setattr(config, "VISUAL_GRADING_SPLIT_TRANSCRIPTION", True, raising=False)
    cache = _cache()

    prompt = cache._build_evidence_only_prompt(
        "page_1",
        evidence_strategy="full_height_columns",
    )

    assert "[task=student_transcription_only]" in prompt
    assert '题号清单=["1","21","22"]' in prompt
    assert '"1":"choice:single"' in prompt
    assert "standard_answer" not in prompt
    assert "rubric_hash" not in prompt
    assert "solution_steps" not in prompt
    assert "根号、分数" in prompt
    assert "冻结的模板分栏规则" in prompt
    assert "模板差分后的学生卷" in prompt
    assert "不完整题干" in prompt
    assert "蓝色 Q 题号标签" in prompt
    assert "只抄可见笔迹" in prompt
    assert "不解题、不判分" in prompt
    assert "answer_state" in prompt
    assert "unreadable" in prompt
    assert "MiMo" not in prompt
    assert "Qwen" not in prompt
    assert "DeepSeek" not in prompt


def test_split_ocr_returns_transcription_markers_without_visual_judgment(monkeypatch):
    import config

    monkeypatch.setattr(config, "VISUAL_GRADING_SPLIT_TRANSCRIPTION", True, raising=False)
    call = AsyncMock(return_value=_transcription_response())
    monkeypatch.setattr("services.ocr_service.call_vision_images", call)
    cache = _cache()

    result = asyncio.run(cache._recognize_answers_only(
        "left.jpg",
        "page_1",
        evidence_mode=True,
        evidence_strategy="full_height_columns",
        evidence_paths=("left.jpg", "right.jpg"),
    ))

    assert "error" not in result
    assert result["_transcription_only"] is True
    assert result["_requires_separate_grading"] is True
    assert "_visual_grading" not in result
    assert result["questions"][1]["student_answer"] == r"4\sqrt{5}"
    assert call.await_args.kwargs["max_tokens"] == config.OCR_TRANSCRIPTION_MAX_OUTPUT_TOKENS
    assert "response_validator" not in call.await_args.kwargs
    prompt = call.await_args.args[1]
    assert "standard_answer" not in prompt
    assert "rubric_hash" not in prompt


def test_split_ocr_preserves_literal_radical_glyph_from_li_shuo_fixture(monkeypatch):
    """The Li Shuo regression keeps ``4√5`` byte-for-byte through OCR."""
    import config

    monkeypatch.setattr(config, "VISUAL_GRADING_SPLIT_TRANSCRIPTION", True, raising=False)
    response = json.loads(_transcription_response())
    response["questions"][1]["student_answer"] = "4√5"
    call = AsyncMock(return_value=json.dumps(response, ensure_ascii=False))
    monkeypatch.setattr("services.ocr_service.call_vision_images", call)
    cache = _cache()

    result = asyncio.run(cache._recognize_answers_only(
        "li-shuo-page-21-left.jpg",
        "page_1",
        evidence_mode=True,
        evidence_strategy="full_height_columns",
        evidence_paths=("li-shuo-page-21-left.jpg", "li-shuo-page-21-right.jpg"),
    ))

    assert "error" not in result
    assert result["questions"][1]["question_number"] == "21"
    assert result["questions"][1]["student_answer"] == "4√5"
    assert result["questions"][1]["answer_state"] == "visible"


def test_validate_transcription_integrity_allows_absent_echo_and_rejects_rewrite():
    unit = {"student_answer": r"4\sqrt{5}"}

    assert validate_transcription_integrity(unit, {"result": "wrong"})
    assert validate_transcription_integrity(
        unit,
        {"student_answer": r"4\sqrt{5}", "result": "wrong"},
    )
    assert not validate_transcription_integrity(
        unit,
        {"student_answer": "4/5", "result": "wrong"},
    )


class _ImmediateQueue:
    async def submit(self, coro_factory, **_kwargs):
        return await coro_factory()


def _split_solve_paper(student_answer: str = r"4\sqrt{5}") -> tuple[dict, dict]:
    answer_map = {
        "21": {
            "matched": True,
            "question_type": "solve",
            "question_text": "填写并说明",
            "standard_answer": "4/5",
            "grading_method": "process",
        },
    }
    return ({
        "image_path": "paper.jpg",
        "ocr_transcription_only": True,
        "ocr_requires_separate_grading": True,
        "questions": [{
            "question_no": "21",
            "question_type": "solve",
            "student_answer": student_answer,
            "answer_state": "visible",
        }],
    }, answer_map)


def test_split_transcription_passes_exact_text_to_the_grader(monkeypatch):

    captured = {}

    async def judge(questions, detail_level="detailed", *, teacher_material=None):
        captured["questions"] = questions
        captured["detail_level"] = detail_level
        return {
            "21": {
                "result": "wrong",
                "explanation": "学生答案与参考答案不一致",
                "hint": "",
                "model_answer": "",
                "standard_answer": "4/5",
                "error_type": "计算",
            },
        }

    monkeypatch.setattr(grader, "get_grading_queue", lambda: _ImmediateQueue())
    monkeypatch.setattr(grader, "judge_paper", judge)
    paper, answer_map = _split_solve_paper()

    result = asyncio.run(grade_paper_unified(
        paper,
        answer_map,
        "学生",
        teacher_id=1,
    ))

    assert captured["questions"] == [{
        "question_no": "21",
        "question_text": "填写并说明",
        "student_answer": r"4\sqrt{5}",
        "standard_answer": "4/5",
        "answer_variants": [],
        "solution_steps": [],
        "grading_method": "process",
    }]
    assert result["wrong_count"] == 1
    assert result["needs_review_count"] == 0


def test_rewritten_text_grader_echo_is_downgraded_to_review(monkeypatch):

    async def judge(_questions, detail_level="detailed", *, teacher_material=None):
        return {
            "21": {
                "student_answer": "4/5",
                "result": "wrong",
                "explanation": "",
                "hint": "",
                "model_answer": "",
                "standard_answer": "4/5",
                "error_type": "计算",
            },
        }

    monkeypatch.setattr(grader, "get_grading_queue", lambda: _ImmediateQueue())
    monkeypatch.setattr(grader, "judge_paper", judge)
    paper, answer_map = _split_solve_paper()

    result = asyncio.run(grade_paper_unified(
        paper,
        answer_map,
        "学生",
        teacher_id=1,
    ))

    assert result["needs_review_count"] == 1
    assert result["details"][0]["student_answer"] == r"4\sqrt{5}"
    assert "error_type" not in result["details"][0]
