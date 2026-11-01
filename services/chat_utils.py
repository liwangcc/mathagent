"""chat 工具函数 — 时间解析、班级过滤、标签"""

from datetime import date, timedelta

from sqlalchemy.orm import Session
from sqlalchemy import or_

from models import Student
from models_rate_limit import TeacherClass
from services.security import escape_like
from services.student_service import student_class_name


def resolve_time_range(params: dict) -> tuple:
    """将自然语言时间范围解析为 (start_date, end_date)。"""
    time_range = params.get("time_range", "")
    start = params.get("start_date")
    end = params.get("end_date")
    today = date.today()

    if start and end:
        return start, end
    if start:
        return start, str(today)

    if time_range in ("this_week", "本周", "这周"):
        monday = today - timedelta(days=today.weekday())
        return str(monday), str(today)
    elif time_range in ("last_week", "上周"):
        last_monday = today - timedelta(days=today.weekday() + 7)
        last_sunday = last_monday + timedelta(days=6)
        return str(last_monday), str(last_sunday)
    elif time_range in ("this_month", "本月", "这个月"):
        return str(today.replace(day=1)), str(today)
    elif time_range in ("last_month", "上个月", "上月"):
        first_day_this_month = today.replace(day=1)
        last_day_last_month = first_day_this_month - timedelta(days=1)
        first_day_last_month = last_day_last_month.replace(day=1)
        return str(first_day_last_month), str(last_day_last_month)

    # Default: last 7 days
    return str(today - timedelta(days=7)), str(today)


def student_class_filter(db: Session, teacher_id: int, class_name: str):
    """构建班级过滤条件，兼容 class_id 和旧 class_name。"""
    cname = (class_name or "").strip()
    class_ids = [
        r[0]
        for r in db.query(TeacherClass.id).filter(
            TeacherClass.teacher_id == teacher_id,
            TeacherClass.class_name == cname,
            TeacherClass.deleted_at.is_(None),
        ).all()
    ]
    filters = [Student.class_name == cname]
    if class_ids:
        filters.insert(0, Student.class_id.in_(class_ids))
    return or_(*filters)


def find_student_by_name(db: Session, teacher_id: int, name: str):
    """按姓名模糊查找学生（限定当前老师）。"""
    safe = escape_like(name)
    return db.query(Student).filter(
        Student.teacher_id == teacher_id,
        Student.name.like(f"%{safe}%", escape='\\'),
    ).first()


def student_class_label(db: Session, student: Student) -> str:
    """获取学生的班级名称，统一以 class_id 关系为准。"""
    return student_class_name(db, student) or "未分班"
