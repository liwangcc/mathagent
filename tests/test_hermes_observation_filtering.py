from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models  # noqa: F401
import models_rate_limit  # noqa: F401
import models_user  # noqa: F401
from database import Base
from models import ExamScore, Student, StudentProfile
from models_user import User
from services import hermes_tools


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(User(id=7, name="教师", role="teacher"))
    student = Student(id=1, teacher_id=7, name="学生", student_no="001")
    db.add(student)
    db.flush()
    common = {
        "student_id": 1,
        "category": "daily",
        "total_questions": 1,
        "correct_count": 1,
        "partial_count": 0,
        "wrong_count": 0,
        "needs_review_count": 0,
        "correct_rate": 1.0,
    }
    db.add_all([
        StudentProfile(analytics_included=True, **common),
        StudentProfile(analytics_included=False, inclusion_reason="observation_mode", **common),
        ExamScore(student_id=1, teacher_id=7, exam_name="正式月考", score=90, analytics_included=True),
        ExamScore(student_id=1, teacher_id=7, exam_name="观察月考", score=10, analytics_included=False, inclusion_reason="observation_mode"),
    ])
    db.commit()
    return db


def test_hermes_student_query_and_summary_exclude_observation_rows(monkeypatch):
    db = _session()
    monkeypatch.setattr(hermes_tools, "_get_db", lambda: db)
    monkeypatch.setattr(hermes_tools, "_get_user_id", lambda: 7)

    student_text = hermes_tools._query_student({"student_name": "学生"})
    assert "正式月考" in student_text
    assert "观察月考" not in student_text
    assert "最近1次批改" in student_text

    db = _session()
    monkeypatch.setattr(hermes_tools, "_get_db", lambda: db)
    summary = hermes_tools._query_summary({"type": "all", "start_date": "2000-01-01", "end_date": "2100-01-01"})
    assert "月考成绩（1条记录）" in summary
    assert "批改汇总（1份试卷）" in summary
