from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models  # noqa: F401
import models_user  # noqa: F401
import models_rate_limit  # noqa: F401
from database import Base
from models import ExamScore, GradingResult, Student
from models_rate_limit import TeacherClass
from models_user import User


@pytest.fixture
def chat_db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    db = Session()
    db.add(User(id=1, name="老师", role="teacher"))
    db.add(TeacherClass(id=1, teacher_id=1, class_name="高一1班"))
    db.add(Student(id=1, teacher_id=1, class_id=1, name="张三"))
    db.add(Student(id=2, teacher_id=1, class_id=1, name="李四", student_no="002"))
    db.add(
        ExamScore(
            student_id=1,
            class_id=1,
            teacher_id=1,
            exam_name="六月月考",
            score=88,
            exam_date=date.today(),
        )
    )
    db.add(
        ExamScore(
            student_id=2,
            class_id=1,
            teacher_id=1,
            exam_name="六月月考",
            score=92,
            exam_date=date.today(),
        )
    )
    result = GradingResult(student_id=1, exam_name="六月月考", total_questions=1)
    result.set_details([
        {
            "question": "1+1=?",
            "student_answer": "3",
            "model_answer": "2",
            "result": "wrong",
        }
    ])
    db.add(result)
    db.commit()
    db.close()

    import services.hermes_tools as hermes_tools

    monkeypatch.setattr(hermes_tools, "_get_db", Session)
    hermes_tools.set_current_user_id(1)
    yield Session


def test_hermes_query_student_returns_imported_exam_scores(chat_db):
    from services.hermes_tools import _query_student

    response = _query_student({"student_name": "张三"})

    assert "高一1班" in response
    assert "六月月考" in response
    assert "88分" in response
    assert "没有批改记录" not in response


def test_hermes_summary_filters_class_by_class_id(chat_db):
    from services.hermes_tools import _query_summary

    response = _query_summary({"type": "exam", "class_name": "高一1班", "time_range": "this_month"})

    assert "月考成绩（2条记录）" in response
    assert "平均90.0分" in response


def test_hermes_pending_counts_model_answers_without_status_column(chat_db):
    from services.hermes_tools import _query_pending

    response = _query_pending({})

    assert "当前有 1 条待入库题目" in response


def test_hermes_export_scores_uses_imported_exam_scores(chat_db):
    from services.hermes_tools import _export_scores_excel

    response = _export_scores_excel({"exam_name": "六月月考", "class_name": "高一1班"})

    assert "成绩表已生成" in response
    assert "（2条记录）" in response
    assert "/api/download/" in response
