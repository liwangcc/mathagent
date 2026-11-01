"""Stage-scoped historical reporting. All records are synthetic in-memory data."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models_user, models_rate_limit, models_accelerator  # noqa: F401
from database import Base, get_db
from models import Student, KnowledgeEvent, StudentProfile, GradingResult, ExamScore, WeaknessOverride, ParentLetterShare, QuestionBank
from models_user import User
from models_rate_limit import TeacherClass
from services import class_analytics as analytics
from services.teaching_context import make_teaching_context, use_teaching_context


@pytest.fixture
def scoped_db(monkeypatch, tmp_path):
    from routers import report, class_mgmt, stats, student
    from routers.auth import _require_user
    import services.generated_files as files
    monkeypatch.setattr(files, "OUTPUT_DIR", str(tmp_path / "generated"))
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    user = User(id=1, name="虚拟教师", education_stage="junior", role="teacher")
    db.add_all([user, TeacherClass(id=1, teacher_id=1, class_name="测试班"),
                Student(id=1, teacher_id=1, class_id=1, name="虚拟学生")])
    db.commit()
    now = datetime.now() - timedelta(hours=1)
    for index, stage in enumerate(("", "junior", "senior"), 1):
        version = make_teaching_context(stage)["curriculum_version"] if stage else ""
        fields = {"education_stage": stage, "catalog_version": version}
        result = GradingResult(id=index, student_id=1, category="daily", exam_name=f"{stage or 'legacy'}样本", total_questions=index,
                               correct_count=0, wrong_count=index, analytics_included=True, **fields)
        result.set_details([{"question": f"{stage or 'legacy'}测试题", "result": "wrong", "chapter": "同名章", "topic": "同名知识点"}])
        db.add(result)
        db.add(StudentProfile(student_id=1, grading_result_id=index, category="daily", exam_name=f"{stage or 'legacy'}记录",
                              total_questions=index, correct_count=0, wrong_count=index, analytics_included=True, created_at=now, **fields))
        db.add(KnowledgeEvent(student_id=1, teacher_id=1, class_id=1, grading_result_id=index, chapter="同名章", topic="同名知识点",
                              result="wrong", analytics_included=True, created_at=now + timedelta(seconds=index), **fields))
        db.add(ExamScore(student_id=1, teacher_id=1, class_id=1, exam_name=f"{stage or 'legacy'}考试", score=index * 10,
                         exam_date=now + timedelta(seconds=index), analytics_included=True, **fields))
    db.commit()
    app = FastAPI()
    for module in (report, class_mgmt, stats, student):
        app.include_router(module.router, prefix="/api")
    def session_dependency():
        session = factory()
        try:
            yield session
        finally:
            session.close()
    app.dependency_overrides[get_db] = session_dependency
    app.dependency_overrides[_require_user] = lambda: user
    yield db, TestClient(app), user, factory
    db.close()
    engine.dispose()


def test_same_topic_never_merges_or_cross_corrects(scoped_db):
    db, _, _, _ = scoped_db
    all_rows = analytics.weakness_list(db, 1)
    assert len(all_rows) == 3
    assert {row["education_stage"] for row in all_rows} == {"", "junior", "senior"}
    db.add(WeaknessOverride(student_id=1, teacher_id=1, topic_key="junior||同名章||同名知识点", status="corrected"))
    db.commit()
    rows = {row["education_stage"]: row for row in analytics.weakness_list(db, 1)}
    assert rows["junior"]["status"] == "corrected"
    assert rows["senior"]["status"] == rows[""]["status"] == "pending"
    assert analytics.correction_rate(db, 1, "junior") == 1
    assert analytics.correction_rate(db, 1, "senior") == 0
    assert len(analytics.class_heatmap(db, 1)) == 2
    assert len(analytics.knowledge_distribution(db, 1)) == 3
    assert analytics.student_detail(db, 1)["latest_exam_delta"] is None


@pytest.mark.parametrize("scope,count,stage", [(None, 3, None), ("", 3, None), ("legacy", 1, "legacy"), ("junior", 1, "junior"), ("senior", 1, "senior")])
def test_statistics_and_summary_scope_contract(scoped_db, scope, count, stage):
    _, client, _, _ = scoped_db
    params = {} if scope is None else {"education_stage": scope}
    response = client.get("/api/stats/1", params=params)
    assert response.status_code == 200
    assert response.json()["total_gradings"] == count
    assert response.json()["education_stage"] == stage
    summary = client.post("/api/reports/summary", json={"period": "all", **params})
    assert summary.status_code == 200
    assert summary.json()["total"] == count
    assert summary.json()["education_stage"] == stage
    detail = client.get("/api/student/1/detail", params=params).json()
    assert len(detail["weaknesses"]) == count
    assert len(detail["exam_scores"]) == count


def test_invalid_scope_rejected_and_legacy_never_reassigned(scoped_db):
    db, client, user, _ = scoped_db
    assert client.get("/api/stats/1", params={"education_stage": "university"}).status_code == 422
    assert client.post("/api/reports/summary", json={"education_stage": "high"}).status_code == 422
    user.education_stage = "senior"
    assert client.get("/api/stats/1", params={"education_stage": "legacy"}).json()["total_gradings"] == 1
    assert db.get(GradingResult, 1).education_stage == ""


def test_shared_letters_freeze_scope_and_do_not_revoke_other_stage(scoped_db):
    db, client, user, _ = scoped_db
    payload = {"type": "parent_letter", "lock": "token"}
    first = client.post("/api/class/1/report/batch", json=payload)
    assert first.status_code == 200, first.text
    junior = first.json()
    assert junior["education_stage"] == "junior"
    assert "初中" in junior["message"] and "冻结" in junior["message"]
    assert junior["shares"][0]["education_stage"] == "junior"
    first_id = junior["shares"][0]["id"]
    assert "senior考试" not in db.get(ParentLetterShare, first_id).html
    assert client.post("/api/class/1/report/batch", json=payload).json()["reused"] == 1
    user.education_stage = "senior"
    senior = client.post("/api/class/1/report/batch", json=payload).json()
    assert senior["created"] == 1 and senior["education_stage"] == "senior"
    assert db.get(ParentLetterShare, first_id).revoked_at is None
    legacy = client.post("/api/class/1/report/batch", json={**payload, "education_stage": "legacy"}).json()
    assert legacy["education_stage"] == ""
    assert len(client.get("/api/class/1/letter-shares").json()["shares"]) == 3
    assert len(client.get("/api/class/1/letter-shares", params={"education_stage": "junior"}).json()["shares"]) == 1
    refreshed = client.post("/api/class/1/report/batch", json={**payload, "refresh": True}).json()
    assert refreshed["education_stage"] == "senior"
    db.expire_all()
    assert db.get(ParentLetterShare, first_id).revoked_at is None


def test_question_bank_backfill_requires_same_stage_and_catalog(scoped_db):
    db, _, _, _ = scoped_db
    db.add(QuestionBank(id=99, question="synthetic", answer="1", chapter="高级章", topic="高级知识点", education_stage="senior", catalog_version=make_teaching_context("senior")["curriculum_version"]))
    db.commit()
    rows = [{"result": "wrong", "student_id": 1, "question_bank_id": 99, "education_stage": "junior", "catalog_version": make_teaching_context("junior")["curriculum_version"]}]
    assert analytics.knowledge_distribution_for_rows(db, rows)[0]["topic"] == "未分类"


def test_hermes_only_reads_active_stage(scoped_db, monkeypatch):
    from services import hermes_tools
    _, _, _, factory = scoped_db
    monkeypatch.setattr(hermes_tools, "_get_db", factory)
    monkeypatch.setattr(hermes_tools, "_get_user_id", lambda: 1)
    with use_teaching_context(make_teaching_context("junior")):
        text = hermes_tools._query_student({"student_name": "虚拟学生"})
        assert "初中数学" in text and "junior考试" in text
        assert "senior考试" not in text and "legacy考试" not in text
        summary = hermes_tools._query_summary({"type": "exam"})
        assert "初中数学" in summary


def test_batch_report_binds_frozen_stage():
    from services.batch_report import build_batch_report_payload, _build_prompt
    context = make_teaching_context("junior")
    batch = SimpleNamespace(teaching_context=context, review_results=[], results=[], batch_type="daily")
    payload = build_batch_report_payload(batch)
    assert payload["stats"]["education_stage"] == "junior"
    assert "初中" in _build_prompt(payload["stats"])
    batch.teaching_context = make_teaching_context("senior")
    assert build_batch_report_payload(batch)["input_hash"] != payload["input_hash"]


def test_export_has_only_requested_stage(scoped_db):
    from openpyxl import load_workbook
    from services.exporter import export_student_list_excel
    db, _, _, _ = scoped_db
    wb = load_workbook(export_student_list_excel(db, 1, education_stage="junior"))
    assert wb.active.max_row == 2
    assert wb.active.cell(2, 8).value == "初中数学"


def test_original_question_cycles_use_their_frozen_result_scope(scoped_db):
    import json
    from models_correction import QuestionAttemptEvent, WrongQuestionCycle
    db, _, _, _ = scoped_db
    for rid, stage, status in ((2, "junior", "corrected"), (3, "senior", "pending")):
        ctx = make_teaching_context(stage)
        attempt = QuestionAttemptEvent(student_id=1, teacher_id=1, class_id=1, grading_result_id=rid,
            question_no="1", question_identity=stage, question_fingerprint=stage, final_result="wrong",
            question_snapshot_json=json.dumps({"chapter": "同名章", "topic": "同名知识点", "question": "试题",
                "education_stage": stage, "catalog_version": ctx["curriculum_version"], "teaching_context": ctx}))
        db.add(attempt)
        db.flush()
        db.add(WrongQuestionCycle(student_id=1, teacher_id=1, class_id=1, question_identity=stage,
            question_fingerprint=stage, opened_attempt_id=attempt.id, status=status))
    db.commit()
    assert analytics.correction_rate(db, 1, "junior") == 1
    assert analytics.correction_rate(db, 1, "senior") == 0
    assert {row["education_stage"] for row in analytics.student_detail(db, 1, "junior")["wrong_questions"]} == {"junior"}
    assert {row["education_stage"] for row in analytics.student_detail(db, 1, "senior")["wrong_questions"]} == {"senior"}
