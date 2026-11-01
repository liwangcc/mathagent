from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models  # noqa: F401
import models_rate_limit  # noqa: F401
import models_user  # noqa: F401
from database import Base
from models import GradingResult, Student
from models_user import User
from services import error_sheet, exam_export, exporter


def _session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def test_exam_export_selection_and_scores_exclude_hidden_records():
    """A teacher-hidden result cannot be selected or emitted by exam export services."""
    Session = _session_factory()
    db = Session()
    try:
        teacher = User(id=1, name="教师", role="teacher")
        db.add(teacher)
        db.commit()
        student = Student(id=1, teacher_id=teacher.id, name="学生", student_no="001", class_name="一班")
        db.add(student)
        db.commit()
        hidden = GradingResult(
            student_id=student.id,
            exam_name="隐藏考试",
            score=100,
            total_questions=1,
            correct_count=1,
            deleted_at=datetime.now(),
        )
        db.add(hidden)
        db.commit()

        assert exam_export.list_exams(db, teacher.id) == []
        assert exam_export.list_classes(db, teacher.id) == []
        result = exam_export.export_exam_scores(db, teacher.id, "隐藏考试")
        assert result == {"error": "未找到考试 '隐藏考试' 的成绩记录"}
        assert error_sheet.export_error_sheet_by_student(db, teacher.id, "隐藏考试") == {
            "error": "未找到考试 '隐藏考试' 的成绩记录"
        }
        assert exporter.export_error_cards_html(db, teacher.id, "隐藏考试") == {
            "error": "未找到考试 '隐藏考试' 的成绩记录"
        }
    finally:
        db.close()
