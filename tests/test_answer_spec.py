import asyncio

import pytest

from services.grading_contract import AnswerSpec
from services.template_session import TemplateSession


def test_answer_spec_normalizes_source_aliases_and_rich_fields():
    spec = AnswerSpec.model_validate({
        "question_number": " 1 ",
        "content": "  求 x  ",
        "question_type": "SOLVE",
        "answer": " x=2 ",
        "answer_variants": ["2", "x = 2", "2"],
        "solution_steps": "移项\n求解",
        "grading_method": "process",
        "chapter": "代数",
        "topic": "方程",
        "matched": True,
    })

    assert spec.question_no == "1"
    assert spec.question_text == "求 x"
    assert spec.question_type == "solve"
    assert spec.standard_answer == "x=2"
    assert spec.answer_variants == ["2", "x = 2"]
    assert spec.solution_steps == ["移项", "求解"]
    assert spec.grading_method == "process"
    entry = spec.to_answer_map_entry(source="teacher_confirmed")
    assert entry["answer_variants"] == ["2", "x = 2"]
    assert entry["solution_steps"] == ["移项", "求解"]
    assert entry["source"] == "teacher_confirmed"


def test_answer_spec_uses_safe_defaults_for_question_type_and_method():
    choice = AnswerSpec.model_validate({
        "question_no": "1", "question_text": "选择", "question_type": "choice", "choice_mode": "single", "standard_answer": "A",
    })
    invalid = AnswerSpec.model_validate({
        "question_no": "2", "question_text": "证明", "question_type": "other", "standard_answer": "略",
        "grading_method": "unknown",
    })

    assert choice.grading_method == "exact"
    assert choice.choice_mode == "single"
    assert invalid.question_type == "solve"
    # Missing/invalid methods cannot impose an unconfirmed process obligation.
    assert invalid.grading_method == ""


def test_template_session_preserves_full_answer_spec():
    page = TemplateSession.make_page(0, [{
        "question_number": "1",
        "question_text": "求值",
        "question_type": "fill",
        "standard_answer": "2",
        "answer_variants": ["2.0"],
        "solution_steps": ["先化简", "再代入"],
        "grading_method": "equivalent",
        "chapter": "代数",
        "topic": "求值",
    }])
    session = TemplateSession("t1", "unused")
    session.pages = [page]

    question = page["questions"][0]
    assert question["answer_variants"] == ["2.0"]
    assert question["solution_steps"] == ["先化简", "再代入"]
    assert question["grading_method"] == "equivalent"
    answer_entry = session.build_answer_map()["1"]
    assert answer_entry["answer_variants"] == ["2.0"]
    assert answer_entry["solution_steps"] == ["先化简", "再代入"]
    assert answer_entry["grading_method"] == "equivalent"
    assert answer_entry["chapter"] == "代数"


def test_grader_uses_variants_for_choice_and_passes_rich_spec_to_llm(monkeypatch):
    from services import grader

    captured = {}

    async def fake_judge(questions, detail_level="detailed", *, teacher_material=None):
        captured["questions"] = questions
        return {
            "2": {
                "result": "correct", "explanation": "", "hint": "", "model_answer": "",
                "standard_answer": "x=2", "error_type": "",
            }
        }

    class ImmediateQueue:
        async def submit(self, coro_factory, **kwargs):
            return await coro_factory()

    monkeypatch.setattr(grader, "judge_paper", fake_judge)
    monkeypatch.setattr(grader, "get_grading_queue", lambda: ImmediateQueue())

    result = asyncio.run(grader.grade_paper_unified(
        paper={
            "image_path": "",
            "questions": [
                {"question_no": "1", "student_answer": "B"},
                {"question_no": "2", "student_answer": "x=2"},
            ],
        },
        answer_map={
            "1": {
                "matched": True, "question_type": "choice", "question_text": "选择",
                "standard_answer": "A", "answer_variants": ["B"],
            },
            "2": {
                "matched": True, "question_type": "solve", "question_text": "解方程",
                "standard_answer": "x=2", "answer_variants": ["2"],
                "solution_steps": ["移项", "求解"], "grading_method": "process",
            },
        },
        student_name="学生",
        teacher_id=1,
    ))

    assert result["correct_count"] == 2
    assert [q["question_no"] for q in captured["questions"]] == ["2"]
    llm_question = captured["questions"][0]
    assert llm_question["answer_variants"] == ["2"]
    assert llm_question["solution_steps"] == ["移项", "求解"]
    assert llm_question["grading_method"] == "process"


def test_whole_paper_prompt_marks_reference_as_non_exhaustive_and_includes_rubric():
    from services.flash_grading import build_text_messages

    prompt = build_text_messages([{
        "question_no": "1",
        "question_text": "解方程",
        "standard_answer": "x=2",
        "answer_variants": ["2"],
        "solution_steps": ["移项", "求解"],
        "grading_method": "process",
    }], "detailed")[0]["content"]

    assert "参考步骤不要求照搬" in prompt
    assert "接受数学等价表达和其他正确解法" in prompt
    assert '"answer_variants":["2"]' in prompt
    assert '"solution_steps":["移项","求解"]' in prompt
    assert '"grading_method":"process"' in prompt


@pytest.mark.parametrize('question_type', ['choice', 'fill', 'solve'])
def test_pure_drawing_standard_may_be_empty_while_mixed_text_slots_cannot(question_type):
    from services.grading_contract import finalize_answer_spec_for_category
    raw = {'question_no': '1', 'question_type': question_type, 'answer_kind': 'drawing',
           'question_text': '按题干要求作图', 'standard_answer': '', 'blank_count': 1,
           'drawing_regions': [[100, 100, 900, 900]], 'label_anchor': [90, 300],
           'slot_ids': ['1[1]'], 'region_kinds': ['drawing'], 'answer_slots': ['']}
    result = finalize_answer_spec_for_category(raw, 'daily')
    assert result.standard_answer == '' and result.answer_kind == 'drawing'
    with pytest.raises(ValueError):
        finalize_answer_spec_for_category({**raw, 'answer_kind': 'mixed', 'blank_count': 2,
            'slot_ids': ['1[1]', '1[2]'], 'region_kinds': ['drawing', 'answer'], 'answer_slots': ['', '']}, 'daily')
