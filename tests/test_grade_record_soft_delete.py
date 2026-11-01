from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models  # noqa: F401
import models_rate_limit  # noqa: F401
import models_user  # noqa: F401
from database import Base
from models import GradingResult, KnowledgeEvent, Student, StudentProfile
from models_user import User
from routers.grading_records import delete_grading_result, delete_grading_results, list_grading_results


def _session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_conn, _connection_record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def test_deleting_grade_record_hides_it_but_preserves_learning_history():
    """A teacher's record deletion must not erase the class analytics source data."""
    Session = _session_factory()
    db = Session()
    try:
        teacher = User(id=1, name="教师", role="teacher")
        db.add(teacher)
        db.commit()

        student = Student(id=1, teacher_id=teacher.id, name="学生", student_no="001")
        db.add(student)
        db.commit()

        result = GradingResult(
            id=1,
            student_id=student.id,
            category="daily",
            exam_name="",
            image_content_hash="soft-delete-test",
            total_questions=1,
            wrong_count=1,
            score=0.0,
        )
        db.add(result)
        db.commit()

        profile = StudentProfile(student_id=student.id, grading_result_id=result.id, score=0.0)
        event = KnowledgeEvent(
            student_id=student.id,
            teacher_id=teacher.id,
            grading_result_id=result.id,
            chapter="代数",
            topic="加法",
            result="wrong",
        )
        db.add_all([profile, event])
        db.commit()

        response = delete_grading_results([result.id], db=db, user=teacher)

        assert response["deleted"] == 1
        hidden = db.get(GradingResult, result.id)
        assert hidden is not None
        assert hidden.deleted_at is not None
        assert db.get(StudentProfile, profile.id) is not None
        assert db.get(KnowledgeEvent, event.id) is not None
        assert list_grading_results(student_id=None, db=db, user=teacher) == []
    finally:
        db.close()


def test_single_soft_delete_preserves_legacy_deleted_files_response_field():
    """Soft deletion retains the legacy response shape while deleting no image files."""
    Session = _session_factory()
    db = Session()
    try:
        teacher = User(id=1, name="教师", role="teacher")
        db.add(teacher)
        db.commit()
        student = Student(id=1, teacher_id=teacher.id, name="学生", student_no="001")
        db.add(student)
        db.commit()
        result = GradingResult(student_id=student.id, total_questions=1, score=1.0)
        db.add(result)
        db.commit()

        response = delete_grading_result(result.id, db=db, user=teacher)

        assert response == {"ok": True, "deleted_files": 0}
        assert db.get(GradingResult, result.id).deleted_at is not None
    finally:
        db.close()
