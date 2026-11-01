"""Schema-only migrations on synthetic in-memory databases."""
import sqlite3

import pytest

from services.school_stage_migration import STAGE_TABLES, migrate_school_stage_schema


@pytest.mark.parametrize("existing", [False, True])
def test_additive_and_idempotent_without_guessing_old_records(existing):
    with sqlite3.connect(":memory:") as db:
        tables = ("users", *STAGE_TABLES)
        for table in tables:
            suffix = ", education_stage VARCHAR(16) NOT NULL DEFAULT ''" if existing else ""
            db.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, label TEXT{suffix})')
            db.execute(f'INSERT INTO "{table}" (label) VALUES (?)', ("synthetic legacy",))
        for _ in range(2):
            migrate_school_stage_schema(db)
        for table in tables:
            row = db.execute(f'SELECT label, education_stage FROM "{table}"').fetchone()
            assert row == ("synthetic legacy", "")
            fields = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
            if table in STAGE_TABLES:
                assert "catalog_version" in fields
            if table in {"question_bank", "knowledge_events"}:
                assert "knowledge_point_id" in fields


def test_empty_database_is_a_safe_noop():
    with sqlite3.connect(":memory:") as db:
        migrate_school_stage_schema(db)
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []


def test_current_model_schema_accepts_both_stages_without_rewriting_legacy():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from database import Base
    import models
    import models_user
    import models_correction
    import models_rate_limit

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        migrate_school_stage_schema(conn.connection)
    with Session(engine) as db:
        for stage in ("", "junior", "senior"):
            db.add(models.QuestionBank(question="synthetic", answer="2", education_stage=stage))
        db.commit()
        assert sorted(row.education_stage for row in db.query(models.QuestionBank)) == ["", "junior", "senior"]
    engine.dispose()
