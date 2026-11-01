"""Mock-only import snapshots and explicit, ownership-safe legacy adoption."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from database import Base
import models_user  # noqa: F401
from models import GradingResult, QuestionBank, Student
from routers import bank_import, pending_answers, teacher
from services.teaching_context import current_teaching_context, make_teaching_context


def user(stage="junior", user_id=7, role="teacher"):
    return SimpleNamespace(id=user_id, name="模拟老师", education_stage=stage, role=role)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def item(content="计算数值", **kwargs):
    return bank_import._SaveItem(question_no="1", content=content, answer="2", **kwargs)


def test_photo_recognition_restoration_and_save_use_original_snapshot(db, tmp_path, monkeypatch):
    monkeypatch.setattr(bank_import, "UPLOAD_DIR", str(tmp_path))
    captured = []
    async def save_upload(_upload, _directory):
        return "synthetic-image-only"
    async def recognize(*_args, **_kwargs):
        captured.append(current_teaching_context())
        return {"questions": [{"question_number": "1", "question_text": "计算数值"}]}
    async def recognize_answer(*_args, **_kwargs):
        captured.append(current_teaching_context())
        return [{"question_no": "1", "answer": "2"}]
    monkeypatch.setattr(bank_import, "_save_upload", save_upload)
    monkeypatch.setattr(bank_import, "recognize_paper", recognize)
    monkeypatch.setattr(bank_import, "recognize_answer_sheet", recognize_answer)
    response = asyncio.run(bank_import.photo_import_recognize([object()], [object()], chapter="", user=user()))
    context = make_teaching_context("junior")
    assert captured == [context, context]
    restored = bank_import.photo_import_restore(response["import_id"], user=user("senior"))
    assert restored["teaching_context"] == context
    result = bank_import.photo_import_save(bank_import.SaveReq(items=[item()], import_id=response["import_id"]), db, user("senior"))
    assert result["teaching_context"] == context
    assert db.query(QuestionBank).one().education_stage == "junior"
    assert current_teaching_context() == {}
    with pytest.raises(HTTPException) as caught:
        bank_import.photo_import_restore(response["import_id"], user=user(user_id=8))
    assert caught.value.status_code == 404
    with pytest.raises(HTTPException):
        bank_import.photo_import_restore("../metadata", user=user())


def test_new_import_requires_stage_and_validates_whole_batch_before_write(db):
    with pytest.raises(HTTPException) as caught:
        bank_import.photo_import_save(bank_import.SaveReq(items=[item()]), db, user(None))
    assert caught.value.status_code == 428
    with pytest.raises(HTTPException) as caught:
        bank_import.photo_import_save(bank_import.SaveReq(items=[item(), item("跨学段", education_stage="senior")]), db, user())
    assert caught.value.status_code == 422
    assert db.query(QuestionBank).count() == 0


def test_public_and_private_dedupe_are_stage_local(db):
    for public in (False, True):
        db.add(QuestionBank(question="计算数值", answer="9", teacher_id=None if public else 7, is_public=public, education_stage="senior", catalog_version=make_teaching_context("senior")["curriculum_version"]))
    db.commit()
    req = bank_import.SaveReq(items=[item()])
    assert bank_import.photo_import_save(req, db, user())["inserted"] == 1
    assert bank_import.public_import_save(req, db, user(role="admin"))["inserted"] == 1
    assert bank_import.public_import_save(req, db, user(role="admin"))["skipped"] == 1
    assert db.query(QuestionBank).filter_by(education_stage="junior").count() == 2


def test_legacy_import_metadata_never_adopts_current_profile(db, tmp_path, monkeypatch):
    monkeypatch.setattr(bank_import, "UPLOAD_DIR", str(tmp_path))
    import_id = "b" * 32
    bank_import._write_import_metadata(import_id, {"teacher_id": 7, "paired": [], "teaching_context": {}})
    bank_import.photo_import_save(bank_import.SaveReq(items=[item()], import_id=import_id), db, user())
    assert db.query(QuestionBank).one().education_stage == ""


def test_legacy_adoption_is_explicit_atomic_and_keeps_question_answer(db, monkeypatch):
    queued = []
    monkeypatch.setattr("services.question_classification.queue_question_classification", lambda _db, row: queued.append(row.id))
    monkeypatch.setattr(teacher, "_mark_question_matcher_dirty", lambda: None)
    db.add_all([QuestionBank(id=1, teacher_id=7, question="原题", answer="原答案", chapter="不存在的旧章", topic="旧知识点"), QuestionBank(id=2, teacher_id=8, question="他人题", answer="2")])
    db.commit()
    with pytest.raises(HTTPException) as caught:
        teacher.assign_legacy_bank_stage(teacher.LegacyBankStageRequest(question_ids=[1, 2], education_stage="junior"), db, user())
    assert caught.value.status_code == 403
    assert db.get(QuestionBank, 1).education_stage == ""
    result = teacher.assign_legacy_bank_stage(teacher.LegacyBankStageRequest(question_ids=[1], education_stage="junior"), db, user())
    row = db.get(QuestionBank, 1)
    assert (row.question, row.answer) == ("原题", "原答案")
    assert (row.education_stage, row.chapter, row.topic) == ("junior", "", "")
    assert queued == [1] and result["classification_pending"] == 1
    with pytest.raises(HTTPException) as caught:
        teacher.assign_legacy_bank_stage(teacher.LegacyBankStageRequest(question_ids=[1], education_stage="senior"), db, user())
    assert caught.value.status_code == 409


def test_legacy_public_adoption_requires_admin_and_queues_unmapped_catalog(db, monkeypatch):
    from models import QuestionClassificationJob
    monkeypatch.setattr(teacher, "_mark_question_matcher_dirty", lambda: None)
    db.add(QuestionBank(id=1, teacher_id=None, is_public=True, question="历史公库数学题", answer="3", chapter="旧目录", topic="旧知识点"))
    db.commit()
    request = teacher.LegacyBankStageRequest(question_ids=[1], education_stage="junior")
    with pytest.raises(HTTPException) as caught:
        teacher.assign_legacy_bank_stage(request, db, user())
    assert caught.value.status_code == 403
    result = teacher.assign_legacy_bank_stage(request, db, user(role="admin"))
    row = db.get(QuestionBank, 1)
    assert result["classification_pending"] == 1
    assert row.classification_status == "pending"
    assert row.catalog_version == make_teaching_context("junior")["curriculum_version"]
    assert db.query(QuestionClassificationJob).filter_by(question_id=1).one().status == "pending"


def test_pending_confirmation_uses_original_detail_not_current_profile(db):
    db.add(Student(id=1, teacher_id=7, name="虚构学生"))
    for row_id, context in ((1, make_teaching_context("senior")), (2, {})):
        row = GradingResult(id=row_id, student_id=1)
        row.set_details([{"question": "同一道题", "model_answer": "2", "teaching_context": context}])
        db.add(row)
    db.commit()
    listed = pending_answers.get_pending_answers(page=1, page_size=50, db=db, user=user())
    assert len(listed["items"]) == 2
    with pytest.raises(HTTPException) as caught:
        pending_answers.confirm_answers(pending_answers.ConfirmRequest(answers=[pending_answers.ConfirmAnswer(question="同一道题", answer="2")]), db, user())
    assert caught.value.status_code == 409
    for source in listed["items"]:
        request = pending_answers.ConfirmRequest(answers=[pending_answers.ConfirmAnswer(**{key: source[key] for key in ("question", "answer", "grading_id", "detail_index")})])
        pending_answers.confirm_answers(request, db, user())
    assert {row.education_stage for row in db.query(QuestionBank).all()} == {"", "senior"}
    assert pending_answers.get_pending_answers(page=1, page_size=50, db=db, user=user())["items"] == []


def test_pending_delete_keeps_same_question_in_other_stage(db):
    db.add(Student(id=1, teacher_id=7, name="虚构学生"))
    for row_id, stage in ((1, "junior"), (2, "senior")):
        row = GradingResult(id=row_id, student_id=1)
        row.set_details([{"question": "同题", "model_answer": "2", "teaching_context": make_teaching_context(stage)}])
        db.add(row)
    db.commit()
    result = pending_answers.delete_pending_answers(pending_answers.DeletePendingRequest(questions=["同题"], sources=[pending_answers.PendingSource(grading_id=1, detail_index=0)]), db, user())
    assert result["removed"] == 1
    db.expire_all()
    # Dismissal hides the suggestion only; formal grading records stay intact.
    assert len(db.get(GradingResult, 1).get_details()) == 1
    assert len(db.get(GradingResult, 2).get_details()) == 1
    listed = pending_answers.get_pending_answers(page=1, page_size=50, db=db, user=user())["items"]
    assert [entry["grading_id"] for entry in listed] == [2]
