"""Confirmed legacy-stage adoption against real ORM schema and read-only APIs.

Only synthetic temporary SQLite records are used. Run through
scripts/run_math_stage_tests.py to disable dotenv loading and external network.
"""
from datetime import datetime, timedelta
import hashlib
import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models_accelerator  # noqa: F401 -- Register referenced metadata.
from database import Base, get_db
from models import (
    ExamScore, GradingResult, KnowledgeEvent, ParentLetterShare, QuestionBank,
    Student, StudentProfile, Weakness, WeaknessOverride,
)
from models_correction import QuestionAttemptEvent, WrongQuestionCycle
from models_rate_limit import TeacherClass
from models_user import User
from scripts import adopt_existing_data_senior as migration
from services import class_analytics as analytics
from services.correction_tracking import attempt_teaching_context, wrong_question_summary
from services.math_curriculum import catalog_version, get_math_catalog
from services.teaching_context import make_teaching_context


def _rows(path, table):
    assert table in Base.metadata.tables
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY id')]


def _apply(path, tmp_path):
    plan = migration.inspect_database(path)
    args = dict(
        through_ids=plan["through_ids"], expected_counts=plan["eligible_counts"],
        expected_plan_sha256=plan["plan_sha256"], backup_path=tmp_path / "before-adoption.db",
        confirm_senior=True, confirm_offline=True,
    )
    return migration.apply_database(path, **args), args


@pytest.fixture
def historical_database(tmp_path):
    """Two legacy batches, labeled stages, and one excluded observation batch."""
    from routers import class_mgmt, report, stats, student, teacher
    from routers.auth import _require_user

    path = tmp_path / "synthetic-historical.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    chapter = get_math_catalog("senior")[0]["chapter"]
    topic = get_math_catalog("senior")[0]["topics"][0]
    now = datetime.now() - timedelta(days=2)
    with factory() as db:
        user = User(id=1, name="合成教师", education_stage="senior", role="teacher")
        db.add_all([
            user, TeacherClass(id=1, teacher_id=1, class_name="合成班"),
            Student(id=1, teacher_id=1, class_id=1, name="合成学生"),
        ])
        db.flush()
        for row_id, stage, correct_count in ((1, "", 3), (2, "", 2), (3, "junior", 1), (4, "senior", 4), (5, "", 0)):
            included = row_id != 5
            version = catalog_version(stage) if stage else ""
            fields = {"education_stage": stage, "catalog_version": version}
            details = [{
                "question_no": str(number), "question": f"合成题 {row_id}-{number}",
                "student_answer": "合成答案", "correct_answer": "参考答案",
                "result": "correct" if number <= correct_count else "wrong",
                "chapter": chapter, "topic": topic,
                "teaching_context": make_teaching_context(stage) if stage else {},
            } for number in range(1, 5)]
            raw = json.dumps(details, ensure_ascii=False)
            result = GradingResult(
                id=row_id, student_id=1, category="daily", exam_name=f"合成批次{row_id}",
                image_content_hash=hashlib.sha256(f"image-{row_id}".encode()).hexdigest(),
                grading_contract_hash=hashlib.sha256(raw.encode()).hexdigest(),
                grading_details=raw, ocr_raw_json='{"synthetic": true}',
                total_questions=4, correct_count=correct_count, wrong_count=4-correct_count,
                correct_rate=correct_count / 4, analytics_included=included,
                inclusion_reason="included" if included else "observation", **fields,
            )
            db.add(result)
            db.add(StudentProfile(
                id=row_id, student_id=1, grading_result_id=row_id, category="daily",
                exam_name=f"合成档案{row_id}", total_questions=4,
                correct_count=correct_count, wrong_count=4-correct_count,
                correct_rate=correct_count / 4, details_json=raw,
                image_content_hash=result.image_content_hash, analytics_included=included,
                inclusion_reason=result.inclusion_reason, created_at=now + timedelta(minutes=row_id), **fields,
            ))
            db.add(ExamScore(
                id=row_id, student_id=1, class_id=1, teacher_id=1,
                exam_name=f"合成考试{row_id}", score=row_id * 10 + 0.5,
                analytics_included=included, inclusion_reason=result.inclusion_reason,
                exam_date=(now + timedelta(days=row_id / 10)).date(), **fields,
            ))
            if not included:
                continue
            db.add(QuestionBank(
                id=row_id, teacher_id=1, is_public=False, question=f"合成题库{row_id}",
                answer="合成参考答案", chapter=chapter, topic=topic,
                source_grading_id=row_id, classification_status="confirmed", **fields,
            ))
            db.add(Weakness(
                id=row_id, student_id=1, chapter=chapter, topic=topic,
                occurrences=row_id, status="corrected" if row_id == 1 else "pending", **fields,
            ))
            db.add(ParentLetterShare(
                id=row_id, token=f"synthetic-share-{row_id}", student_id=1,
                teacher_id=1, class_id=1, phone_last4="0000", html=f"<p>合成正文{row_id}</p>",
                view_count=row_id, fail_count=row_id, **fields,
            ))
            for number, detail in enumerate(details, 1):
                db.add(KnowledgeEvent(
                    id=(row_id-1)*4+number, student_id=1, teacher_id=1, class_id=1,
                    grading_result_id=row_id, question_bank_id=row_id, chapter=chapter, topic=topic,
                    result=detail["result"], analytics_included=True,
                    created_at=now + timedelta(minutes=row_id, seconds=number), **fields,
                ))
            if row_id <= 3:
                # Both an entirely empty snapshot and a legacy empty context occur
                # in historical records. Neither may be rewritten by adoption.
                snapshot = {} if row_id == 1 else {
                    "chapter": chapter, "topic": topic,
                    "teaching_context": make_teaching_context(stage) if stage else {},
                }
                db.add(QuestionAttemptEvent(
                    id=row_id, student_id=1, teacher_id=1, class_id=1, grading_result_id=row_id,
                    question_bank_id=row_id, question_no="4", question_identity=f"synthetic-identity-{row_id}",
                    question_fingerprint=hashlib.sha256(f"question-{row_id}".encode()).hexdigest(),
                    question_snapshot_json=json.dumps(snapshot, ensure_ascii=False),
                    student_answer_snapshot_json='{"answer":"合成答案"}', final_result="wrong",
                    occurred_at=now + timedelta(minutes=row_id),
                ))
                db.flush()
                db.add(WrongQuestionCycle(
                    id=row_id, student_id=1, teacher_id=1, class_id=1,
                    question_identity=f"synthetic-identity-{row_id}",
                    question_fingerprint=hashlib.sha256(f"question-{row_id}".encode()).hexdigest(),
                    opened_attempt_id=row_id, status="corrected" if row_id == 2 else "pending",
                    close_method="teacher_manual" if row_id == 2 else "", wrong_count=row_id,
                    opened_at=now + timedelta(minutes=row_id),
                ))
        db.add_all([
            WeaknessOverride(id=1, student_id=1, teacher_id=1, topic_key=f"{chapter}||{topic}", status="corrected"),
            WeaknessOverride(id=2, student_id=1, teacher_id=1, topic_key=f"junior||{chapter}||{topic}", status="pending"),
        ])
        db.commit()
        db.expunge(user)

    app = FastAPI()
    for module in (class_mgmt, report, stats, student, teacher):
        app.include_router(module.router, prefix="/api")

    def synthetic_session():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = synthetic_session
    app.dependency_overrides[_require_user] = lambda: user
    with TestClient(app) as client:
        yield path, factory, client, user, chapter, topic
    engine.dispose()


def test_real_apis_show_adopted_history_in_senior_without_junior_or_observation_leak(historical_database, tmp_path):
    path, factory, client, user, _, _ = historical_database
    assert client.get("/api/teacher/bank").json()["total"] == 1
    assert client.get("/api/stats/1", params={"education_stage": "senior"}).json()["total_gradings"] == 1
    _apply(path, tmp_path)

    bank = client.get("/api/teacher/bank")
    assert bank.status_code == 200, bank.text
    assert {item["id"] for item in bank.json()["items"]} == {1, 2, 4}
    assert {item["education_stage"] for item in bank.json()["items"]} == {"senior"}
    for stage, gradings, questions, correct in (("senior", 3, 12, 9), ("junior", 1, 4, 1), ("legacy", 0, 0, 0)):
        stats = client.get("/api/stats/1", params={"education_stage": stage})
        assert stats.status_code == 200, stats.text
        payload = stats.json()
        assert (payload["total_gradings"], payload["total_questions"], payload["correct_count"]) == (gradings, questions, correct)
        summary = client.post("/api/reports/summary", json={"period": "all", "education_stage": stage})
        assert summary.status_code == 200, summary.text
        assert summary.json()["total"] == gradings
        assert summary.json()["total_questions"] == questions
        detail = client.get("/api/student/1/detail", params={"education_stage": stage})
        assert detail.status_code == 200, detail.text
        assert len(detail.json()["exam_scores"]) == gradings
        if stage == "senior":
            assert {item["score"] for item in detail.json()["exam_scores"]} == {10.5, 20.5, 40.5}
            assert {item["grading_result_id"] for item in detail.json()["wrong_questions"]} == {1, 2}
            assert detail.json()["correction_rate"] == 0.5
        if stage == "junior":
            assert {item["grading_result_id"] for item in detail.json()["wrong_questions"]} == {3}
    shares = client.get("/api/class/1/letter-shares", params={"education_stage": "senior"})
    assert shares.status_code == 200, shares.text
    assert {item["id"] for item in shares.json()["shares"]} == {1, 2, 4}
    user.education_stage = "junior"
    assert {item["id"] for item in client.get("/api/teacher/bank").json()["items"]} == {3}
    with factory() as db:
        assert db.get(User, 1).education_stage == "senior"  # The migration never rewrites teacher profiles.


def test_manual_correction_and_immutable_snapshots_survive_real_schema_adoption(historical_database, tmp_path):
    path, factory, _, _, chapter, topic = historical_database
    immutable = ("question_attempt_events", "wrong_question_cycles", "users", "students", "teacher_classes")
    before = {table: _rows(path, table) for table in (*migration.TABLES, *immutable)}
    result, args = _apply(path, tmp_path)
    assert result["mode"] == "applied"
    for table in migration.TABLES:
        after = _rows(path, table)
        assert len(after) == len(before[table])
        for old, new in zip(before[table], after):
            allowed = migration.CHANGED_FIELDS[table]
            assert {key: value for key, value in old.items() if key not in allowed} == {
                key: value for key, value in new.items() if key not in allowed
            }
            if table != "weakness_overrides" and old["education_stage"] in {"junior", "senior"}:
                assert old == new
        assert _rows(args["backup_path"], table) == before[table]
    for table in immutable:
        assert _rows(path, table) == before[table]
    with factory() as db:
        senior = analytics.weakness_list(db, 1, "senior")
        assert len(senior) == 1
        assert senior[0]["manual"] is True and senior[0]["status"] == "corrected"
        assert senior[0]["topic_key"] == f"senior||{chapter}||{topic}"
        assert senior[0]["occurrences"] == 3
        junior = analytics.weakness_list(db, 1, "junior")
        assert junior[0]["status"] == "pending" and junior[0]["occurrences"] == 3
        for attempt_id in (1, 2):
            assert attempt_teaching_context(db, db.get(QuestionAttemptEvent, attempt_id)) == make_teaching_context("senior")
        items = wrong_question_summary(db, 1, teaching_context=make_teaching_context("senior"))
        assert {item["grading_result_id"] for item in items} == {1, 2}
        assert {item["education_stage"] for item in items} == {"senior"}
        assert all(item["catalog_version"] == catalog_version("senior") for item in items)
    replay = migration.apply_database(path, **args)
    assert replay["mode"] == "already-applied"
    for table in immutable:
        assert _rows(path, table) == before[table]
