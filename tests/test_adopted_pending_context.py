"""Adopted archive pending answers, synthetic records only."""
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import models_user  # noqa: F401
from database import Base
from models import GradingResult, QuestionBank, Student
from models_user import User
from routers.pending_answers import (
    ConfirmAnswer, ConfirmRequest, _pending_sources, _source_teaching_context, confirm_answers,
)
from services.teaching_context import make_teaching_context, use_teaching_context


@pytest.mark.parametrize("stored", [{}, {"teaching_context": {}}, {"teaching_context": None}])
def test_adopted_empty_json_uses_archive_not_profile_without_mutating(stored):
    senior = make_teaching_context("senior")
    row = SimpleNamespace(education_stage="senior", catalog_version=senior["curriculum_version"])
    before = json.dumps(stored)
    with use_teaching_context(make_teaching_context("junior")):
        assert _source_teaching_context(row, stored, {"teaching_context": {}}) == senior
    assert json.dumps(stored) == before


def test_unadopted_history_stays_legacy_and_explicit_context_stays_frozen():
    row = SimpleNamespace(education_stage="", catalog_version="")
    junior = make_teaching_context("junior")
    with use_teaching_context(make_teaching_context("senior")):
        assert _source_teaching_context(row, {"teaching_context": {}}, {}) == {}
        assert _source_teaching_context(row, {"teaching_context": junior}, {}) == junior


def test_conflicting_explicit_context_is_not_silently_relabelled():
    senior, junior = (make_teaching_context(stage) for stage in ("senior", "junior"))
    row = SimpleNamespace(education_stage="senior", catalog_version=senior["curriculum_version"])
    with pytest.raises(HTTPException) as caught:
        _source_teaching_context(row, {"teaching_context": junior}, {})
    assert caught.value.status_code == 409
    with pytest.raises(HTTPException):
        _source_teaching_context(row, {"teaching_context": senior}, {"teaching_context": junior})


def test_confirm_adopted_pending_answer_keeps_archive_and_writes_senior_bank():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    senior = make_teaching_context("senior")
    try:
        with Session(engine) as db:
            user = User(id=1, name="synthetic", education_stage="junior", role="teacher")
            db.add_all([user, Student(id=1, teacher_id=1, name="synthetic")])
            result = GradingResult(student_id=1, education_stage="senior",
                catalog_version=senior["curriculum_version"], grading_contract_hash="original-contract",
                grading_details=json.dumps([{"question": "synthetic pending question", "model_answer": "2",
                    "teaching_context": {}, "result": "wrong"}]), ocr_raw_json='{"teaching_context": {}}')
            db.add(result)
            db.commit()
            before = (result.grading_details, result.ocr_raw_json, result.grading_contract_hash)
            with use_teaching_context(make_teaching_context("junior")):
                assert list(_pending_sources(db, user))[0][3] == senior
                response = confirm_answers(ConfirmRequest(answers=[ConfirmAnswer(
                    question="synthetic pending question", answer="2", grading_id=result.id, detail_index=0,
                )]), db=db, user=user)
            assert response["imported"] == 1
            question = db.query(QuestionBank).one()
            assert question.education_stage == "senior"
            assert question.catalog_version == senior["curriculum_version"]
            db.refresh(result)
            assert (result.grading_details, result.ocr_raw_json, result.grading_contract_hash) == before
    finally:
        engine.dispose()
