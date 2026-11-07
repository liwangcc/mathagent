"""Bank knowledge labels: reuse grading labels, retry rejects, teacher picks.

Synthetic records only; no model is called.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import models_accelerator  # noqa: F401
import models_rate_limit  # noqa: F401
import models_user  # noqa: F401
from database import Base
from models import GradingResult, QuestionBank, QuestionClassificationJob, Student
from models_correction import KnowledgeEventClassificationJob, QuestionAttemptEvent
from routers import pending_answers, teacher
from services import question_classification as classification
from services.math_curriculum import catalog_version, get_knowledge_point, get_math_catalog
from services.teaching_context import make_teaching_context
from tests.grade_family import set_grade_global
from routers import grade_report

SENIOR = make_teaching_context("senior")
_MODULE = get_math_catalog("senior")[0]
LABEL = (_MODULE["chapter"], _MODULE["topics"][0])
OTHER_LABEL = (_MODULE["chapter"], _MODULE["topics"][1])


def user(user_id=7):
    return SimpleNamespace(id=user_id, name="模拟老师", education_stage="senior", role="teacher")


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def sessions(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'classification.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(classification, "SessionLocal", factory)
    yield factory
    engine.dispose()


def _graded(db, details, *, result_id=1):
    if db.get(Student, 1) is None:
        db.add(Student(id=1, teacher_id=7, name="虚构学生"))
    row = GradingResult(
        id=result_id, student_id=1, education_stage="senior",
        catalog_version=SENIOR["curriculum_version"],
    )
    row.set_details(details)
    db.add(row)
    db.commit()
    return row


def _attempt(db, *, result_id, question_no, fingerprint, chapter="", topic=""):
    attempt = QuestionAttemptEvent(
        student_id=1, teacher_id=7, grading_result_id=result_id, question_no=question_no,
        question_identity=f"fingerprint:{fingerprint}", question_fingerprint=fingerprint,
        question_snapshot_json=json.dumps({
            "question_text": "合成题干", "chapter": chapter, "topic": topic,
            "knowledge_point": topic, "teaching_context": SENIOR,
        }, ensure_ascii=False),
        final_result="wrong",
    )
    db.add(attempt)
    db.commit()
    return attempt


def _confirm(db, source, **picked):
    answer = pending_answers.ConfirmAnswer(
        question=source["question"], answer=source["answer"],
        grading_id=source["grading_id"], detail_index=source["detail_index"], **picked,
    )
    return pending_answers.confirm_answers(pending_answers.ConfirmRequest(answers=[answer]), db, user())


def test_pending_list_reuses_grading_labels_and_flags_failures(db):
    _graded(db, [
        {"question_no": "1", "question": "带批改标签", "model_answer": "1",
         "chapter": LABEL[0], "topic": LABEL[1]},
        {"question_no": "2", "question": "批改未分类", "model_answer": "2",
         "chapter": "未知", "topic": "未分类"},
        {"question_no": "3", "question": "标签在归档快照", "model_answer": "3"},
        {"question_no": "4", "question": "归档分类中", "model_answer": "4"},
        {"question_no": "5", "question": "目录外标签", "model_answer": "5",
         "chapter": "不存在的章节", "topic": "不存在的知识点"},
    ])
    _attempt(db, result_id=1, question_no="3", fingerprint="f3", chapter=OTHER_LABEL[0], topic=OTHER_LABEL[1])
    waiting = _attempt(db, result_id=1, question_no="4", fingerprint="f4")
    db.add(KnowledgeEventClassificationJob(attempt_id=waiting.id, question_fingerprint="f4", status="pending"))
    db.commit()

    items = {
        row["question"]: row
        for row in pending_answers.get_pending_answers(page=1, page_size=50, db=db, user=user())["items"]
    }

    assert (items["带批改标签"]["chapter"], items["带批改标签"]["topic"]) == LABEL
    assert items["带批改标签"]["classification_status"] == "classified"
    assert items["带批改标签"]["classification_source"] == "grading"
    assert (items["标签在归档快照"]["chapter"], items["标签在归档快照"]["topic"]) == OTHER_LABEL
    assert items["归档分类中"]["classification_status"] == "pending"
    for question in ("批改未分类", "目录外标签"):
        assert items[question]["classification_status"] == "failed"
        assert (items[question]["chapter"], items[question]["topic"]) == ("", "")


def test_confirm_reuses_grading_label_as_suggestion(db):
    _graded(db, [{"question_no": "1", "question": "带批改标签", "model_answer": "1",
                  "chapter": LABEL[0], "topic": LABEL[1]}])
    source = pending_answers.get_pending_answers(page=1, page_size=50, db=db, user=user())["items"][0]

    assert _confirm(db, source, chapter=source["chapter"], topic=source["topic"])["imported"] == 1

    row = db.query(QuestionBank).one()
    assert (row.chapter, row.topic) == LABEL
    assert row.classification_status == "suggested"
    assert row.classification_source == "grading"
    assert row.knowledge_point_id == get_knowledge_point("senior", *LABEL)["id"]
    assert db.query(QuestionClassificationJob).count() == 0


def test_confirm_from_older_client_still_reuses_grading_label(db):
    _graded(db, [{"question_no": "1", "question": "带批改标签", "model_answer": "1",
                  "chapter": LABEL[0], "topic": LABEL[1]}])
    source = pending_answers.get_pending_answers(page=1, page_size=50, db=db, user=user())["items"][0]

    _confirm(db, source)

    row = db.query(QuestionBank).one()
    assert (row.chapter, row.topic, row.classification_source) == (*LABEL, "grading")


def test_teacher_pick_for_failed_classification_is_confirmed(db):
    _graded(db, [{"question_no": "1", "question": "批改未分类", "model_answer": "2", "topic": "未分类"}])
    source = pending_answers.get_pending_answers(page=1, page_size=50, db=db, user=user())["items"][0]
    assert source["classification_status"] == "failed"

    _confirm(db, source, chapter=OTHER_LABEL[0], topic=OTHER_LABEL[1])

    row = db.query(QuestionBank).one()
    assert (row.chapter, row.topic) == OTHER_LABEL
    assert row.classification_status == "confirmed"
    assert row.classification_source == "teacher"
    assert row.knowledge_point_id == get_knowledge_point("senior", *OTHER_LABEL)["id"]


def test_teacher_override_of_grading_label_wins(db):
    _graded(db, [{"question_no": "1", "question": "带批改标签", "model_answer": "1",
                  "chapter": LABEL[0], "topic": LABEL[1]}])
    source = pending_answers.get_pending_answers(page=1, page_size=50, db=db, user=user())["items"][0]

    _confirm(db, source, chapter=OTHER_LABEL[0], topic=OTHER_LABEL[1])

    row = db.query(QuestionBank).one()
    assert (row.chapter, row.topic, row.classification_source) == (*OTHER_LABEL, "teacher")


def test_teacher_pick_outside_catalog_is_rejected(db):
    _graded(db, [{"question_no": "1", "question": "批改未分类", "model_answer": "2"}])
    source = pending_answers.get_pending_answers(page=1, page_size=50, db=db, user=user())["items"][0]

    with pytest.raises(HTTPException) as caught:
        _confirm(db, source, chapter=LABEL[0], topic="不存在的知识点")

    assert caught.value.status_code == 422
    assert db.query(QuestionBank).count() == 0


def test_unlabelled_confirm_is_left_for_background_classification(db):
    _graded(db, [{"question_no": "1", "question": "批改未分类", "model_answer": "2"}])
    source = pending_answers.get_pending_answers(page=1, page_size=50, db=db, user=user())["items"][0]

    _confirm(db, source)

    row = db.query(QuestionBank).one()
    assert (row.chapter, row.topic, row.classification_status) == ("", "", "pending")


def test_review_page_bank_add_reuses_grading_label(db):
    _graded(db, [{"question_no": "1", "question": "复核入库", "model_answer": "3",
                  "result": "wrong", "chapter": LABEL[0], "topic": LABEL[1]}])

    response = teacher.confirm_judgment(
        1, teacher.ConfirmJudgmentRequest(bank_action="add", detail_index=0), db=db, user=user(),
    )

    assert response == {"ok": True}
    row = db.query(QuestionBank).one()
    assert (row.chapter, row.topic) == LABEL
    assert (row.classification_status, row.classification_source) == ("suggested", "grading")
    assert row.knowledge_point_id == get_knowledge_point("senior", *LABEL)["id"]


def _bank_rows(sessions, count):
    db = sessions()
    rows = [
        QuestionBank(question=f"合成题{index}", answer="", education_stage="senior",
                     catalog_version=catalog_version("senior"))
        for index in range(count)
    ]
    db.add_all(rows)
    db.commit()
    ids = [row.id for row in rows]
    db.close()
    return ids


def _rejected(question_id):
    return {"id": str(question_id), "status": "rejected",
            "error": "knowledge classifier returned a point outside the catalog"}


def test_rejected_bank_row_retries_alone_while_peer_keeps_label(sessions, monkeypatch):
    clock = [datetime(2026, 10, 8, 9, 0, 0)]
    monkeypatch.setattr(classification, "_now", lambda: clock[0])
    good, bad = _bank_rows(sessions, 2)
    classification.reconcile_classification_jobs_once()
    claimed = classification.claim_classification_batch("worker", 10)

    classification.finish_classification_batch(claimed, [
        {"id": str(good), "status": "suggested", "chapter": LABEL[0], "topic": LABEL[1],
         "confidence": "high", "classification_version": catalog_version("senior")},
        _rejected(bad),
    ])

    db = sessions()
    assert db.get(QuestionBank, good).classification_status == "suggested"
    assert db.get(QuestionBank, bad).classification_status == "pending"
    job = db.query(QuestionClassificationJob).filter_by(question_id=bad).one()
    assert job.status == classification.STATUS_RETRY_WAITING
    assert job.last_error_category == "rejected"
    assert job.next_attempt_at == clock[0] + timedelta(seconds=60)
    db.close()


def test_repeatedly_rejected_bank_row_becomes_classification_failed(sessions, monkeypatch):
    clock = [datetime(2026, 10, 8, 9, 0, 0)]
    monkeypatch.setattr(classification, "_now", lambda: clock[0])
    (bad,) = _bank_rows(sessions, 1)
    classification.reconcile_classification_jobs_once()

    for attempt in range(1, classification.MAX_ATTEMPTS + 1):
        claimed = classification.claim_classification_batch("worker", 10)
        assert claimed and claimed[0]["attempts"] == attempt
        classification.finish_classification_batch(claimed, [_rejected(bad)])
        clock[0] += timedelta(hours=2)

    db = sessions()
    question = db.get(QuestionBank, bad)
    assert question.classification_status == classification.STATUS_MANUAL
    assert question.classification_error == classification.REJECTED_ERROR
    assert db.query(QuestionClassificationJob).filter_by(question_id=bad).one().status == classification.STATUS_MANUAL
    db.close()


def test_rejected_historical_question_retries_before_manual(sessions, monkeypatch):
    clock = [datetime(2026, 10, 8, 9, 0, 0)]
    monkeypatch.setattr(classification, "_now", lambda: clock[0])
    db = sessions()
    _graded(db, [{"question_no": "1", "question": "合成错题", "result": "wrong"}])
    _attempt(db, result_id=1, question_no="1", fingerprint="hist")
    db.close()
    assert classification.reconcile_knowledge_event_classification_jobs_once() == 1

    for attempt in range(1, classification.MAX_ATTEMPTS + 1):
        claimed = classification.claim_knowledge_event_classification("worker")
        assert claimed and claimed[0]["attempts"] == attempt
        classification.finish_knowledge_event_classification(claimed, [{
            "id": str(claimed[0]["attempt_id"]), "status": "rejected", "error": "outside the catalog",
        }])
        db = sessions()
        job = db.query(KnowledgeEventClassificationJob).one()
        expected = (
            classification.STATUS_RETRY_WAITING if attempt < classification.MAX_ATTEMPTS
            else classification.STATUS_MANUAL
        )
        assert job.status == expected
        db.close()
        clock[0] += timedelta(hours=2)


def _confirmed_template(tmp_path, name):
    from services.template_session import TemplateSession

    sess = TemplateSession(name, str(tmp_path), teacher_id=7, teaching_context=SENIOR)
    sess.category = "daily"
    sess.add_page(0, [{"question_number": "1", "question_text": "合成模板题", "question_type": "solve",
                       "standard_answer": "x>0", "max_score": 5}])
    sess.status = "confirmed"
    sess.save_meta()
    return sess


def _template_classifier(delay, calls):
    import asyncio

    async def classify(items):
        calls.append([item["question"] for item in items])
        await asyncio.sleep(delay)
        return [{"id": item["id"], "status": "suggested", "chapter": LABEL[0], "topic": LABEL[1],
                 "confidence": "high", "classification_version": SENIOR["curriculum_version"]}
                for item in items]
    return classify


def test_template_bank_import_waits_for_in_flight_template_labels(sessions, tmp_path, monkeypatch):
    import asyncio
    import routers.grade as grade
    from routers import grade_template

    db = sessions()

    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    calls = []
    set_grade_global(monkeypatch, "_template_knowledge_classifier", _template_classifier(0.2, calls))
    _confirmed_template(tmp_path, "tplwait")

    result = asyncio.run(grade_template.batch_grade_template_bank_import("tplwait", user=user(), db=db))

    assert result["added_to_bank"] == 1 and calls == [["合成模板题"]]
    row = db.query(QuestionBank).one()
    assert (row.chapter, row.topic) == LABEL
    assert row.knowledge_point_id == get_knowledge_point("senior", *LABEL)["id"]


def test_template_bank_import_does_not_block_past_its_bound(sessions, tmp_path, monkeypatch):
    import asyncio
    import routers.grade as grade
    from routers import grade_template

    db = sessions()

    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_template, "TEMPLATE_BANK_IMPORT_KNOWLEDGE_WAIT_S", 0.1)
    set_grade_global(monkeypatch, "_template_knowledge_classifier", _template_classifier(5, []))
    _confirmed_template(tmp_path, "tplslow")

    result = asyncio.run(asyncio.wait_for(
        grade_template.batch_grade_template_bank_import("tplslow", user=user(), db=db), 3))

    assert result["added_to_bank"] == 1
    row = db.query(QuestionBank).one()
    assert (row.chapter, row.topic, row.classification_status) == ("", "", "pending")
