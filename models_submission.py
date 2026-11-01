"""Student self-upload: class links, uploaded pages and optional passwords.

A teacher creates a submission link for one class.  Students open it without
an account, identify themselves with name + student number from that class's
roster (plus a password only when the teacher set one), and upload their paper
page by page until the deadline.  See services/student_submission.py.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint

from database import Base


class StudentSubmissionLink(Base):
    """One class-scoped upload link with a deadline."""

    __tablename__ = "student_submission_links"
    __table_args__ = (
        Index("ix_submission_links_teacher_class", "teacher_id", "class_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Kept in clear so the teacher can show the same QR code again; it grants
    # upload rights for one class only, never read access to any image.
    token = Column(String(64), nullable=False, unique=True)
    teacher_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    class_id = Column(Integer, ForeignKey("teacher_classes.id"), nullable=False)
    title = Column(String(100), nullable=False, default="")
    closes_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now())


class StudentSubmissionPage(Base):
    """One uploaded page; the unique key is what prevents duplicate submissions."""

    __tablename__ = "student_submission_pages"
    __table_args__ = (
        UniqueConstraint("link_id", "student_id", "page_no", name="uq_submission_page"),
        Index("ix_submission_pages_link_student", "link_id", "student_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    link_id = Column(Integer, ForeignKey("student_submission_links.id", ondelete="CASCADE"), nullable=False)
    student_id = Column(Integer, ForeignKey("students.id", ondelete="CASCADE"), nullable=False)
    page_no = Column(Integer, nullable=False)
    file = Column(String(128), nullable=False)
    sha256 = Column(String(64), nullable=False)
    bytes = Column(Integer, nullable=False, default=0)
    width = Column(Integer, nullable=False, default=0)
    height = Column(Integer, nullable=False, default=0)
    uploaded_at = Column(DateTime, default=lambda: datetime.now())


class StudentSubmitPassword(Base):
    """Optional per-student password; students without one need none."""

    __tablename__ = "student_submit_passwords"

    student_id = Column(Integer, ForeignKey("students.id", ondelete="CASCADE"), primary_key=True)
    password_hash = Column(String(128), nullable=False)
    fail_count = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=lambda: datetime.now())


class StudentSubmissionThrottle(Base):
    """Failed identity checks per client address and link, in fixed windows."""

    __tablename__ = "student_submission_throttles"

    key = Column(String(128), primary_key=True)
    window_started_at = Column(DateTime, nullable=False)
    failures = Column(Integer, nullable=False, default=0)
