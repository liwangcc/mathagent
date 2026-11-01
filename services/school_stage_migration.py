"""Additive school-stage migration. Caller owns the transaction/connection."""

STAGE_TABLES = (
    "question_bank", "grading_results", "student_profiles", "knowledge_events",
    "exam_scores", "weaknesses", "parent_letter_shares",
)


def migrate_school_stage_schema(connection) -> None:
    """Idempotent on both new/old SQLite DBs; never classifies existing rows."""
    cursor = connection.cursor()
    tables = {row[0] for row in cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    for table in ("users", *STAGE_TABLES):
        if table not in tables:
            continue
        # Identifiers come exclusively from the constant allowlist above.
        columns = {row[1] for row in cursor.execute(f'PRAGMA table_info("{table}")')}
        if "education_stage" not in columns:
            cursor.execute(
                f'ALTER TABLE "{table}" ADD COLUMN education_stage VARCHAR(16) NOT NULL DEFAULT \'\''
            )
        if table in STAGE_TABLES and "catalog_version" not in columns:
            cursor.execute(
                f'ALTER TABLE "{table}" ADD COLUMN catalog_version VARCHAR(96) NOT NULL DEFAULT \'\''
            )
        if table in {"question_bank", "knowledge_events"} and "knowledge_point_id" not in columns:
            cursor.execute(
                f'ALTER TABLE "{table}" ADD COLUMN knowledge_point_id VARCHAR(96) NOT NULL DEFAULT \'\''
            )
        cursor.execute(
            f'CREATE INDEX IF NOT EXISTS "ix_{table}_education_stage" ON "{table}" (education_stage)'
        )
    cursor.close()
