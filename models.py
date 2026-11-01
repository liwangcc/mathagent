import json
from datetime import date, datetime

from sqlalchemy import Column, Integer, String, Float, Text, Date, DateTime, ForeignKey, Index, UniqueConstraint, Boolean, event, text
from sqlalchemy.orm import relationship

from database import Base
from models_teaching import TeachingStageMixin
# Register cross-module dependencies whenever legacy callers import ``models``
# before calling Base.metadata.create_all().
import models_rate_limit  # noqa: F401,E402
import models_correction  # noqa: F401,E402


def _json_field(raw: str | None) -> list | dict:
    """Parse a JSON text column, returning [] on any failure."""
    try:
        return json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []


class Student(Base):
    __tablename__ = "students"
    __table_args__ = (
        Index("ix_students_name", "name"),
        Index("ix_students_teacher", "teacher_id"),
        Index("ix_students_class", "class_id"),
        # 学号唯一性按老师隔离：不同老师可各自有学号"001"，同一老师内不可重复（P0-3）。
        UniqueConstraint("teacher_id", "student_no", name="uq_students_teacher_student_no"),
        # 准考证号属于一次学校班级的长期身份，不替代学号；空值不参与唯一约束。
        Index(
            "uq_students_teacher_class_exam_no",
            "teacher_id", "class_id", "exam_no",
            unique=True,
            sqlite_where=text("exam_no IS NOT NULL AND exam_no != ''"),
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    teacher_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    class_id = Column(Integer, ForeignKey("teacher_classes.id"), nullable=True, index=True)
    name = Column(String(50), nullable=False)
    student_no = Column(String(50), default=None, nullable=True)
    exam_no = Column(String(64), default=None, nullable=True)
    phone = Column(String(20), default=None, nullable=True)  # 家长联系电话，群发家长信校验后4位用
    class_name = Column(String(50), default="")  # 保留用于兼容/迁移
    enrolled = Column(Date, default=date.today)
    created_at = Column(DateTime, default=lambda: datetime.now())

    weaknesses = relationship("Weakness", back_populates="student", cascade="all, delete-orphan")
    grading_results = relationship("GradingResult", back_populates="student", cascade="all, delete-orphan")
    profiles = relationship("StudentProfile", back_populates="student", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "teacher_id": self.teacher_id,
            "class_id": self.class_id,
            "name": self.name,
            "student_no": self.student_no,
            "exam_no": self.exam_no,
            "phone": self.phone,
            "class_name": self.class_name,
            "enrolled": str(self.enrolled) if self.enrolled else None,
        }


class Weakness(TeachingStageMixin, Base):
    __tablename__ = "weaknesses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    chapter = Column(String(100), default="")
    topic = Column(String(100), default="")
    subtopic = Column(String(100), default="")
    error_type = Column(String(100), default="")  # 旧兼容列；不再写入学生错因分类
    difficulty = Column(Integer, default=1)
    occurrences = Column(Integer, default=1)
    status = Column(String(20), default="pending")

    student = relationship("Student", back_populates="weaknesses")

    def to_dict(self):
        return {
            "id": self.id,
            "student_id": self.student_id,
            "chapter": self.chapter or "",
            "topic": self.topic,
            "subtopic": self.subtopic,
            "education_stage": self.education_stage or "",
            "catalog_version": self.catalog_version or "",
            # Semantic student error causes were retired. Keep the legacy
            # column/API key for compatibility, but expose only technical
            # diagnostics owned by the grading safety gates.
            "error_type": sanitize_error_type(self.error_type),
            "difficulty": self.difficulty,
            "occurrences": self.occurrences,
            "status": self.status,
        }


class QuestionBank(TeachingStageMixin, Base):
    __tablename__ = "question_bank"
    __table_args__ = (
        Index("ix_question_bank_question", "question"),
        Index("ix_question_bank_chapter_topic", "chapter", "topic"),
        Index("ix_question_bank_owner", "teacher_id"),
        Index("ix_question_bank_type", "question_type"),
        Index("ix_question_bank_classification_status", "classification_status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 归属：teacher_id 为某老师 = 私库；teacher_id 为 NULL 且 is_public = 公库（P0-4）。
    # 公库仅 admin 可写，全体老师可读用；私库仅本人可读写。
    teacher_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    is_public = Column(Boolean, default=False, nullable=False)
    subject = Column(String(50), default="数学")
    knowledge_point_id = Column(String(96), nullable=False, default="", server_default="")
    chapter = Column(String(100), default="")
    topic = Column(String(100), default="")
    question_type = Column(String(16), default="unknown", nullable=False)
    # 知识点分类由后台任务异步补齐。分类未完成不影响题目入库，但必须
    # 显式记录状态，避免把空字段误认为“已经确认没有知识点”。
    classification_status = Column(String(20), default="pending", nullable=False)
    classification_source = Column(String(20), default="", nullable=False)
    classification_confidence = Column(String(10), default="", nullable=False)
    classification_version = Column(String(64), default="", nullable=False)
    classification_error = Column(String(500), default="", nullable=False)
    classified_at = Column(DateTime, nullable=True)
    difficulty = Column(Integer, default=1)
    question = Column(Text, nullable=False)
    answer = Column(Text, nullable=False)
    answer_variants = Column(Text, default="[]")
    grading_method = Column(String(20), default="exact")
    solution_steps = Column(Text, default="[]")
    source = Column(String(20), default="manual")
    source_grading_id = Column(Integer, nullable=True)
    usage_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now())
    updated_at = Column(DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now())

    def get_variants(self):
        return _json_field(self.answer_variants)

    def set_variants(self, variants: list):
        self.answer_variants = json.dumps(variants, ensure_ascii=False)

    def get_solution_steps(self):
        return _json_field(self.solution_steps)

    def set_solution_steps(self, steps: list):
        self.solution_steps = json.dumps(steps, ensure_ascii=False)

    def to_dict(self):
        return {
            "id": self.id,
            "teacher_id": self.teacher_id,
            **self.teaching_fields(),
            "knowledge_point_id": self.knowledge_point_id or "",
            "is_public": self.is_public,
            "subject": self.subject,
            "chapter": self.chapter,
            "topic": self.topic,
            "question_type": self.question_type,
            "classification_status": self.classification_status or "pending",
            "classification_source": self.classification_source or "",
            "classification_confidence": self.classification_confidence or "",
            "classification_version": self.classification_version or "",
            "classification_error": self.classification_error or "",
            "classified_at": self.classified_at.isoformat() if self.classified_at else None,
            "difficulty": self.difficulty,
            "question": self.question,
            "answer": self.answer,
            "answer_variants": self.get_variants(),
            "grading_method": self.grading_method,
            "solution_steps": self.get_solution_steps(),
            "source": self.source,
            "usage_count": self.usage_count,
        }


class QuestionClassificationJob(Base):
    """Durable, single-lane background classification task for one question."""

    __tablename__ = "question_classification_jobs"
    __table_args__ = (
        UniqueConstraint("question_id", name="uq_question_classification_job_question"),
        Index(
            "ix_question_classification_job_status_next",
            "status", "next_attempt_at",
        ),
        Index(
            "ix_question_classification_job_lock",
            "status", "locked_until",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    question_id = Column(
        Integer,
        ForeignKey("question_bank.id", ondelete="CASCADE"),
        nullable=False,
    )
    status = Column(String(20), default="pending", nullable=False)
    attempts = Column(Integer, default=0, nullable=False)
    next_attempt_at = Column(DateTime, default=lambda: datetime.now(), nullable=False)
    locked_until = Column(DateTime, nullable=True)
    worker_id = Column(String(64), default="", nullable=False)
    last_error = Column(String(500), default="", nullable=False)
    last_error_category = Column(String(32), default="", nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now(), nullable=False)
    completed_at = Column(DateTime, nullable=True)


def _initialize_question_classification(target: QuestionBank) -> None:
    """Treat a supplied chapter/topic pair as an explicit import decision."""
    unclassified = {"", "未知", "未指定", "未标注", "未分类", "综合"}
    if (
        str(target.chapter or "").strip() not in unclassified
        and str(target.topic or "").strip() not in unclassified
        and str(target.classification_status or "pending") in {"", "pending"}
    ):
        target.classification_status = "confirmed"
        target.classification_source = (
            str(target.classification_source or "").strip()
            or str(target.source or "import").strip()
            or "import"
        )[:20]
        target.classification_confidence = "high"
        target.classification_error = ""
        target.classified_at = datetime.now()


@event.listens_for(QuestionBank, "before_insert")
def _question_bank_classification_before_insert(_mapper, _connection, target):
    _initialize_question_classification(target)


@event.listens_for(QuestionBank, "before_update")
def _question_bank_classification_before_update(_mapper, _connection, target):
    _initialize_question_classification(target)


class GeneratedFile(Base):
    """Hermes 工具生成的可下载文件（错题条/成绩表/办公文档），P0-1/P0-2。

    归属隔离：下载按 id 查本表并校验 owner_id == 当前用户，杜绝凭文件名猜测越权下载。
    物理文件名用 uuid（不可猜），display_name 为友好中文名（走 Content-Disposition）。
    """
    __tablename__ = "generated_files"
    __table_args__ = (
        Index("ix_generated_files_owner", "owner_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    stored_name = Column(String(128), nullable=False, unique=True)  # output/ 下的物理文件名（uuid）
    display_name = Column(String(255), default="")                  # 下载时呈现的友好名
    mime = Column(String(128), default="application/octet-stream")
    created_at = Column(DateTime, default=lambda: datetime.now())


class GradingResult(TeachingStageMixin, Base):
    __tablename__ = "grading_results"
    __table_args__ = (
        Index("ix_grading_image_hash", "image_content_hash"),
        # 落库幂等的"真相源"：同一(学生,图片内容,类别,考试,评分规则快照)只允许一行。
        # 这是数据正确性的唯一保证——内存队列缓存（services/task_queue.py）只是
        # 性能优化，重启/多 worker 下失效不影响正确性，最终都由本约束兜底。
        # 部分索引：仅对非空 image_content_hash 生效。空 hash 表示内容 hash 算不出
        # （文件读不到等降级情况），这种行不参与去重、允许并存，否则会把无关的
        # 失败样本误判为同一张卷。索引名与 database.py 启动迁移保持一致。
        Index(
            "uq_grading_student_image_category_exam_contract",
            "student_id", "image_content_hash", "category", "exam_name", "grading_contract_hash",
            unique=True,
            sqlite_where=text("image_content_hash IS NOT NULL AND image_content_hash != '' AND deleted_at IS NULL"),
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    category = Column(String(20), default="daily", nullable=False)
    exam_name = Column(String(200), default="")
    image_content_hash = Column(String(64), default="", nullable=False)
    # Stable v2 fingerprint of the confirmed answer/scoring contract.  Empty is
    # reserved for legacy v1 records migrated from before scoring snapshots.
    grading_contract_hash = Column(String(64), default="", nullable=False)
    ocr_raw_name = Column(String(100), default="")
    ocr_confidence = Column(Float, default=0.0)
    ocr_raw_json = Column(Text, default="{}")
    total_questions = Column(Integer, default=0)
    correct_count = Column(Integer, default=0)
    partial_count = Column(Integer, default=0)
    wrong_count = Column(Integer, default=0)
    needs_review_count = Column(Integer, default=0)
    # v2: exam score is actual awarded points; daily homework stores NULL and
    # uses correct_rate. max_score=NULL identifies legacy normalized rows.
    score = Column(Float, nullable=True)
    max_score = Column(Float, nullable=True)
    correct_rate = Column(Float, nullable=True)
    scoring_version = Column(Integer, default=2, nullable=False)
    grading_mode = Column(String(10), default="precise")  # precise=LLM精批 / auto=机械比对
    grading_details = Column(Text, default="[]")
    # Frozen at archive time. Observation-period history must never re-enter
    # analytics merely because the student's current mode later changes.
    analytics_included = Column(Boolean, default=True, nullable=False)
    inclusion_reason = Column(String(64), default="included", nullable=False)
    observation_period_id = Column(Integer, ForeignKey("student_observation_periods.id"), nullable=True)
    assessment_purpose = Column(String(32), default="standard", nullable=False)
    deleted_at = Column(DateTime, nullable=True, index=True)  # Teacher-hidden; analytics history remains intact.

    student = relationship("Student", back_populates="grading_results")

    def get_details(self):
        return sanitize_error_type_fields(_json_field(self.grading_details))

    def set_details(self, details: list):
        self.grading_details = json.dumps(sanitize_error_type_fields(details), ensure_ascii=False)

    def source_assets(self) -> dict | None:
        """Expose only the safe, routed answer-sheet archive reference."""
        value = _json_field(self.ocr_raw_json)
        if not isinstance(value, dict) or value.get("source") != "answer_sheet":
            return None
        session_id = str(value.get("session_id") or "")
        pages = []
        for page in value.get("pages") or []:
            if not isinstance(page, dict):
                continue
            paper_id = str(page.get("paper_id") or "")
            if not paper_id:
                continue
            pages.append({
                "paper_id": paper_id,
                "page_index": int(page.get("page_index") or 0),
                "filename": str(page.get("filename") or ""),
            })
        if not session_id or not pages:
            return None
        return {
            "source": "answer_sheet",
            "session_id": session_id,
            "pages": pages,
        }

    def to_dict(self):
        return {
            "id": self.id,
            "student_id": self.student_id,
            "category": self.category,
            "exam_name": self.exam_name,
            "image_content_hash": self.image_content_hash,
            "grading_contract_hash": self.grading_contract_hash,
            "education_stage": self.education_stage or "",
            "catalog_version": self.catalog_version or "",
            "ocr_raw_name": self.ocr_raw_name,
            "ocr_confidence": self.ocr_confidence,
            "total_questions": self.total_questions,
            "correct_count": self.correct_count,
            "partial_count": self.partial_count,
            "wrong_count": self.wrong_count,
            "needs_review_count": self.needs_review_count,
            "score": self.score,
            "max_score": self.max_score,
            "correct_rate": self.correct_rate,
            "scoring_version": self.scoring_version,
            "grading_mode": self.grading_mode,
            "analytics_included": bool(self.analytics_included),
            "inclusion_reason": self.inclusion_reason,
            "observation_period_id": self.observation_period_id,
            "assessment_purpose": self.assessment_purpose,
            "grading_details": self.get_details(),
            "source_assets": self.source_assets(),
        }


class GradingCorrection(Base):
    """Append-only teacher verdict audit, persisted only when a review is archived."""

    __tablename__ = "grading_corrections"
    __table_args__ = (
        Index("ix_grading_corrections_grading", "grading_result_id"),
        Index("ix_grading_corrections_teacher", "teacher_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    grading_result_id = Column(Integer, ForeignKey("grading_results.id"), nullable=False)
    teacher_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    paper_id = Column(String(100), nullable=False, default="")
    question_no = Column(String(50), nullable=False, default="")
    audit_key = Column(String(64), nullable=False, unique=True)
    old_result = Column(String(20), nullable=False, default="")
    new_result = Column(String(20), nullable=False, default="")
    note = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, default=lambda: datetime.now(), nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "grading_result_id": self.grading_result_id,
            "teacher_id": self.teacher_id,
            "paper_id": self.paper_id,
            "question_no": self.question_no,
            "audit_key": self.audit_key,
            "old_result": self.old_result,
            "new_result": self.new_result,
            "note": self.note,
            "created_at": str(self.created_at) if self.created_at else None,
        }


class StudentProfile(TeachingStageMixin, Base):
    """Archive of each grading submission, organized by category."""

    __tablename__ = "student_profiles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    category = Column(String(20), default="daily")
    exam_name = Column(String(200), nullable=True)
    grading_result_id = Column(Integer, ForeignKey("grading_results.id"), nullable=True)
    score = Column(Float, nullable=True)
    max_score = Column(Float, nullable=True)
    correct_rate = Column(Float, nullable=True)
    scoring_version = Column(Integer, default=2, nullable=False)
    total_questions = Column(Integer, default=0)
    correct_count = Column(Integer, default=0)
    partial_count = Column(Integer, default=0)
    wrong_count = Column(Integer, default=0)
    needs_review_count = Column(Integer, default=0)
    details_json = Column(Text, default="[]")
    image_path = Column(String(500), default="")
    image_content_hash = Column(String(64), default="")
    analytics_included = Column(Boolean, default=True, nullable=False)
    inclusion_reason = Column(String(64), default="included", nullable=False)
    observation_period_id = Column(Integer, ForeignKey("student_observation_periods.id"), nullable=True)
    assessment_purpose = Column(String(32), default="standard", nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now())

    student = relationship("Student", back_populates="profiles")

    def get_details(self):
        return sanitize_error_type_fields(_json_field(self.details_json))

    def set_details(self, details: list):
        self.details_json = json.dumps(sanitize_error_type_fields(details), ensure_ascii=False)

    def to_dict(self):
        return {
            "id": self.id,
            "student_id": self.student_id,
            "education_stage": self.education_stage or "",
            "catalog_version": self.catalog_version or "",
            "category": self.category,
            "exam_name": self.exam_name,
            "grading_result_id": self.grading_result_id,
            "score": self.score,
            "max_score": self.max_score,
            "correct_rate": self.correct_rate,
            "scoring_version": self.scoring_version,
            "total_questions": self.total_questions,
            "correct_count": self.correct_count,
            "partial_count": self.partial_count,
            "wrong_count": self.wrong_count,
            "needs_review_count": self.needs_review_count,
            "analytics_included": bool(self.analytics_included),
            "inclusion_reason": self.inclusion_reason,
            "observation_period_id": self.observation_period_id,
            "assessment_purpose": self.assessment_purpose,
            "created_at": str(self.created_at) if self.created_at else None,
        }


# ``error_type`` remains in the schema for old databases and clients. It is no
# longer a student-error taxonomy: only these machine diagnostics may survive
# serialization. Semantic values are deliberately discarded at every boundary.
RETIRED_ERROR_TYPES = frozenset({
    "计算", "概念", "审题", "方法", "未分类",
    "计算错误", "概念不清", "审题不清", "方法不会",
})
TECHNICAL_ERROR_TYPE_LABELS = {
    "ocr_answer_alignment_suspected": "题号对齐待核对",
    "unexpected_page_type": "页型待核对",
    "ocr_unreadable": "笔迹无法可靠识别",
    "drawing_review": "图形待复核",
    "drawing_pixel_mismatch": "图形证据不一致",
    "drawing_mixed_review": "图文结果待复核",
}
ERROR_TYPE_LABELS = TECHNICAL_ERROR_TYPE_LABELS
VALID_ERROR_TYPES = set(ERROR_TYPE_LABELS)


def sanitize_error_type(raw: str | None) -> str:
    """Keep technical review diagnostics and remove retired semantic causes."""
    value = str(raw or "").strip()
    return "" if value in RETIRED_ERROR_TYPES else value


def sanitize_error_type_fields(value):
    """Strip retired causes from detail snapshots without changing other data."""
    if isinstance(value, list):
        return [sanitize_error_type_fields(item) for item in value]
    if isinstance(value, dict):
        return {
            key: sanitize_error_type(item) if key == "error_type"
            else sanitize_error_type_fields(item)
            for key, item in value.items()
        }
    return value


class KnowledgeEvent(TeachingStageMixin, Base):
    """逐题知识点事件 — 学生在某时刻、某知识点上的一次作答结果。

    成长护城河的脊椎（坑1）：从批改流程自动沉淀，老师零额外录入。
    所有成长曲线 / 班级热力图 / 纠错率 / 错题率均以本表为数据源，
    weaknesses 表退化为本表的聚合视图（保留供旧页面兼容）。
    """
    __tablename__ = "knowledge_events"
    __table_args__ = (
        Index("ix_ke_student_topic", "student_id", "chapter", "topic"),
        Index("ix_ke_student_time", "student_id", "created_at"),
        Index("ix_ke_class", "class_id"),
        Index("ix_ke_grading", "grading_result_id"),
        Index("ix_ke_teacher", "teacher_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False, index=True)
    class_id = Column(Integer, ForeignKey("teacher_classes.id"), nullable=True, index=True)  # 冗余，加速班级聚合
    knowledge_point_id = Column(String(96), nullable=False, default="", server_default="")
    teacher_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)          # 越权隔离
    grading_result_id = Column(Integer, ForeignKey("grading_results.id"), nullable=True)
    question_bank_id = Column(Integer, ForeignKey("question_bank.id"), nullable=True)          # 关联题，可空

    chapter = Column(String(100), default="")     # 大章（来自 knowledge_points.json）
    topic = Column(String(100), default="")        # 知识点
    result = Column(String(20), default="")        # correct / partial / wrong / needs_review
    error_type = Column(String(20), default="")    # 仅保留技术复核诊断；旧语义值不再暴露
    difficulty = Column(Integer, default=3)
    analytics_included = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(), index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "student_id": self.student_id,
            "education_stage": self.education_stage or "",
            "catalog_version": self.catalog_version or "",
            "class_id": self.class_id,
            "grading_result_id": self.grading_result_id,
            "question_bank_id": self.question_bank_id,
            "chapter": self.chapter,
            "topic": self.topic,
            "result": self.result,
            "error_type": sanitize_error_type(self.error_type),
            "error_type_label": ERROR_TYPE_LABELS.get(sanitize_error_type(self.error_type), ""),
            "difficulty": self.difficulty,
            "analytics_included": bool(self.analytics_included),
            "created_at": str(self.created_at) if self.created_at else None,
        }


class ExamScore(TeachingStageMixin, Base):
    """月考成绩 — Excel 导入，独立于批改流程（规划 3.4）。

    学生详情页折线图、班级表格"最近月考成绩"列的数据源。
    """
    __tablename__ = "exam_scores"
    __table_args__ = (
        Index("ix_exam_student_time", "student_id", "exam_date"),
        Index("ix_exam_class", "class_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False, index=True)
    class_id = Column(Integer, ForeignKey("teacher_classes.id"), nullable=True, index=True)
    teacher_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    exam_name = Column(String(200), default="")
    score = Column(Float, default=0.0)
    analytics_included = Column(Boolean, default=True, nullable=False)
    inclusion_reason = Column(String(64), default="included", nullable=False)
    observation_period_id = Column(Integer, ForeignKey("student_observation_periods.id"), nullable=True)
    exam_date = Column(Date, default=date.today)
    created_at = Column(DateTime, default=lambda: datetime.now())

    def to_dict(self):
        return {
            "id": self.id,
            "student_id": self.student_id,
            "education_stage": self.education_stage or "",
            "catalog_version": self.catalog_version or "",
            "exam_name": self.exam_name,
            "score": self.score,
            "analytics_included": bool(self.analytics_included),
            "inclusion_reason": self.inclusion_reason,
            "observation_period_id": self.observation_period_id,
            "exam_date": str(self.exam_date) if self.exam_date else None,
        }


class WeaknessOverride(Base):
    """老师对某学生某知识点掌握状态的手动修正（规划 3.5）。

    优先级高于 knowledge_events 自动判定。topic_key = "chapter||topic"。
    status: corrected（手动标记已纠正）/ pending（教师确认仍未纠正，覆盖自动"已纠正"）。
    教师的选择与自动判定一致时删除本行，恢复自动判定。
    """
    __tablename__ = "weakness_overrides"
    __table_args__ = (
        UniqueConstraint("student_id", "topic_key", name="uq_weakness_override"),
        Index("ix_woverride_student", "student_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False, index=True)
    teacher_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    topic_key = Column(String(220), nullable=False)   # "chapter||topic"
    status = Column(String(20), default="corrected")  # corrected / pending
    created_at = Column(DateTime, default=lambda: datetime.now())
    updated_at = Column(DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now())


class PendingAnswerDismissal(Base):
    """老师忽略的待入库题目（按题干 + 教学范围）。

    忽略只隐藏待入库建议，绝不改写 grading_results 里的正式批改记录。
    question_hash = sha256(题干)；context_key = sha256(规范化教学范围 JSON)。
    """
    __tablename__ = "pending_answer_dismissals"
    __table_args__ = (
        UniqueConstraint("teacher_id", "question_hash", "context_key", name="uq_pending_answer_dismissal"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    teacher_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    question_hash = Column(String(64), nullable=False)
    context_key = Column(String(64), nullable=False)
    question_preview = Column(String(200), default="")
    created_at = Column(DateTime, default=lambda: datetime.now())


class ParentLetterShare(TeachingStageMixin, Base):
    """致家长信群发链接（带锁）。

    群发不再让老师本地下发 docx，而是为每个学生生成一个不可猜 token 链接。
    家长打开链接需用"手机后4位"二次校验（决议⑤），杜绝链接转发即泄露。
    - token：secrets.token_urlsafe 生成，~256bit 不可枚举。
    - html：渲染好的信件正文（校验通过后才返回，杜绝 PII 在 meta 阶段泄露）。
    - fail_count：校验失败累计，超 MAX_FAIL_ATTEMPTS 锁死该 token，防手机后4位暴力枚举。
    - 三态失效（撤销/过期/锁死）对外统一表现为 404，杜绝侧信道。
    """
    __tablename__ = "parent_letter_shares"
    __table_args__ = (
        Index("ix_letter_share_token", "token"),
        Index("ix_letter_share_class", "class_id"),
        Index("ix_letter_share_teacher", "teacher_id"),
    )

    MAX_FAIL_ATTEMPTS = 8  # 手机后4位仅1万组合，8次失败即锁死防枚举

    id = Column(Integer, primary_key=True, autoincrement=True)
    token = Column(String(64), nullable=False, unique=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False, index=True)
    teacher_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    class_id = Column(Integer, ForeignKey("teacher_classes.id"), nullable=True)
    phone_last4 = Column(String(4), nullable=False)  # 校验口令，仅存后4位
    html = Column(Text, default="")                  # 渲染好的信件正文
    display_name = Column(String(255), default="")
    expires_at = Column(DateTime, nullable=True)     # 有效期，null=永久（当前不用）
    revoked_at = Column(DateTime, nullable=True)     # 手动失效时间
    view_count = Column(Integer, default=0)
    fail_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now())

    @property
    def is_locked(self) -> bool:
        return (self.fail_count or 0) >= self.MAX_FAIL_ATTEMPTS

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and datetime.now() >= self.expires_at

    @property
    def is_valid(self) -> bool:
        """可被家长访问：未撤销 且 未过期 且 未锁死。"""
        return self.revoked_at is None and not self.is_expired and not self.is_locked

    def status_label(self) -> str:
        if self.revoked_at is not None:
            return "已失效"
        if self.is_expired:
            return "已过期"
        if self.is_locked:
            return "已锁定"
        return "有效"

    def to_dict(self):
        """老师端列表用，不含 html 正文与完整手机号。"""
        return {
            "id": self.id,
            "education_stage": self.education_stage or "",
            "catalog_version": self.catalog_version or "",
            "token": self.token,
            "student_id": self.student_id,
            "display_name": self.display_name,
            "password_required": bool(self.phone_last4),
            "status": self.status_label(),
            "view_count": self.view_count or 0,
            "expires_at": str(self.expires_at) if self.expires_at else None,
            "revoked_at": str(self.revoked_at) if self.revoked_at else None,
            "created_at": str(self.created_at) if self.created_at else None,
            "url": f"/letter/{self.token}",
        }
