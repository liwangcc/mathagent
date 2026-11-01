"""Question-level correction tracking and observation-mode audit models."""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from database import Base


def _json_load(raw: str | None, fallback):
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError):
        return fallback
    return value


class StudentObservationPeriod(Base):
    """Audited interval during which newly archived results stay observational."""

    __tablename__ = "student_observation_periods"
    __table_args__ = (
        Index("ix_observation_student_time", "student_id", "started_at", "ended_at"),
        Index("ix_observation_teacher", "teacher_id"),
        Index("ix_observation_class", "class_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    class_id = Column(Integer, ForeignKey("teacher_classes.id"), nullable=True)
    teacher_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    started_at = Column(DateTime, nullable=False, default=lambda: datetime.now())
    ended_at = Column(DateTime, nullable=True)
    reason = Column(String(200), nullable=False, default="")
    note = Column(Text, nullable=False, default="")
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    ended_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now())
    updated_at = Column(DateTime, nullable=False, default=lambda: datetime.now(), onupdate=lambda: datetime.now())

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "student_id": self.student_id,
            "class_id": self.class_id,
            "started_at": str(self.started_at) if self.started_at else None,
            "ended_at": str(self.ended_at) if self.ended_at else None,
            "reason": self.reason,
            "note": self.note,
            "created_by": self.created_by,
            "ended_by": self.ended_by,
            "active": self.ended_at is None,
            "created_at": str(self.created_at) if self.created_at else None,
        }


class QuestionAttemptEvent(Base):
    """Immutable top-level-question attempt created by one formal archive."""

    __tablename__ = "question_attempt_events"
    __table_args__ = (
        UniqueConstraint(
            "grading_result_id", "question_no", name="uq_question_attempt_result_question",
        ),
        Index("ix_attempt_student_identity_time", "student_id", "question_identity", "occurred_at"),
        Index("ix_attempt_student_status", "student_id", "final_result"),
        Index("ix_attempt_teacher", "teacher_id"),
        Index("ix_attempt_grading", "grading_result_id"),
        Index("ix_attempt_fingerprint", "question_fingerprint"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    class_id = Column(Integer, ForeignKey("teacher_classes.id"), nullable=True)
    teacher_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    grading_result_id = Column(Integer, ForeignKey("grading_results.id"), nullable=False)
    question_bank_id = Column(Integer, ForeignKey("question_bank.id"), nullable=True)
    question_no = Column(String(50), nullable=False, default="")
    question_identity = Column(String(160), nullable=False)
    identity_method = Column(String(32), nullable=False, default="fingerprint")
    question_fingerprint = Column(String(64), nullable=False)
    question_type = Column(String(16), nullable=False, default="solve")
    question_snapshot_json = Column(Text, nullable=False, default="{}")
    student_answer_snapshot_json = Column(Text, nullable=False, default="{}")
    final_result = Column(String(20), nullable=False)
    analytics_included = Column(Boolean, nullable=False, default=True)
    inclusion_reason = Column(String(64), nullable=False, default="included")
    observation_period_id = Column(Integer, ForeignKey("student_observation_periods.id"), nullable=True)
    source_category = Column(String(20), nullable=False, default="daily")
    assessment_purpose = Column(String(32), nullable=False, default="standard")
    occurred_at = Column(DateTime, nullable=False, default=lambda: datetime.now())
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now())

    def question_snapshot(self) -> dict:
        value = _json_load(self.question_snapshot_json, {})
        return value if isinstance(value, dict) else {}

    def to_dict(self) -> dict:
        snapshot = self.question_snapshot()
        return {
            "id": self.id,
            "student_id": self.student_id,
            "grading_result_id": self.grading_result_id,
            "question_no": self.question_no,
            "question_identity": self.question_identity,
            "identity_method": self.identity_method,
            "question_fingerprint": self.question_fingerprint,
            "question_type": self.question_type,
            "question": snapshot.get("question_text") or snapshot.get("question") or "",
            "chapter": snapshot.get("chapter") or "",
            "topic": snapshot.get("topic") or snapshot.get("knowledge_point") or "",
            "knowledge_point": snapshot.get("knowledge_point") or snapshot.get("topic") or "",
            "final_result": self.final_result,
            "analytics_included": bool(self.analytics_included),
            "inclusion_reason": self.inclusion_reason,
            "assessment_purpose": self.assessment_purpose,
            "occurred_at": str(self.occurred_at) if self.occurred_at else None,
        }


class KnowledgeEventClassificationJob(Base):
    """Durable YS classification job for one archived original question."""

    __tablename__ = "knowledge_event_classification_jobs"
    __table_args__ = (
        UniqueConstraint(
            "question_fingerprint",
            name="uq_knowledge_classification_job_fingerprint",
        ),
        Index(
            "ix_knowledge_classification_job_status_next",
            "status",
            "next_attempt_at",
        ),
        Index(
            "ix_knowledge_classification_job_lock",
            "status",
            "locked_until",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    attempt_id = Column(
        Integer,
        ForeignKey("question_attempt_events.id", ondelete="CASCADE"),
        nullable=False,
    )
    question_fingerprint = Column(String(64), nullable=False)
    status = Column(String(20), default="pending", nullable=False)
    attempts = Column(Integer, default=0, nullable=False)
    next_attempt_at = Column(DateTime, default=lambda: datetime.now(), nullable=False)
    locked_until = Column(DateTime, nullable=True)
    worker_id = Column(String(64), default="", nullable=False)
    last_error = Column(String(500), default="", nullable=False)
    last_error_category = Column(String(32), default="", nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(), nullable=False)
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(),
        onupdate=lambda: datetime.now(),
        nullable=False,
    )
    completed_at = Column(DateTime, nullable=True)


class WrongQuestionCycle(Base):
    """One correction cycle for a stable original question identity."""

    __tablename__ = "wrong_question_cycles"
    __table_args__ = (
        UniqueConstraint("student_id", "question_identity", "cycle_no", name="uq_wrong_cycle_number"),
        Index("ix_wrong_cycle_student_status", "student_id", "status"),
        Index("ix_wrong_cycle_identity", "student_id", "question_identity"),
        Index("ix_wrong_cycle_opened", "opened_attempt_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    class_id = Column(Integer, ForeignKey("teacher_classes.id"), nullable=True)
    teacher_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    question_identity = Column(String(160), nullable=False)
    question_fingerprint = Column(String(64), nullable=False)
    cycle_no = Column(Integer, nullable=False, default=1)
    status = Column(String(20), nullable=False, default="pending")
    opened_attempt_id = Column(Integer, ForeignKey("question_attempt_events.id"), nullable=False)
    closed_attempt_id = Column(Integer, ForeignKey("question_attempt_events.id"), nullable=True)
    close_method = Column(String(32), nullable=False, default="")
    relation_id = Column(Integer, ForeignKey("question_correction_relations.id"), nullable=True)
    wrong_count = Column(Integer, nullable=False, default=1)
    opened_at = Column(DateTime, nullable=False, default=lambda: datetime.now())
    closed_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=lambda: datetime.now(), onupdate=lambda: datetime.now())


class QuestionCorrectionRelation(Base):
    """Teacher-authorized asymmetric relation from a current question to a target cycle."""

    __tablename__ = "question_correction_relations"
    __table_args__ = (
        UniqueConstraint(
            "teacher_id", "source_question_fingerprint", "target_cycle_id",
            name="uq_correction_relation_source_target",
        ),
        Index("ix_correction_relation_source", "teacher_id", "source_question_fingerprint"),
        Index("ix_correction_relation_target", "target_cycle_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    teacher_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    target_cycle_id = Column(Integer, ForeignKey("wrong_question_cycles.id"), nullable=False)
    source_question_identity = Column(String(160), nullable=False)
    source_question_fingerprint = Column(String(64), nullable=False)
    relation_type = Column(String(32), nullable=False, default="teacher_authorized_similar")
    similarity_score = Column(Float, nullable=True)
    difference_summary = Column(Text, nullable=False, default="")
    authorized_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    authorized_at = Column(DateTime, nullable=False, default=lambda: datetime.now())
    revoked_at = Column(DateTime, nullable=True)


class TemplateQuestionAuthorization(Base):
    """Teacher selection saved before any particular student's attempt exists."""

    __tablename__ = "template_question_authorizations"
    __table_args__ = (
        UniqueConstraint(
            "teacher_id", "template_id", "source_question_fingerprint", "target_cycle_id",
            name="uq_template_question_authorization",
        ),
        Index("ix_template_authorization_source", "teacher_id", "source_question_fingerprint"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    teacher_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    template_id = Column(String(64), nullable=False)
    source_question_fingerprint = Column(String(64), nullable=False)
    target_cycle_id = Column(Integer, ForeignKey("wrong_question_cycles.id"), nullable=False)
    authorized_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    authorized_at = Column(DateTime, nullable=False, default=lambda: datetime.now())
    revoked_at = Column(DateTime, nullable=True)


class CorrectionEvidence(Base):
    """Append-only link explaining why a cycle closed."""

    __tablename__ = "correction_evidence"
    __table_args__ = (
        UniqueConstraint("cycle_id", "correct_attempt_id", name="uq_correction_cycle_attempt"),
        Index("ix_correction_evidence_student", "student_id"),
        Index("ix_correction_evidence_cycle", "cycle_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    cycle_id = Column(Integer, ForeignKey("wrong_question_cycles.id"), nullable=False)
    wrong_attempt_id = Column(Integer, ForeignKey("question_attempt_events.id"), nullable=False)
    correct_attempt_id = Column(Integer, ForeignKey("question_attempt_events.id"), nullable=False)
    relation_id = Column(Integer, ForeignKey("question_correction_relations.id"), nullable=True)
    match_method = Column(String(32), nullable=False)
    match_confidence = Column(Float, nullable=True)
    confirmed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now())
