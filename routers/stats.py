"""统计查询接口"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models import Student, GradingResult
from models_rate_limit import TeacherClass
from models_user import User
from routers.auth import _require_user
import services.class_analytics as analytics
from services.scoring_compat import record_correct_rate
from services.student_service import student_to_dict

router = APIRouter()


@router.get("/stats/{student_id}")
def get_student_stats(
    student_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
    education_stage: Literal["primary", "junior", "senior", "legacy", ""] | None = None,
):
    """查看学生统计"""
    education_stage = education_stage or None
    student = db.query(Student).filter(
        Student.id == student_id,
        Student.teacher_id == user.id,
    ).first()
    if not student:
        raise HTTPException(404, detail="学生不存在")

    grading_results = db.query(GradingResult).filter(
        GradingResult.student_id == student_id,
        GradingResult.analytics_included.is_(True),
    ).all()
    grading_results = analytics._scoped_rows(grading_results, education_stage)
    completed_results = [
        row for row in grading_results
        if int(row.needs_review_count or 0) == 0
    ]

    weaknesses = analytics.weakness_list(db, student_id, education_stage)

    rates = [
        rate for row in completed_results
        if (rate := record_correct_rate(row)) is not None
    ]
    average_correct_rate = sum(rates) / len(rates) if rates else 0.0
    total_questions = sum(max(int(row.total_questions or 0), 0) for row in completed_results)
    correct_count = sum(max(int(row.correct_count or 0), 0) for row in completed_results)
    partial_count = sum(max(int(row.partial_count or 0), 0) for row in completed_results)
    wrong_count = sum(max(int(row.wrong_count or 0), 0) for row in completed_results)
    accuracy = (
        correct_count / total_questions
        if total_questions
        else average_correct_rate
    )
    error_distribution = [
        item for item in analytics.error_distribution(db, student_id, education_stage)
        if int(item.get("count") or 0) > 0
    ]
    knowledge_distribution = analytics.knowledge_distribution(db, student_id, education_stage)
    unclassified_knowledge_count = sum(
        int(item.get("count") or 0)
        for item in knowledge_distribution
        if item.get("topic") == "未分类"
    )
    from services.question_classification import get_classification_status

    classification = get_classification_status(db, teacher_id=user.id)

    pending_weak = sum(1 for row in weaknesses if row["status"] == "pending")
    resolved_weak = sum(1 for row in weaknesses if row["status"] == "corrected")

    return {
        "student": student_to_dict(db, student),
        "education_stage": education_stage,
        "scope_label": analytics.report_scope_label(education_stage),
        "education_stages": sorted({row.education_stage or "" for row in grading_results}),
        # Canonical fields consumed by the current statistics page.
        "total_gradings": len(completed_results),
        "total_questions": total_questions,
        "correct_count": correct_count,
        "partial_count": partial_count,
        "wrong_count": wrong_count,
        "incorrect_count": partial_count + wrong_count,
        "accuracy": round(accuracy, 4),
        "accuracy_percent": round(accuracy * 100, 1),
        # The currently deployed page labels this normalized percentage as
        # average score. Keep it until that page is replaced by the new
        # question-count card in the next complete frontend release.
        "avg_score": round(average_correct_rate * 100, 2),
        # Compatibility aliases retained for older clients.
        "total_exams": len(completed_results),
        "average_score": round(average_correct_rate, 2),
        "average_correct_rate": round(average_correct_rate, 4),
        "total_weaknesses": len(weaknesses),
        "pending_weaknesses": pending_weak,
        "resolved_weaknesses": resolved_weak,
        # Canonical distribution is knowledge point only. Keep the empty
        # compatibility field for older deployed clients.
        "knowledge_distribution": knowledge_distribution,
        "unclassified_knowledge_count": unclassified_knowledge_count,
        "question_classification": classification,
        "error_distribution": error_distribution,
    }


@router.get("/dashboard/overview")
def get_dashboard_overview(
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
    education_stage: Literal["primary", "junior", "senior", "legacy", ""] | None = None,
):
    """仪表盘「本周概览」4 张统计卡（仅当前老师数据）。"""
    return analytics.dashboard_overview(db, user.id, education_stage or None)


@router.get("/stats/class/{class_name}")
def get_class_stats(
    class_name: str,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
    education_stage: Literal["primary", "junior", "senior", "legacy", ""] | None = None,
):
    """查看班级统计"""
    education_stage = education_stage or None
    teacher_class = db.query(TeacherClass).filter(
        TeacherClass.class_name == class_name,
        TeacherClass.teacher_id == user.id,
        TeacherClass.deleted_at.is_(None),
    ).first()
    student_query = db.query(Student).filter(Student.teacher_id == user.id)
    if teacher_class:
        student_query = student_query.filter(
            (Student.class_id == teacher_class.id)
            | (Student.class_name == teacher_class.class_name)
        )
    else:
        student_query = student_query.filter(Student.class_name == class_name)
    students = student_query.all()
    if not students:
        raise HTTPException(404, detail="班级不存在或没有学生")

    student_ids = [s.id for s in students]

    class_results = db.query(GradingResult).filter(
        GradingResult.student_id.in_(student_ids),
        GradingResult.analytics_included.is_(True),
    ).all()
    class_results = analytics._scoped_rows(class_results, education_stage)
    total_exams = len(class_results)
    rates = [rate for row in class_results if (rate := record_correct_rate(row)) is not None]
    avg_score = sum(rates) / len(rates) if rates else 0.0

    weakness_totals: dict[tuple, int] = {}
    for student_id in student_ids:
        for weakness in analytics.weakness_list(db, student_id, education_stage):
            if weakness["status"] != "pending":
                continue
            topic = (weakness["topic"], weakness["education_stage"], weakness["catalog_version"])
            weakness_totals[topic] = weakness_totals.get(topic, 0) + int(
                weakness["occurrences"] or 0
            )
    weakness_counts = sorted(
        weakness_totals.items(), key=lambda item: (-item[1], item[0])
    )[:10]
    knowledge_distribution = analytics.class_knowledge_distribution(db, student_ids, education_stage=education_stage)
    unclassified_knowledge_count = sum(
        int(item.get("count") or 0)
        for item in knowledge_distribution
        if item.get("topic") == "未分类"
    )

    return {
        "class_name": teacher_class.class_name if teacher_class else class_name,
        "education_stage": education_stage,
        "scope_label": analytics.report_scope_label(education_stage),
        "education_stages": sorted({row.education_stage or "" for row in class_results}),
        "student_count": len(students),
        "total_exams": total_exams,
        "average_score": round(float(avg_score), 2),
        "average_correct_rate": round(float(avg_score), 4),
        "top_weaknesses": [
            {"topic": topic[0], "education_stage": topic[1], "catalog_version": topic[2], "total_occurrences": total}
            for topic, total in weakness_counts
        ],
        "knowledge_distribution": knowledge_distribution,
        "unclassified_knowledge_count": unclassified_knowledge_count,
    }
