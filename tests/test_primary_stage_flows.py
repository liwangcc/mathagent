"""Primary stage reuses production entry points with isolated data/model doubles.

Run through scripts/run_math_stage_tests.py: no production storage, credentials,
student images or upstream model requests are used. These are integration
contracts, not an accuracy claim for primary-school visual question types.
"""
from __future__ import annotations

import asyncio
import copy
import json
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models_accelerator  # noqa: F401 - register tables used by real metadata
import models_rate_limit  # noqa: F401
from database import Base, get_db
from models import (
    GradingResult, KnowledgeEvent, QuestionBank, Student, StudentProfile, Weakness,
)
from models_correction import QuestionAttemptEvent
from models_rate_limit import TeacherClass
from models_user import User
from services import grading_persistence, question_classification
from services.math_curriculum import catalog_version, get_knowledge_point, get_math_catalog
from services.teaching_context import (
    current_teaching_context, make_teaching_context, stage_prompt,
    use_teaching_context, validate_teaching_context,
)


STAGES = ("primary", "junior", "senior")


def context():
    return make_teaching_context("primary")


def point(stage="primary"):
    module = get_math_catalog(stage)[0]
    return module["chapter"], module["topics"][0]


def question():
    return {
        "question_no": "1", "question_text": "计算 1+1。",
        "question_type": "fill", "standard_answer": "2",
    }


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    from routers import auth, class_mgmt, exam_export, report, stats, student, teacher
    import services.generated_files as generated_files

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(question_classification, "SessionLocal", factory)
    monkeypatch.setattr(grading_persistence, "ARCHIVE_DIR", str(tmp_path / "archives"))
    monkeypatch.setattr(generated_files, "OUTPUT_DIR", str(tmp_path / "generated"))
    db = factory()
    user = User(id=1, name="小学隔离测试教师", education_stage="primary", role="teacher")
    pupil = Student(id=1, teacher_id=1, class_id=1, name="合成学生", class_name="合成班")
    db.add_all([
        user, TeacherClass(id=1, teacher_id=1, class_name="合成班"), pupil,
    ])
    db.commit()
    app = FastAPI()
    for module in (auth, teacher, class_mgmt, stats, student, report, exam_export):
        app.include_router(module.router, prefix="/api")

    def sessions():
        # Match the real auth dependency: authenticated User and route DB share
        # one request session, so update/refresh is a genuine ORM round trip.
        yield db

    app.dependency_overrides[get_db] = sessions
    app.dependency_overrides[auth._require_user] = lambda: user
    try:
        with TestClient(app) as client:
            yield db, pupil, user, client
    finally:
        db.close()
        engine.dispose()


def persist(db, pupil, stage):
    chapter, topic = point(stage) if stage else ("历史合成章", "历史合成知识点")
    return grading_persistence.persist_grading_result(
        db, student=pupil, teacher_id=1, category="daily", exam_name="",
        image_path="", image_hash="same-synthetic-image", total_questions=1,
        correct_count=0, partial_count=0, wrong_count=1, needs_review_count=0,
        score=None, teaching_context=make_teaching_context(stage) if stage else {},
        details=[{
            "question_no": "1", "question": "计算 1+1。", "question_type": "fill",
            "standard_answer": "2", "student_answer": "3", "result": "wrong",
            "chapter": chapter, "topic": topic,
        }],
    )[0]


def test_old_released_versions_and_legacy_context_are_unchanged():
    assert catalog_version("junior") == "math:junior:2026-09-09.v1:94f08fd4a0bc"
    assert catalog_version("senior") == "math:senior:2026-09-09.v1:0511b9cf6dd6"
    assert context()["rules_version"] == "math-stage-v1"
    for stage in STAGES:
        assert validate_teaching_context(make_teaching_context(stage)) == make_teaching_context(stage)
    with use_teaching_context(context()):
        assert validate_teaching_context({}) == {}
        assert stage_prompt("[task=student_transcription_only]\n高中数学").startswith(
            "[task=student_transcription_only]\n小学数学"
        )


@pytest.mark.parametrize("mode", ["blank", "marked"])
@pytest.mark.parametrize("anonymous", [False, True])
def test_template_and_student_batch_restore_frozen_primary_context(tmp_path, mode, anonymous):
    from services.quick_grader import QuickBatch
    from services.template_session import TemplateSession

    template = TemplateSession("primary-template", str(tmp_path / "template"), 1, teaching_context=context())
    template.template_mode = mode
    template.add_page(0, [question()])
    template.confirm_current_contract(1)
    original_hash = template.confirmed_contract_hash
    template.save_meta()
    batch = QuickBatch("primary-batch", str(tmp_path / "batch"), teaching_context=template.teaching_context)
    batch.identity_mode = "anonymous" if anonymous else "identified"
    batch.save_meta()
    with use_teaching_context(make_teaching_context("senior")):
        restored = TemplateSession.load_meta(template.template_id, template.upload_dir)
        recovered = QuickBatch.load_meta(batch.batch_id, batch.upload_dir)
    assert restored.teaching_context == recovered.teaching_context == context()
    assert restored.template_mode == mode
    assert restored.confirmed_contract_hash == restored.content_hash() == original_hash
    assert recovered.identity_mode == batch.identity_mode


def test_grading_cache_separates_all_three_stages_and_legacy():
    from routers.grade import _build_grading_contract, _grading_contract_hash
    from services.grading_contract import AnswerSpec

    snapshots = [{}, *(make_teaching_context(stage) for stage in STAGES)]
    hashes = set()
    for snapshot in snapshots:
        contract = _build_grading_contract(
            mode="precise", category="daily", exam_name="", identity_plan={},
            confirmed=[AnswerSpec(**question())], teaching_context=snapshot,
        )
        hashes.add(_grading_contract_hash(contract))
    assert len(hashes) == 4


@pytest.mark.parametrize("anonymous", [False, True])
def test_student_ocr_reuses_primary_batch_even_after_profile_change(monkeypatch, tmp_path, anonymous):
    from routers import grade
    from services.quick_grader import QuickBatch

    batch = QuickBatch("primary-ocr", str(tmp_path), teaching_context=context())
    batch.identity_mode = "anonymous" if anonymous else "identified"
    seen = []

    async def identity(*_args, **_kwargs):
        return {"name": "合成学生", "confidence": 0.9}

    async def recognize(*_args, **kwargs):
        seen.append(current_teaching_context())
        return {"questions": []}

    monkeypatch.setattr(grade, "recognize_student_info_with_roster", identity)
    monkeypatch.setattr(grade, "recognize_paper_with_cache", recognize)
    with use_teaching_context(make_teaching_context("junior")):
        result = asyncio.run(grade._recognize_batch_upload(
            batch, "never-read-synthetic.png", object(), teacher_id="1", paper_id="p1",
        ))
    assert seen == [context()]
    assert ("student_name" in result) is not anonymous


def test_primary_student_transcription_keeps_existing_route(monkeypatch):
    from services import ocr_service

    calls = []

    async def transcribe(_path, prompt, **kwargs):
        calls.append((prompt, kwargs))
        return '{"questions":[{"question_number":"1","student_answer":"3","answer_state":"visible","question_type":"fill"}]}'

    monkeypatch.setattr(ocr_service, "call_vision", transcribe)
    with use_teaching_context(context()):
        student = asyncio.run(ocr_service.recognize_paper("synthetic.png"))
    assert calls[0][0].startswith("[task=student_transcription_only]")
    assert "小学数学" in calls[0][0]
    assert student["questions"][0]["student_answer"] == "3"


def test_primary_classification_is_one_text_batch_with_exact_local_catalog(monkeypatch):
    from services import analyzer

    sent = []
    chapter, topic = point()
    input_rows = [{"id": "1", "question": "计算1+1"}, {"id": "2", "question": "数一数有几个苹果"}]

    async def classify(messages, **kwargs):
        sent.append((messages, kwargs))
        return json.dumps({"results": [{
            "id": row["id"], "knowledge_points": [{"chapter": chapter, "topic": topic}],
            "confidence": "high",
        } for row in input_rows]}, ensure_ascii=False)

    monkeypatch.setattr(analyzer, "_call_knowledge_classifier_text", classify)
    with use_teaching_context(context()):
        result = asyncio.run(analyzer.classify_questions_knowledge(copy.deepcopy(input_rows)))
    assert len(sent) == 1 and sent[0][1]["question_count"] == 2
    messages = sent[0][0]
    assert all(isinstance(item["content"], str) for item in messages)
    assert "小学数学题库知识点分类器" in messages[0]["content"]
    catalog_text = messages[1]["content"].split("允许目录：", 1)[1].split("\n题目：", 1)[0]
    assert json.loads(catalog_text) == get_math_catalog("primary")
    assert "不解题" in messages[1]["content"] and "不接收图片" in messages[1]["content"]
    assert {row["education_stage"] for row in result} == {"primary"}
    assert all(row["knowledge_point_id"].startswith("primary.math.") for row in result)
    assert [row["id"] for row in result] == ["1", "2"]


def test_primary_queued_grading_rebinds_frozen_context_without_changing_answers(monkeypatch):
    from services import grader

    seen, keys = [], []

    class Queue:
        async def submit(self, *, coro_factory, idempotency_key, teacher_id):
            keys.append(idempotency_key)
            with use_teaching_context({}):
                return await coro_factory()

    async def judge(questions, detail_level, *, teacher_material=None):
        seen.append(current_teaching_context())
        assert questions[0]["student_answer"] == "3"
        return {"1": {"result": "wrong", "standard_answer": "2"}}

    monkeypatch.setattr(grader, "get_grading_queue", lambda: Queue())
    monkeypatch.setattr(grader, "judge_paper", judge)
    monkeypatch.setattr(grader, "image_content_hash", lambda _path: "synthetic")
    for stage in ("primary", "junior", "primary"):
        with use_teaching_context(make_teaching_context(stage)):
            result = asyncio.run(grader.grade_paper_unified(
                paper={"questions": [{"question_number": "1", "student_answer": "3", "question_type": "solve"}]},
                answer_map={"1": {**question(), "question_type": "solve", "matched": True}},
                student_name="合成学生", teacher_id=1,
            ))
        assert result["wrong_count"] == 1
    assert [item["education_stage"] for item in seen] == ["primary", "junior", "primary"]
    assert keys[0] == keys[2] and keys[0] != keys[1]


def test_primary_background_classifier_groups_text_and_rejects_stale_catalog(monkeypatch):
    from services import analyzer

    calls = []

    async def classify(rows, *, per_question=False):
        assert per_question is True
        calls.append((current_teaching_context(), rows))
        return [{"id": row["id"], "status": "suggested"} for row in rows]

    monkeypatch.setattr(analyzer, "classify_questions_knowledge", classify)
    claimed = [{
        "question_id": index, "question": f"合成文字题干{index}",
        "education_stage": stage, "catalog_version": catalog_version(stage),
    } for index, stage in enumerate(("primary", "primary", "junior"))]
    claimed.append({
        "question_id": 9, "question": "过期目录不得发送", "education_stage": "primary",
        "catalog_version": "unknown",
    })
    with use_teaching_context(make_teaching_context("senior")):
        results = asyncio.run(question_classification._classify_stage_groups(claimed, "question_id"))
        assert current_teaching_context()["education_stage"] == "senior"
    assert [(snapshot["education_stage"], len(rows)) for snapshot, rows in calls] == [("primary", 2), ("junior", 1)]
    assert next(row for row in results if row["id"] == "9")["status"] == "manual"


def test_primary_bank_background_classification(isolated):
    db, _, _, _ = isolated
    bank_question = QuestionBank(
        teacher_id=1, question="合成待分类题", answer="2", education_stage="primary",
        catalog_version=catalog_version("primary"),
    )
    db.add(bank_question)
    db.commit()
    question_classification.reconcile_classification_jobs_once()
    claimed = question_classification.claim_classification_batch("synthetic-worker", 10)
    assert len(claimed) == 1 and claimed[0]["education_stage"] == "primary"
    chapter, topic = point()
    question_classification.finish_classification_batch(claimed, [{
        "id": str(bank_question.id), "status": "suggested", "chapter": chapter, "topic": topic,
    }])
    db.expire_all()
    assert bank_question.knowledge_point_id == get_knowledge_point("primary", chapter, topic)["id"]


def test_primary_profile_bank_and_catalog_share_existing_http_contract(isolated):
    db, _, user, client = isolated
    for stage in ("junior", "senior", ""):
        db.add(QuestionBank(teacher_id=1, question="同名题", answer="2", education_stage=stage))
    db.add(QuestionBank(teacher_id=2, question="他人小学题", answer="2", education_stage="primary"))
    db.commit()
    profile = client.put("/api/auth/me", json={"education_stage": "primary"})
    assert profile.status_code == 200, profile.text
    assert profile.json()["user"]["profile_complete"] is True
    assert client.get("/api/auth/me").json()["user"]["education_stage"] == "primary"
    assert client.get("/api/teacher/chapters").json() == get_math_catalog("primary")
    chapter, topic = point()
    added = client.post("/api/teacher/bank", json={"question": "同名题", "answer": "2", "chapter": chapter, "topic": topic})
    assert added.status_code == 200, added.text
    row = db.get(QuestionBank, added.json()["id"])
    assert row.education_stage == "primary"
    assert row.knowledge_point_id == get_knowledge_point("primary", chapter, topic)["id"]
    items = client.get("/api/teacher/bank").json()["items"]
    assert [item["id"] for item in items] == [row.id]
    assert client.get("/api/bank/export").json()["count"] == 1
    for stale in ("junior", "senior", "legacy"):
        assert client.get("/api/teacher/bank", params={"education_stage": stale}).status_code == 409
    user.education_stage = "junior"
    assert row.id not in {item["id"] for item in client.get("/api/teacher/bank").json()["items"]}
    assert db.get(QuestionBank, row.id).education_stage == "primary"


def test_primary_archive_classification_statistics_and_correction_remain_isolated(isolated):
    db, pupil, user, client = isolated
    results = {stage: persist(db, pupil, stage) for stage in ("", *STAGES)}
    assert len({row.id for row in results.values()}) == 4
    for model in (GradingResult, StudentProfile, KnowledgeEvent, Weakness):
        assert {row.education_stage for row in db.query(model).all()} == {"", *STAGES}
    repeated = persist(db, pupil, "primary")
    assert repeated.id == results["primary"].id
    attempt = db.query(QuestionAttemptEvent).filter_by(grading_result_id=repeated.id).one()
    assert attempt.question_snapshot()["teaching_context"] == context()
    assert attempt.question_snapshot()["knowledge_point_id"].startswith("primary.math.")
    user.education_stage = "senior"
    params = {"education_stage": "primary"}
    stats = client.get("/api/stats/1", params=params)
    assert stats.status_code == 200, stats.text
    assert stats.json()["total_gradings"] == 1
    summary = client.post("/api/reports/summary", json={"period": "all", **params})
    assert summary.status_code == 200, summary.text
    assert summary.json()["total"] == 1
    detail = client.get("/api/student/1/detail", params=params)
    assert detail.status_code == 200, detail.text
    assert {item["education_stage"] for item in detail.json()["weaknesses"]} == {"primary"}
    chapter, topic = point()
    correction = client.post("/api/student/1/weakness/correct", params={"topic_key": f"primary||{chapter}||{topic}"}, json={"status": "corrected"})
    assert correction.status_code == 200, correction.text
    assert results[""].education_stage == "" and results["junior"].education_stage == "junior"


def test_primary_photo_import_restore_and_save_preserve_original_context(isolated, monkeypatch, tmp_path):
    from routers import bank_import

    db, _, user, _ = isolated
    monkeypatch.setattr(bank_import, "UPLOAD_DIR", str(tmp_path / "imports"))
    seen = []

    async def upload(*_args):
        return "never-read-synthetic-image"

    async def recognize(*_args, **_kwargs):
        seen.append(current_teaching_context())
        return {"questions": [{"question_number": "1", "question_text": "计算1+1"}]}

    monkeypatch.setattr(bank_import, "_save_upload", upload)
    monkeypatch.setattr(bank_import, "recognize_paper", recognize)
    response = asyncio.run(bank_import.photo_import_recognize([object()], [], chapter="", user=user))
    user.education_stage = "senior"
    restored = bank_import.photo_import_restore(response["import_id"], user=user)
    assert restored["teaching_context"] == context()
    request = bank_import.SaveReq(items=[bank_import._SaveItem(question_no="1", content="计算1+1", answer="2")], import_id=response["import_id"])
    saved = bank_import.photo_import_save(request, db, user)
    assert saved["teaching_context"] == context() and seen == [context()]
    assert db.query(QuestionBank).one().education_stage == "primary"


def test_primary_answer_sheet_source_and_grading_use_original_session(monkeypatch, tmp_path):
    from routers import answer_sheet
    from services.answer_sheet_session import AnswerSheetSession

    monkeypatch.setattr(answer_sheet, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(answer_sheet, "_answer_sheet_writes_enabled", lambda: None)

    async def ready(_user):
        return None

    monkeypatch.setattr(answer_sheet, "_require_answer_sheet_chain", ready)
    monkeypatch.setattr(answer_sheet, "_owned_class", lambda *_args: SimpleNamespace(id=1))
    # Answer-sheet redesign slice 1: the class is inherited from the global
    # current class and a batch binds a frozen paper template of its stage.
    from tests.answer_sheet_redesign_helpers import confirmed_detailed_template, install_registries

    paper_registry, _sheet_registry = install_registries(monkeypatch, tmp_path)
    paper_source = confirmed_detailed_template(
        str(tmp_path), "primary-paper", category="daily", questions=[question()], stage="primary",
    )
    paper_code = paper_registry.register(paper_source, teacher_id=1)["template_code"]
    user = SimpleNamespace(id=1, education_stage="primary", current_class_id=1)
    created = asyncio.run(answer_sheet.create_session(answer_sheet.SessionCreateRequest(paper_template_code=paper_code), db=object(), user=user))
    session = AnswerSheetSession.load(created["session_id"], str(tmp_path))
    session.status = "source_uploaded"
    session.answer_source = "embedded"
    session.source_exam_pages = [{"path": "synthetic.png", "sha256": "synthetic"}]
    session.save()
    user.education_stage = "senior"
    seen = []

    async def recognize(_path):
        seen.append(current_teaching_context())
        return {"questions": [{**question(), "question_number": "1"}]}

    async def grade(ocr, answers, name, teacher_id, **kwargs):
        seen.append(current_teaching_context())
        return {"details": [{"question_no": "1", "result": "correct"}]}

    monkeypatch.setattr(answer_sheet, "recognize_template_with_answers", recognize)
    assert asyncio.run(answer_sheet.recognize_source(session.session_id, user=user))["status"] == "source_recognized"
    group = {"questions": [{"question_number": "1", "student_answer": "2"}], "student_name": "合成学生", "pages": ["p1"], "question_pages": {"1": ["p1"]}}
    with use_teaching_context(make_teaching_context("senior")):
        result = asyncio.run(answer_sheet._grade_answer_sheet_group(grade, 1, group, {}, 1, "daily", teaching_context=session.teaching_context))
    assert seen == [context(), context()]
    assert result["result"]["details"][0]["teaching_context"] == context()


def test_primary_exercise_generation_and_document_metadata_follow_snapshot(monkeypatch):
    from routers import exercise
    from services.exercise_schema import ExercisePaper
    from services.exercise_document import build_exercise_documents
    from services.answer_sheet_document import build_answer_sheet_document
    from services.practice import load_knowledge_points

    row = {"type": "fill", "question": "1+1=____。", "answer": "2", "solution": "相加。"}
    seen = []

    async def generate(*_args):
        seen.append(current_teaching_context())
        return {"title": "合成练习", "exercises": [row]}

    monkeypatch.setattr(exercise, "_generate_exercises", generate)
    response = asyncio.run(exercise.generate_exercises(exercise.ExerciseGenerateRequest(question_count=1), db=object(), user=SimpleNamespace(id=1, education_stage="primary")))
    assert seen == [context()] and response["teaching_context"] == context()
    assert ExercisePaper(questions=[row], teaching_context=context()).title == "小学数学练习"
    tree = load_knowledge_points("primary")["数学"]
    assert set(tree) == {module["chapter"] for module in get_math_catalog("primary")}
    with use_teaching_context(make_teaching_context("senior")):
        _, answers = build_exercise_documents("合成练习", [row], teaching_context=context())
        card = build_answer_sheet_document("合成练习", [row], teaching_context=context())
    for data, expected in ((answers, "科目：小学数学"), (card, "小学数学答题卡")):
        with ZipFile(BytesIO(data)) as document:
            xml = document.read("word/document.xml").decode()
        assert expected in xml and "科目：高中数学" not in xml
        if data is card:
            assert "初中数学答题卡" not in xml and "普通高中数学答题卡" not in xml


def test_primary_exam_export_does_not_merge_same_name_other_stages(isolated):
    from services import exam_export, error_sheet

    db, pupil, _, _ = isolated
    for stage in STAGES:
        record = GradingResult(student_id=pupil.id, education_stage=stage, category="exam", exam_name="同名考试", score=0, max_score=10, total_questions=1, wrong_count=1, analytics_included=True)
        record.set_details([{"question_no": "1", "question": f"{stage}专属题干", "result": "wrong", "student_answer": "3", "correct_answer": "2"}])
        db.add(record)
    db.commit()
    assert {row["education_stage"] for row in exam_export.list_exams(db, 1)} == set(STAGES)
    assert exam_export.export_exam_scores(db, 1, "同名考试")["code"] == "ambiguous_export_stage"
    html = error_sheet.export_error_sheet_html(db, 1, "同名考试", education_stage="primary")
    assert "primary专属题干" in html
    assert "junior专属题干" not in html and "senior专属题干" not in html


def test_primary_assistant_scope_is_not_unlabelled_history(monkeypatch):
    from services import chat_intent, hermes_tools

    sent = []
    original_message = "原题引用里写着高中数学，不要替换它"

    async def reply(messages, **_kwargs):
        sent.append(messages)
        return '{"intent":"help","params":{},"clarify":""}'

    monkeypatch.setattr(chat_intent, "_call_llm", reply)
    with use_teaching_context(context()):
        assert hermes_tools._teaching_read_scope() == ("primary", catalog_version("primary"), "小学数学")
        asyncio.run(chat_intent.recognize_intent(original_message, teacher_id="1"))
    assert "本次任务学段为小学" in sent[0][0]["content"]
    assert sent[0][1]["content"] == original_message


def _seed_hermes_stage_records(db, pupil, selected_stage):
    """Only synthetic records; same-owner stages, old version, and another owner."""
    foreign = Student(id=2, teacher_id=2, name="其他教师合成学生", class_name="合成班")
    db.add_all([User(id=2, name="其他合成教师", role="teacher"), foreign])
    for stage, version, student_id, text in [
        *((stage, catalog_version(stage), pupil.id, f"only-{stage}-question") for stage in STAGES),
        (selected_stage, "stale-catalog", pupil.id, f"stale-{selected_stage}-question"),
        (selected_stage, catalog_version(selected_stage), foreign.id, "foreign-owner-question"),
    ]:
        record = GradingResult(
            student_id=student_id, education_stage=stage, catalog_version=version,
            exam_name="同名合成考试", category="exam", total_questions=1, wrong_count=1,
        )
        record.set_details([{
            "question_no": "1", "question": text, "model_answer": "2",
            "student_answer": "3", "correct_answer": "2", "result": "wrong",
        }])
        db.add(record)
    db.commit()


@pytest.mark.parametrize("stage", STAGES)
def test_hermes_pending_uses_exact_frozen_stage_catalog_and_owner(isolated, monkeypatch, stage):
    from services import hermes_tools

    db, pupil, user, _ = isolated
    _seed_hermes_stage_records(db, pupil, stage)
    sessions = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr(hermes_tools, "_get_db", sessions)
    monkeypatch.setattr(hermes_tools, "_get_user_id", lambda: user.id)
    # The current profile disagrees deliberately; only the frozen context wins.
    user.education_stage = next(value for value in STAGES if value != stage)
    with use_teaching_context(make_teaching_context(stage)):
        result = hermes_tools._query_pending({})
    assert result == "当前有 1 条待入库题目，请前往「待入库」页面（/pending-answers）确认。"


@pytest.mark.parametrize("stage", STAGES)
def test_hermes_export_uses_exact_frozen_stage_catalog_and_owner(isolated, monkeypatch, stage):
    from services import generated_files, hermes_tools

    db, pupil, user, _ = isolated
    _seed_hermes_stage_records(db, pupil, stage)
    sessions = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr(hermes_tools, "_get_db", sessions)
    monkeypatch.setattr(hermes_tools, "_get_user_id", lambda: user.id)
    generated = []

    def register(_db, owner, payload, name):
        generated.append((owner, payload, name))
        return SimpleNamespace(id=91)

    monkeypatch.setattr(generated_files, "register_generated_file", register)
    user.education_stage = next(value for value in STAGES if value != stage)
    with use_teaching_context(make_teaching_context(stage)):
        result = hermes_tools._export_error_sheet({"exam_name": "同名合成考试"})
    assert "（1条记录）" in result
    assert len(generated) == 1 and generated[0][0] == user.id
    with ZipFile(BytesIO(generated[0][1])) as document:
        xml = document.read("word/document.xml").decode()
    assert f"only-{stage}-question" in xml
    assert all(f"only-{other}-question" not in xml for other in STAGES if other != stage)
    assert "stale-" not in xml and "foreign-owner-question" not in xml


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("tool_name,row_limit", [("pending", 300), ("export", 50)])
def test_hermes_stage_filter_precedes_record_limit(isolated, monkeypatch, stage, tool_name, row_limit):
    from services import generated_files, hermes_tools

    db, pupil, user, _ = isolated
    other = next(value for value in STAGES if value != stage)
    for index in range(row_limit + 1):
        record = GradingResult(
            student_id=pupil.id, education_stage=other if index % 2 else stage,
            catalog_version=catalog_version(other) if index % 2 else "stale-catalog",
            total_questions=1, wrong_count=1,
        )
        record.set_details([{
            "question_no": "1", "question": f"limit-distractor-{index}",
            "model_answer": "", "student_answer": "3", "correct_answer": "2", "result": "wrong",
        }])
        db.add(record)
    target = GradingResult(
        student_id=pupil.id, education_stage=stage, catalog_version=catalog_version(stage),
        total_questions=1, wrong_count=1,
    )
    target.set_details([{
        "question_no": "1", "question": "late-in-scope-question", "model_answer": "2",
        "student_answer": "3", "correct_answer": "2", "result": "wrong",
    }])
    db.add(target)
    db.commit()
    engine = db.get_bind()
    monkeypatch.setattr(hermes_tools, "_get_db", sessionmaker(bind=engine))
    monkeypatch.setattr(hermes_tools, "_get_user_id", lambda: user.id)
    generated, statements = [], []

    def register(_db, owner, payload, name):
        generated.append((owner, payload, name))
        return SimpleNamespace(id=92)

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(" ".join(statement.lower().split()))

    monkeypatch.setattr(generated_files, "register_generated_file", register)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        with use_teaching_context(make_teaching_context(stage)):
            result = (hermes_tools._query_pending({}) if tool_name == "pending"
                      else hermes_tools._export_error_sheet({}))
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    if tool_name == "pending":
        assert result == "当前有 1 条待入库题目，请前往「待入库」页面（/pending-answers）确认。"
    else:
        assert "（1条记录）" in result and len(generated) == 1
        with ZipFile(BytesIO(generated[0][1])) as document:
            xml = document.read("word/document.xml").decode()
        assert "late-in-scope-question" in xml and "limit-distractor-" not in xml
    # Also guard the SQL contract independently of SQLite's incidental row order.
    query = next(sql for sql in statements if "from grading_results join students" in sql)
    where_before_limit = query.split(" where ", 1)[1].split(" limit ", 1)[0]
    assert "grading_results.education_stage =" in where_before_limit
    assert "grading_results.catalog_version =" in where_before_limit


@pytest.mark.parametrize("stage", STAGES)
def test_hermes_pending_bank_match_does_not_borrow_other_stage_version_or_owner(isolated, monkeypatch, stage):
    from services import hermes_tools

    db, pupil, user, _ = isolated
    other = next(value for value in STAGES if value != stage)
    texts = ("present-here", "other-stage-only", "stale-version-only", "foreign-owner-only")
    record = GradingResult(
        student_id=pupil.id, education_stage=stage, catalog_version=catalog_version(stage),
    )
    record.set_details([{"question": text, "model_answer": "2"} for text in texts])
    db.add(record)
    for text, bank_stage, version, owner in (
        (texts[0], stage, catalog_version(stage), user.id),
        (texts[1], other, catalog_version(other), user.id),
        (texts[2], stage, "stale-catalog", user.id),
        (texts[3], stage, catalog_version(stage), 2),
    ):
        db.add(QuestionBank(
            question=text, answer="2", teacher_id=owner, is_public=False,
            education_stage=bank_stage, catalog_version=version,
        ))
    db.commit()
    monkeypatch.setattr(hermes_tools, "_get_db", sessionmaker(bind=db.get_bind()))
    monkeypatch.setattr(hermes_tools, "_get_user_id", lambda: user.id)
    with use_teaching_context(make_teaching_context(stage)):
        result = hermes_tools._query_pending({})
    assert result == "当前有 3 条待入库题目，请前往「待入库」页面（/pending-answers）确认。"
