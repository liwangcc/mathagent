"""防薅羊毛限流模型 — 教师班级/学生绑定/用量记录。"""

from datetime import date, datetime

from sqlalchemy import Column, Integer, String, Date, DateTime, ForeignKey, Index, UniqueConstraint, func

from database import Base


class TeacherClass(Base):
    """教师班级 — 每教师最多4个班。删除冷却期当前关闭（DELETE_COOLDOWN_DAYS=0）。"""
    __tablename__ = "teacher_classes"
    __table_args__ = (
        UniqueConstraint("teacher_id", "class_name", name="uq_teacher_class"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    teacher_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    class_name = Column(String(50), nullable=False)
    # student_count 已废弃，改用实时查询 get_student_count()
    student_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now())
    deleted_at = Column(DateTime, nullable=True)  # 软删除时间，非空表示已删除

    MAX_CLASSES_PER_TEACHER = 4
    DELETE_COOLDOWN_DAYS = 0  # 0 = 关闭冷却期；改回 30 即恢复"删除后30天内不可再删/建同名"

    def get_student_count(self, db) -> int:
        """实时查询班级学生数（避免冗余字段不一致问题）"""
        from models import Student
        return db.query(Student).filter(Student.class_id == self.id).count()

    def to_dict(self, db=None):
        """转字典，db 参数用于实时查询 student_count"""
        count = self.get_student_count(db) if db else self.student_count
        return {
            "id": self.id,
            "teacher_id": self.teacher_id,
            "class_name": self.class_name,
            "student_count": count,
            "created_at": str(self.created_at) if self.created_at else None,
            "deleted_at": str(self.deleted_at) if self.deleted_at else None,
        }


class TeacherStudentBinding(Base):
    """教师-学生绑定 — 学生必须先绑定才能批阅。"""
    __tablename__ = "teacher_student_bindings"
    __table_args__ = (
        UniqueConstraint("teacher_id", "student_id", name="uq_teacher_student"),
        Index("ix_binding_teacher", "teacher_id"),
        Index("ix_binding_student", "student_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    teacher_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    class_name = Column(String(50), default="")
    created_at = Column(DateTime, default=lambda: datetime.now())

    def to_dict(self):
        return {
            "id": self.id,
            "teacher_id": self.teacher_id,
            "student_id": self.student_id,
            "class_name": self.class_name,
            "created_at": str(self.created_at) if self.created_at else None,
        }


class UsageRecord(Base):
    """每日兼容汇总 — 包含存量基线及 UsageEvent 新增用量。"""
    __tablename__ = "usage_records"
    __table_args__ = (
        UniqueConstraint("teacher_id", "record_date", name="uq_usage_teacher_date"),
        Index("ix_usage_teacher_date", "teacher_id", "record_date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    teacher_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    record_date = Column(Date, nullable=False, default=date.today)
    grade_count = Column(Integer, default=0)
    over_limit_days = Column(Integer, default=0)  # 连续超限天数
    created_at = Column(DateTime, default=lambda: datetime.now())
    updated_at = Column(DateTime, default=lambda: datetime.now(), onupdate=func.now())

    def to_dict(self):
        return {
            "id": self.id,
            "teacher_id": self.teacher_id,
            "record_date": str(self.record_date),
            "grade_count": self.grade_count,
            "over_limit_days": self.over_limit_days,
        }


class UsageEvent(Base):
    """One completed batch contribution to a teacher's daily allowance.

    The unique batch key makes retry/reconnects idempotent and keeps every
    new usage traceable to a real batch rather than a silent increment.  The
    legacy aggregate remains a compatibility floor for pre-ledger usage.
    """

    __tablename__ = "usage_events"
    __table_args__ = (
        UniqueConstraint("teacher_id", "batch_id", name="uq_usage_event_teacher_batch"),
        Index("ix_usage_event_teacher_date", "teacher_id", "record_date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    teacher_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    batch_id = Column(String(64), nullable=False)
    record_date = Column(Date, nullable=False, default=date.today)
    count = Column(Integer, nullable=False)
    source = Column(String(32), nullable=False, default="recognize")
    created_at = Column(DateTime, default=lambda: datetime.now())

    def to_dict(self):
        return {
            "id": self.id,
            "teacher_id": self.teacher_id,
            "batch_id": self.batch_id,
            "record_date": str(self.record_date),
            "count": self.count,
            "source": self.source,
            "created_at": str(self.created_at) if self.created_at else None,
        }
