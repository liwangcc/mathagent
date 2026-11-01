"""Isolated math-stage contracts for exercises, downloads and teacher chat."""

import asyncio
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from services.teaching_context import make_teaching_context, use_teaching_context


def _question():
    return {
        "type": "fill", "question": "x+1=3，则x=____。", "answer": "2",
        "solution": "等式两边同时减去1。", "knowledge_point": "一元一次方程",
    }


def _document_xml(data):
    with ZipFile(BytesIO(data)) as document:
        return document.read("word/document.xml").decode("utf-8")


def test_knowledge_tree_uses_only_selected_catalog():
    from services.math_curriculum import get_math_catalog
    from services.practice import load_knowledge_points

    for stage in ("junior", "senior"):
        catalog = get_math_catalog(stage)
        tree = load_knowledge_points(stage)["数学"]
        assert set(tree) == {chapter["chapter"] for chapter in catalog}
        assert {topic for topics in tree.values() for topic in topics} == {
            topic for chapter in catalog for topic in chapter["topics"]
        }
    assert load_knowledge_points("junior") != load_knowledge_points("senior")


def test_exercise_snapshot_validates_stage_and_versions_without_guessing():
    from services.exercise_schema import ExercisePaper

    context = make_teaching_context("junior")
    paper = ExercisePaper(questions=[_question()], teaching_context=context)
    context["education_stage"] = "senior"
    assert paper.teaching_context["education_stage"] == "junior"
    assert paper.title == "初中数学练习"
    assert ExercisePaper(questions=[_question()]).teaching_context == {}
    for invalid in ({"education_stage": "primary"}, {"education_stage": "junior"}, {**make_teaching_context("junior"), "rules_version": "injected"}):
        with pytest.raises(ValueError):
            ExercisePaper(questions=[_question()], teaching_context=invalid)


def test_document_subject_comes_from_paper_snapshot_not_active_profile():
    from services.answer_sheet_document import build_answer_sheet_document
    from services.exercise_document import build_exercise_documents

    junior = make_teaching_context("junior")
    with use_teaching_context(make_teaching_context("senior")):
        _student, answer = build_exercise_documents("代数练习", [_question()], teaching_context=junior)
        card = build_answer_sheet_document("代数练习", [_question()], teaching_context=junior)
    assert "科目：初中数学" in _document_xml(answer)
    assert "科目：高中数学" not in _document_xml(answer)
    assert "初中数学答题卡" in _document_xml(card)
    assert "普通高中数学答题卡" not in _document_xml(card)


def test_legacy_document_keeps_original_subject_even_with_new_profile_context():
    from services.exercise_document import build_exercise_documents

    with use_teaching_context(make_teaching_context("junior")):
        _student, answer = build_exercise_documents("历史试卷", [_question()])
    assert "科目：高中数学" in _document_xml(answer)


def test_history_filter_does_not_assign_unlabelled_history_to_current_stage():
    from routers.exercise import _matches_task_stage

    historical = SimpleNamespace(question_snapshot=lambda: {})
    junior = SimpleNamespace(question_snapshot=lambda: {"teaching_context": make_teaching_context("junior")})
    senior = SimpleNamespace(education_stage="senior")
    assert _matches_task_stage(historical)
    with use_teaching_context(make_teaching_context("junior")):
        assert not _matches_task_stage(historical)
        assert _matches_task_stage(junior)
        assert not _matches_task_stage(senior)


def test_new_generation_freezes_context_and_missing_profile_never_queries_bank(monkeypatch):
    from fastapi import HTTPException
    from routers import exercise
    from services.teaching_context import current_teaching_context

    calls = []

    async def generate(_data, _db, _user):
        calls.append(current_teaching_context())
        return {"title": "测试卷", "exercises": [_question()]}

    monkeypatch.setattr(exercise, "_generate_exercises", generate)
    request = exercise.ExerciseGenerateRequest(question_count=1)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(exercise.generate_exercises(request, db=object(), user=SimpleNamespace(id=1)))
    assert caught.value.status_code == 428
    assert calls == []
    user = SimpleNamespace(id=1, education_stage="junior")
    response = asyncio.run(exercise.generate_exercises(request, db=object(), user=user))
    user.education_stage = "senior"
    assert response["teaching_context"] == make_teaching_context("junior")
    assert calls == [make_teaching_context("junior")]
    assert current_teaching_context() == {}


def test_stage_bank_filter_is_exact_or_fail_closed_without_schema():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from models import QuestionBank
    from services.practice import filter_bank_stage

    with Session(create_engine("sqlite:///:memory:")) as session:
        query = session.query(QuestionBank)
        assert filter_bank_stage(query) is query
        for stage in ("primary", "junior", "senior"):
            selected = filter_bank_stage(query, stage)
            compiled = selected.statement.compile()
            if hasattr(QuestionBank, "education_stage"):
                assert stage in compiled.params.values()
                assert "education_stage =" in str(compiled)
            else:
                assert "WHERE false" in str(compiled)
        with pytest.raises(ValueError):
            filter_bank_stage(query, "university")


def test_intent_request_uses_stage_prompt_without_changing_user_content(monkeypatch):
    from services import chat_intent

    sent = []

    async def fake_llm(messages, **kwargs):
        sent.append(messages)
        return '{"intent":"help","params":{},"clarify":""}'

    monkeypatch.setattr(chat_intent, "_call_llm", fake_llm)
    message = "高中数学这几个字是我在引用旧试卷"
    with use_teaching_context(make_teaching_context("junior")):
        asyncio.run(chat_intent.recognize_intent(message, teacher_id="1"))
    assert "本次任务学段为初中" in sent[0][0]["content"]
    assert sent[0][1]["content"] == message


def test_stage_bank_selection_excludes_other_stage_legacy_and_other_teacher():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    import models_user  # noqa: F401 - resolve the users foreign key
    from models import QuestionBank
    from services.exercise_bank_selector import select_exercises_from_bank
    from services.exercise_schema import ExerciseTypeCounts
    from services.practice import get_bank_statistics

    engine = create_engine("sqlite:///:memory:")
    QuestionBank.__table__.create(engine)
    with Session(engine) as session:
        for row_id, stage, owner in ((1, "junior", 7), (2, "senior", 7), (3, "", 7), (4, "junior", 8)):
            session.add(QuestionBank(
                id=row_id, teacher_id=owner, is_public=False, education_stage=stage,
                chapter="有理数与实数", topic="有理数与数轴", question_type="fill",
                question=f"{row_id}+x=5，则x=____。", answer=str(5-row_id),
            ))
        session.commit()
        selected = select_exercises_from_bank(
            session, user_id=7, education_stage="junior",
            counts=ExerciseTypeCounts(choice=0, fill=1, solution=0),
        )
        assert [item.row.id for item in selected] == [1]
        assert session.get(QuestionBank, 1).usage_count == 1
        assert session.get(QuestionBank, 2).usage_count == 0
        assert session.get(QuestionBank, 3).usage_count == 0
        assert get_bank_statistics(session, teacher_id=7, education_stage="junior")["total"] == 1
