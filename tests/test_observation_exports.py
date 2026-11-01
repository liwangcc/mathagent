from __future__ import annotations

from datetime import datetime, timedelta

from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models  # noqa: F401
import models_rate_limit  # noqa: F401
import models_user  # noqa: F401
from database import Base
from models import GradingResult, Student
from services.exam_export import export_exam_scores, list_exams
from services.exporter import export_student_list_excel


def _session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    included = Student(id=1, teacher_id=7, name="正式学生", student_no="001", class_name="一班")
    observed = Student(id=2, teacher_id=7, name="观察学生", student_no="002", class_name="一班")
    db.add_all([included, observed])
    db.flush()
    common = {
        "category": "exam",
        "exam_name": "月考",
        "total_questions": 1,
        "correct_count": 1,
        "partial_count": 0,
        "wrong_count": 0,
        "needs_review_count": 0,
        "score": 100,
        "max_score": 100,
        "correct_rate": 1.0,
    }
    db.add_all([
        GradingResult(student_id=1, image_content_hash="a" * 64, analytics_included=True, **common),
        GradingResult(
            student_id=2,
            image_content_hash="b" * 64,
            analytics_included=False,
            inclusion_reason="observation_mode",
            **common,
        ),
    ])
    db.commit()
    return db


def _sheet_names(buffer) -> list[str]:
    workbook = load_workbook(buffer, read_only=True)
    sheet = workbook.active
    return [str(row[0].value) for row in sheet.iter_rows(min_row=2) if row[0].value is not None]


def test_observation_results_are_excluded_from_both_score_exports_and_exam_listing():
    db = _session()
    assert _sheet_names(export_student_list_excel(db, 7)) == ["正式学生"]

    workbook = load_workbook(export_exam_scores(db, 7, "月考"), read_only=True)
    names = [str(row[2].value) for row in workbook.active.iter_rows(min_row=2)]
    assert names == ["正式学生"]

    assert list_exams(db, 7) == [{
        "exam_name": "月考",
        "class_name": "一班",
        "education_stage": "legacy",
        "student_count": 1,
        "avg_score": 100.0,
    }]
