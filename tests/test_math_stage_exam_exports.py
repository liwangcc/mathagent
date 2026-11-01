"""Archived export scope tests using only synthetic in-memory records."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from database import Base
import models_user  # noqa: F401
from models import GradingResult, Student
from routers import exam_export as routes
from services import error_sheet, exam_export
from services.teaching_context import make_teaching_context, use_teaching_context


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for number, stage in enumerate(("junior", "senior", ""), 1):
            student = Student(id=number, teacher_id=7, name=f"模拟{stage or 'legacy'}", student_no=str(number), class_name="测试班")
            session.add(student)
            row = GradingResult(student_id=number, education_stage=stage, exam_name="同名考试", category="exam", score=5, max_score=10, total_questions=1, correct_count=0, wrong_count=1, analytics_included=True)
            row.set_details([{"question_no": "12", "question": f"{stage or 'legacy'}独立题干", "result": "wrong", "student_answer": "0", "correct_answer": "1"}])
            session.add(row)
        session.commit()
        yield session


def test_exam_listing_separates_same_name_by_persisted_stage(db):
    listed = exam_export.list_exams(db, 7)
    assert {row["education_stage"] for row in listed} == {"junior", "senior", "legacy"}
    assert all(row["student_count"] == 1 for row in listed)
    assert [row["education_stage"] for row in exam_export.list_exams(db, 7, education_stage="senior")] == ["senior"]
    assert exam_export.list_exams(db, 8) == []


@pytest.mark.parametrize("export", [exam_export.export_exam_scores, error_sheet.export_error_sheet_by_student, error_sheet.export_error_sheet_by_question, error_sheet.export_error_sheet_html])
def test_unspecified_mixed_export_fails_closed_instead_of_mixing_ranks_or_question_numbers(db, export):
    result = export(db, 7, "同名考试")
    assert result["code"] == "ambiguous_export_stage"
    assert result["education_stages"] == ["junior", "legacy", "senior"]


def test_explicit_historical_stage_ignores_current_profile_context(db):
    with use_teaching_context(make_teaching_context("junior")):
        for stage in ("senior", "legacy"):
            workbook = load_workbook(exam_export.export_exam_scores(db, 7, "同名考试", education_stage=stage), read_only=True)
            assert [row[2].value for row in workbook.active.iter_rows(min_row=2)] == [f"模拟{stage}"]
            html = error_sheet.export_error_sheet_html(db, 7, "同名考试", education_stage=stage)
            assert f"{stage}独立题干" in html
            assert "junior独立题干" not in html


@pytest.mark.parametrize("export", [error_sheet.export_error_sheet_by_student, error_sheet.export_error_sheet_by_question])
def test_both_pdf_exports_select_only_archived_stage_before_grouping(db, monkeypatch, export):
    collected = []
    original = error_sheet._collect_errors
    def capture(records):
        collected.extend(row.education_stage for row in records)
        return original(records)
    monkeypatch.setattr(error_sheet, "_collect_errors", capture)
    result = export(db, 7, "同名考试", education_stage="senior")
    assert result.getvalue().startswith(b"%PDF")
    assert collected == ["senior"]


def test_export_api_returns_conflict_and_forwards_explicit_legacy_stage(db):
    user = SimpleNamespace(id=7, education_stage="junior")
    with pytest.raises(HTTPException) as caught:
        routes.api_export_scores(routes.ExamNameRequest(exam_name="同名考试"), db, user)
    assert caught.value.status_code == 409
    response = routes.api_error_sheet_preview("同名考试", class_name=None, mode="student", db=db, user=user, education_stage="legacy")
    assert "legacy独立题干" in response.body.decode()
    assert "junior独立题干" not in response.body.decode()
