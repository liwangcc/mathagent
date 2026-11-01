"""Regression coverage for the daily original student-image allowance."""

from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models  # noqa: F401
import models_rate_limit  # noqa: F401
import models_user  # noqa: F401
from database import Base
from models_rate_limit import UsageEvent
from models_user import User
from services.rate_limiter import RateLimiter


def _session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_admin_is_never_blocked_or_charged():
    db = _session()
    try:
        admin = User(id=1, name="管理员", role="admin")
        db.add(admin)
        db.commit()

        limiter = RateLimiter(db)
        result = limiter.reserve_student_images(
            admin.id, 200, batch_id="student-upload-admin"
        )

        assert result["allowed"] is True
        assert result["unlimited"] is True
        assert db.query(UsageEvent).count() == 0
    finally:
        db.close()


def test_student_upload_is_charged_once_per_batch():
    db = _session()
    try:
        teacher = User(id=2, name="老师", role="teacher")
        db.add(teacher)
        db.commit()

        limiter = RateLimiter(db)
        first = limiter.reserve_student_images(
            teacher.id, 3, batch_id="student-upload-batch-123"
        )
        second = limiter.reserve_student_images(
            teacher.id, 3, batch_id="student-upload-batch-123"
        )

        assert first["current"] == 3
        assert second["current"] == 3
        assert second["remaining"] == 397
        event = db.query(UsageEvent).one()
        assert event.count == 3
        assert event.source == "student_upload"
    finally:
        db.close()


def test_daily_limit_rejects_batch_that_would_exceed_400_images():
    db = _session()
    try:
        teacher = User(id=3, name="老师", role="teacher")
        db.add(teacher)
        db.commit()
        limiter = RateLimiter(db)

        accepted = limiter.reserve_student_images(
            teacher.id, 399, batch_id="student-upload-first"
        )
        rejected = limiter.reserve_student_images(
            teacher.id, 2, batch_id="student-upload-second"
        )

        assert accepted["allowed"] is True
        assert rejected["allowed"] is False
        assert rejected["current"] == 399
        assert rejected["requested"] == 2
        assert db.query(UsageEvent).count() == 1
    finally:
        db.close()


def test_template_retry_and_internal_crop_events_do_not_consume_image_quota():
    db = _session()
    try:
        teacher = User(id=4, name="老师", role="teacher")
        db.add(teacher)
        db.commit()
        limiter = RateLimiter(db)
        for batch_id, source in (("legacy-template", "template_cut"), ("legacy-retry", "orientation_retry")):
            db.add(UsageEvent(teacher_id=teacher.id, batch_id=batch_id,
                              record_date=date.today(), count=99, source=source))
        db.commit()

        result = limiter.check_daily_image_limit(teacher.id, 400)

        assert result["allowed"] is True
        assert result["current"] == 0
    finally:
        db.close()


def test_usage_stats_expose_daily_image_count_and_reset_time():
    db = _session()
    try:
        teacher = User(id=5, name="老师", role="teacher")
        db.add(teacher)
        db.commit()
        limiter = RateLimiter(db)
        limiter.reserve_student_images(
            teacher.id, 40, batch_id="student-upload-stats"
        )

        stats = limiter.get_usage_stats(teacher.id)

        assert stats["monthly_images"]["current"] == 40
        assert stats["daily_images"]["limit"] == 400
        assert stats["daily_images"]["remaining"] == 360
        assert stats["reset_time"].endswith("00:00:00")
    finally:
        db.close()
