"""Answer-sheet stage snapshots, resumed SSE and captured model requests."""

import asyncio
from types import SimpleNamespace

import pytest

from services.answer_sheet_session import AnswerSheetSession
from services.teaching_context import current_teaching_context, make_teaching_context, use_teaching_context
from routers import answer_sheet_availability
from routers import answer_sheet_common
from routers import answer_sheet_sessions
from routers import answer_sheet_source


def _question():
    return {"question_number": "1", "question_text": "x+1=3", "question_type": "fill", "standard_answer": "2"}


def _route_setup(monkeypatch, tmp_path):
    from routers import answer_sheet

    monkeypatch.setattr(answer_sheet_sessions, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(answer_sheet_common, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(answer_sheet_availability, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(answer_sheet_common, "_answer_sheet_writes_enabled", lambda: None)

    async def ready(_user):
        return None

    monkeypatch.setattr(answer_sheet_source, "_require_answer_sheet_chain", ready)
    monkeypatch.setattr(answer_sheet_sessions, "_require_answer_sheet_chain", ready)
    monkeypatch.setattr(answer_sheet, "_require_answer_sheet_chain", ready)
    return answer_sheet


def test_new_session_requires_profile_and_freezes_snapshot(monkeypatch, tmp_path):
    from fastapi import HTTPException

    from services.answer_sheet_paper_templates import PaperTemplateRegistry
    from tests.answer_sheet_redesign_helpers import confirmed_detailed_template

    route = _route_setup(monkeypatch, tmp_path)
    monkeypatch.setattr(answer_sheet_sessions, "_owned_class", lambda *_args: SimpleNamespace(id=1))
    # The class is inherited from the teacher's global current class and the
    # paper semantics come from a frozen paper template of the same stage.
    registry = PaperTemplateRegistry(tmp_path / "paper-registry")
    monkeypatch.setattr(answer_sheet_sessions, "get_paper_template_registry", lambda: registry)
    monkeypatch.setattr(route, "get_paper_template_registry", lambda: registry)
    paper = registry.register(
        confirmed_detailed_template(str(tmp_path), stage="junior"), teacher_id=1,
    )
    req = route.SessionCreateRequest(paper_template_code=paper["template_code"])
    with pytest.raises(HTTPException) as caught:
        asyncio.run(route.create_session(req, db=object(), user=SimpleNamespace(id=1, current_class_id=1)))
    assert caught.value.status_code == 428
    user = SimpleNamespace(id=1, education_stage="junior", current_class_id=1)
    result = asyncio.run(route.create_session(req, db=object(), user=user))
    user.education_stage = "senior"
    restored = AnswerSheetSession.load(result["session_id"], str(tmp_path))
    assert restored.teaching_context == make_teaching_context("junior")


@pytest.mark.parametrize("answer_source", ["embedded", "ai_generated", "separate"])
def test_sync_source_models_receive_session_stage_not_changed_teacher(monkeypatch, tmp_path, answer_source):
    route = _route_setup(monkeypatch, tmp_path)
    junior = make_teaching_context("junior")
    session = AnswerSheetSession("a" * 16, str(tmp_path), teacher_id=1, class_id=1, teaching_context=junior)
    session.status = "source_uploaded"
    session.answer_source = answer_source
    session.source_exam_pages = [{"path": "fake-source.png", "sha256": "source"}]
    session.answer_pages = [{"path": "fake-answer.png", "sha256": "answer"}]
    session.save()
    calls = []

    async def recognize(path):
        calls.append((path, current_teaching_context()))
        return {"questions": [_question()]}

    async def solve(questions):
        calls.append(("solve", current_teaching_context()))
        return {"answers": {"1": {"standard_answer": "2", "model_answer": ["解方程"]}}}

    monkeypatch.setattr(answer_sheet_source, "recognize_template_blank", recognize)
    monkeypatch.setattr(answer_sheet_source, "recognize_template_with_answers", recognize)
    monkeypatch.setattr(answer_sheet_source, "generate_template_answers_batch", solve)
    result = asyncio.run(route.recognize_source(session.session_id, user=SimpleNamespace(id=1, education_stage="senior")))
    assert result["status"] == "source_recognized"
    assert calls and all(context == junior for _, context in calls)
    assert current_teaching_context() == {}


def test_source_sse_resumption_checks_snapshot_and_binds_iteration(monkeypatch, tmp_path):
    route = _route_setup(monkeypatch, tmp_path)
    junior = make_teaching_context("junior")
    session = AnswerSheetSession("b" * 16, str(tmp_path), teacher_id=1, class_id=1, teaching_context=junior)
    session.status = "source_uploaded"
    session.answer_source = "embedded"
    session.source_exam_pages = [{
        "path": "fake.png", "sha256": "source",
        "recognition_checkpoint": {"mode": "embedded", "sha256": "source", "questions": [_question()], "teaching_context": make_teaching_context("senior")},
    }]
    session.save()
    seen = []

    async def recognize(_path):
        seen.append(current_teaching_context())
        return {"questions": [_question()]}

    monkeypatch.setattr(answer_sheet_source, "recognize_template_with_answers", recognize)

    async def consume():
        response = await route.recognize_source_stream(session.session_id, user=SimpleNamespace(id=1, education_stage="senior"))
        chunks = [chunk async for chunk in response.body_iterator]
        return "".join(chunks)

    result = asyncio.run(consume())
    assert 'event: done' in result
    assert seen == [junior]
    assert AnswerSheetSession.load(session.session_id, str(tmp_path)).teaching_context == junior


def test_student_checkpoint_is_stage_bound_but_legacy_format_is_preserved(tmp_path):
    from routers.answer_sheet import _student_recognition_signature

    page = {"sha256": "image"}
    legacy = AnswerSheetSession("c" * 16, str(tmp_path), teacher_id=1, class_id=1)
    legacy.layout_hash = "layout"
    assert _student_recognition_signature(legacy, page) == "layout:image"
    junior = AnswerSheetSession("d" * 16, str(tmp_path), teacher_id=1, class_id=1, teaching_context=make_teaching_context("junior"))
    senior = AnswerSheetSession("e" * 16, str(tmp_path), teacher_id=1, class_id=1, teaching_context=make_teaching_context("senior"))
    junior.layout_hash = senior.layout_hash = "layout"
    assert _student_recognition_signature(junior, page) != _student_recognition_signature(senior, page)


def test_vision_receives_stage_specific_prompt_and_unchanged_region_schema(monkeypatch):
    from services.answer_sheet_acceleration import transcribe_answer_sheet_evidence
    import services.ocr_service as ocr

    sent = []

    async def vision(path, prompt, **kwargs):
        sent.append((path, prompt))
        return '{"questions":[{"question_number":"Q001","student_answer":"2","answer_state":"visible","confidence":0.99}]}'

    async def semaphore(*_args):
        return asyncio.Semaphore(1)

    monkeypatch.setattr(ocr, "call_vision", vision)
    monkeypatch.setattr("services.key_pool.get_ocr_teacher_semaphore", semaphore)
    plan = [{"question_number": "Q001", "kind": "answer", "question_no": "1", "question_type": "fill", "image_index": 1, "answer_bbox_1000": [10, 20, 300, 400]}]
    with use_teaching_context(make_teaching_context("junior")):
        result = asyncio.run(transcribe_answer_sheet_evidence("synthetic.png", plan, teacher_id=1))
    assert "中国初中数学答题区域 OCR" in sent[0][1]
    assert '"answer_bbox_1000":[10,20,300,400]' in sent[0][1]
    assert result[0]["text"] == "2"


def test_grading_call_and_archive_details_keep_the_session_snapshot():
    from routers.answer_sheet import _grade_answer_sheet_group

    junior = make_teaching_context("junior")
    group = {"questions": [{"question_number": "1", "student_answer": "2"}], "student_name": "模拟学生", "pages": ["p1"], "question_pages": {"1": ["p1"]}}
    seen = []

    async def grade(ocr, answers, name, teacher_id, **kwargs):
        seen.append((current_teaching_context(), ocr))
        return {"details": [{"question_no": "1", "result": "correct"}]}

    with use_teaching_context(make_teaching_context("senior")):
        result = asyncio.run(_grade_answer_sheet_group(grade, 10, group, {}, 1, "daily", teaching_context=junior))
    assert seen[0][0] == junior
    assert seen[0][1]["teaching_context"] == junior
    assert result["result"]["details"][0]["teaching_context"] == junior
    assert result["result"]["details"][0]["paper_ids"] == ["p1"]


@pytest.mark.parametrize("streaming", [False, True])
def test_student_recognition_workers_inherit_persisted_stage(monkeypatch, tmp_path, streaming):
    route = _route_setup(monkeypatch, tmp_path)
    junior = make_teaching_context("junior")
    session = AnswerSheetSession("f" * 16, str(tmp_path), teacher_id=1, class_id=1, teaching_context=junior)
    session.status = "recognizing"
    session.student_pages = [{"paper_id": "p1", "sha256": "image"}]
    session.save()
    seen = []

    async def recognize(*_args):
        seen.append(current_teaching_context())
        yield {"page": {"paper_id": "p1"}, "page_index": 0}

    async def persist(*_args):
        seen.append(current_teaching_context())

    monkeypatch.setattr(route, "_student_recognition_completion_stream", recognize)
    monkeypatch.setattr(route, "_persist_student_recognition_result", persist)
    query = SimpleNamespace(all=lambda: [])
    query.filter = lambda *_args: query
    db = SimpleNamespace(query=lambda *_args: query)

    async def run():
        function = route.recognize_students_stream if streaming else route.recognize_students
        result = await function(session.session_id, db=db, user=SimpleNamespace(id=1, education_stage="senior"))
        if streaming:
            assert "event: done" in "".join([part async for part in result.body_iterator])
        else:
            assert result["status"] == "review"

    asyncio.run(run())
    assert seen == [junior, junior]


def test_grading_sse_workers_and_saved_drafts_inherit_session_stage(monkeypatch, tmp_path):
    route = _route_setup(monkeypatch, tmp_path)
    junior = make_teaching_context("junior")
    session = AnswerSheetSession("1" * 16, str(tmp_path), teacher_id=1, class_id=1, teaching_context=junior)
    session.status = "review"
    session.student_pages = [{"paper_id": "p1"}]
    session.save()
    group = {"questions": [{"question_number": "1", "student_answer": "2"}], "student_name": "模拟学生", "pages": ["p1"], "question_pages": {"1": ["p1"]}}
    monkeypatch.setattr(route, "_grading_groups", lambda _session: {10: group})
    seen = []

    async def grade(*_args, **_kwargs):
        seen.append(current_teaching_context())
        return {"details": [{"question_no": "1", "result": "correct"}]}

    monkeypatch.setattr("services.grader.grade_paper_unified", grade)

    async def run():
        response = await route.build_grading_draft_stream(session.session_id, user=SimpleNamespace(id=1, education_stage="senior"))
        return "".join([part async for part in response.body_iterator])

    assert "event: done" in asyncio.run(run())
    assert seen == [junior]
    restored = AnswerSheetSession.load(session.session_id, str(tmp_path))
    assert restored.grouping_decisions[0]["result"]["teaching_context"] == junior
