"""Teacher limits and daily original student-image allowance."""

import logging
from datetime import date

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import Student
from models_rate_limit import TeacherClass, UsageEvent
from models_user import User

logger = logging.getLogger(__name__)

# ── 限流规则常量 ──
MAX_CLASSES_PER_TEACHER = 2       # 每老师最多2个班级
MAX_STUDENTS_PER_CLASS = 60       # 每班最多60人
MAX_STUDENTS_PER_TEACHER = MAX_CLASSES_PER_TEACHER * MAX_STUDENTS_PER_CLASS  # 120人
DAILY_STUDENT_IMAGE_LIMIT = 400
STUDENT_IMAGE_USAGE_SOURCE = "student_upload"


class RateLimiter:
    """限流服务 — 所有方法基于数据库实时查询。"""

    def __init__(self, db: Session):
        self.db = db

    def _is_admin(self, teacher_id: int) -> bool:
        user = self.db.get(User, teacher_id)
        return bool(user and (user.role or "").lower() == "admin")

    # ── 核心检查 ──

    def check_daily_image_limit(
        self,
        teacher_id: int,
        requested_images: int = 0,
    ) -> dict:
        """Check this natural day's original student-image allowance."""
        requested_images = max(0, int(requested_images or 0))
        today = date.today()
        next_day = today.fromordinal(today.toordinal() + 1)
        if self._is_admin(teacher_id):
            return {
                "allowed": True,
                "unlimited": True,
                "current": 0,
                "limit": None,
                "remaining": None,
                "requested": requested_images,
                "reset_time": "",
            }
        current = self._daily_student_image_count(teacher_id, today)
        return {
            "allowed": current + requested_images <= DAILY_STUDENT_IMAGE_LIMIT,
            "current": current,
            "limit": DAILY_STUDENT_IMAGE_LIMIT,
            "remaining": max(0, DAILY_STUDENT_IMAGE_LIMIT - current),
            "requested": requested_images,
            "reset_time": f"{next_day} 00:00:00",
        }

    check_monthly_image_limit = check_daily_image_limit

    def reserve_student_images(
        self,
        teacher_id: int,
        count: int,
        *,
        batch_id: str,
    ) -> dict:
        """Idempotently reserve one accepted upload batch against the day."""
        count = int(count or 0)
        if count <= 0 or not batch_id:
            raise ValueError("student image reservation requires count and batch_id")
        if self._is_admin(teacher_id):
            return self.check_monthly_image_limit(teacher_id, count)

        existing = self.db.query(UsageEvent).filter(
            UsageEvent.teacher_id == teacher_id,
            UsageEvent.batch_id == batch_id,
        ).first()
        if existing is not None:
            return self.check_monthly_image_limit(teacher_id)

        today = date.today()
        event = UsageEvent(
            teacher_id=teacher_id,
            batch_id=batch_id,
            record_date=today,
            count=count,
            source=STUDENT_IMAGE_USAGE_SOURCE,
        )
        try:
            # The insert obtains SQLite's write lock before the total is read,
            # so concurrent workers serialize their reservations.
            self.db.add(event)
            self.db.flush()
            current = self._daily_student_image_count(teacher_id, today)
            if current > DAILY_STUDENT_IMAGE_LIMIT:
                self.db.delete(event)
                self.db.commit()
                return self.check_monthly_image_limit(teacher_id, count)
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            return self.check_monthly_image_limit(teacher_id)
        return self.check_monthly_image_limit(teacher_id)

    def check_class_limit(self, teacher_id: int) -> dict:
        """检查班级数量是否超限。

        Returns:
            {"allowed": bool, "current": int, "limit": int}
        """
        count = self.db.query(TeacherClass).filter(
            TeacherClass.teacher_id == teacher_id
        ).count()
        return {
            "allowed": count < MAX_CLASSES_PER_TEACHER,
            "current": count,
            "limit": MAX_CLASSES_PER_TEACHER,
        }

    # ── 记录使用 ──

    # ── 统计查询 ──

    def get_usage_stats(self, teacher_id: int) -> dict:
        """Return daily image allowance plus class/student limits."""
        daily = self.check_daily_image_limit(teacher_id)
        if self._is_admin(teacher_id):
            return {
                "daily_images": daily,
                "monthly_images": daily,
                "students": {"total": self._get_student_count(teacher_id), "limit": None},
                "classes": {"list": [], "count": 0, "limit": None},
                "reset_time": "",
                "unlimited": True,
            }

        binding_count = self._get_student_count(teacher_id)
        classes = self.db.query(TeacherClass).filter(
            TeacherClass.teacher_id == teacher_id
        ).all()
        return {
            "daily_images": daily,
            "monthly_images": daily,
            "students": {
                "total": binding_count,
                "limit": MAX_STUDENTS_PER_TEACHER,
            },
            "classes": {
                "list": [c.to_dict() for c in classes],
                "count": len(classes),
                "limit": MAX_CLASSES_PER_TEACHER,
            },
            "reset_time": daily["reset_time"],
        }

    def get_limits_info(self) -> dict:
        """获取限流规则说明。"""
        return {
            "max_classes_per_teacher": MAX_CLASSES_PER_TEACHER,
            "max_students_per_class": MAX_STUDENTS_PER_CLASS,
            "max_students_per_teacher": MAX_STUDENTS_PER_TEACHER,
            "daily_student_image_limit": DAILY_STUDENT_IMAGE_LIMIT,
            "monthly_student_image_limit": DAILY_STUDENT_IMAGE_LIMIT,
            "upload_count_rule": "每张原始学生图片计1张；模板、裁剪和重试不重复计数",
        }

    # ── 内部方法 ──

    def _get_student_count(self, teacher_id: int) -> int:
        """获取教师的学生总数。

        按 Student.teacher_id 统计（学生在批改时直接以 teacher_id 创建），
        而非 TeacherStudentBinding（该绑定表通常为空，会导致上限恒为保底值）。
        """
        return self.db.query(Student).filter(
            Student.teacher_id == teacher_id
        ).count()

    def _daily_student_image_count(self, teacher_id: int, target_date: date) -> int:
        value = self.db.query(func.coalesce(func.sum(UsageEvent.count), 0)).filter(
            UsageEvent.teacher_id == teacher_id,
            UsageEvent.record_date == target_date,
            UsageEvent.source == STUDENT_IMAGE_USAGE_SOURCE,
        ).scalar()
        return int(value or 0)
