import pytest
from sqlalchemy import create_engine

import database


def test_scoring_migration_marks_existing_rows_v1_and_adds_nullable_metrics(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE grading_results ("
            "id INTEGER PRIMARY KEY, student_id INTEGER, score FLOAT, "
            "needs_review_count INTEGER DEFAULT 0, category VARCHAR(20) DEFAULT 'daily', "
            "exam_name VARCHAR(100) DEFAULT '', image_content_hash VARCHAR(64) DEFAULT '', "
            "ocr_raw_name VARCHAR(100) DEFAULT '', ocr_confidence FLOAT DEFAULT 0, "
            "ocr_raw_json TEXT DEFAULT '{}', deleted_at DATETIME)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE student_profiles ("
            "id INTEGER PRIMARY KEY, score FLOAT, needs_review_count INTEGER DEFAULT 0, "
            "image_content_hash VARCHAR(64) DEFAULT '')"
        )
        conn.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY, password_hash VARCHAR(128), current_class_id INTEGER)")
        conn.exec_driver_sql(
            "CREATE TABLE students (id INTEGER PRIMARY KEY, teacher_id INTEGER, class_id INTEGER, "
            "name VARCHAR(50) NOT NULL, student_no VARCHAR(50), class_name VARCHAR(50), "
            "enrolled DATE, created_at DATETIME, phone VARCHAR(20))"
        )
        conn.exec_driver_sql("CREATE TABLE teacher_classes (id INTEGER PRIMARY KEY, deleted_at DATETIME)")
        conn.exec_driver_sql("CREATE TABLE question_bank (id INTEGER PRIMARY KEY, teacher_id INTEGER, is_public BOOLEAN DEFAULT 0)")
        conn.exec_driver_sql("INSERT INTO grading_results (id, student_id, score) VALUES (1, 1, 0.8)")
        conn.exec_driver_sql("INSERT INTO student_profiles (id, score) VALUES (1, 0.8)")

    monkeypatch.setattr(database, "engine", engine)
    database._migrate()

    with engine.connect() as conn:
        gr = conn.exec_driver_sql(
            "SELECT score, max_score, correct_rate, scoring_version, grading_contract_hash FROM grading_results WHERE id = 1"
        ).one()
        profile = conn.exec_driver_sql(
            "SELECT score, max_score, correct_rate, scoring_version FROM student_profiles WHERE id = 1"
        ).one()
        gr_columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(grading_results)")}
        profile_columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(student_profiles)")}
        index_names = {row[1] for row in conn.exec_driver_sql("PRAGMA index_list(grading_results)")}
        index_columns = [
            row[2]
            for row in conn.exec_driver_sql("PRAGMA index_info(uq_grading_student_image_category_exam_contract)")
        ]

    assert tuple(gr) == (0.8, None, None, 1, "")
    assert tuple(profile) == (0.8, None, None, 1)
    assert {"max_score", "correct_rate", "scoring_version", "grading_contract_hash"} <= gr_columns
    assert {"max_score", "correct_rate", "scoring_version"} <= profile_columns
    assert "uq_grading_student_image_category_exam_contract" in index_names
    assert index_columns == ["student_id", "image_content_hash", "category", "exam_name", "grading_contract_hash"]


def test_scoring_migration_refuses_to_start_without_contract_idempotency_index(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'duplicate-contract.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE grading_results ("
            "id INTEGER PRIMARY KEY, student_id INTEGER, score FLOAT, "
            "needs_review_count INTEGER DEFAULT 0, category VARCHAR(20) DEFAULT 'daily', "
            "exam_name VARCHAR(100) DEFAULT '', image_content_hash VARCHAR(64) DEFAULT '', "
            "ocr_raw_name VARCHAR(100) DEFAULT '', ocr_confidence FLOAT DEFAULT 0, "
            "ocr_raw_json TEXT DEFAULT '{}', deleted_at DATETIME)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE student_profiles ("
            "id INTEGER PRIMARY KEY, score FLOAT, needs_review_count INTEGER DEFAULT 0, "
            "image_content_hash VARCHAR(64) DEFAULT '')"
        )
        conn.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY, password_hash VARCHAR(128), current_class_id INTEGER)")
        conn.exec_driver_sql(
            "CREATE TABLE students (id INTEGER PRIMARY KEY, teacher_id INTEGER, class_id INTEGER, "
            "name VARCHAR(50) NOT NULL, student_no VARCHAR(50), class_name VARCHAR(50), "
            "enrolled DATE, created_at DATETIME, phone VARCHAR(20))"
        )
        conn.exec_driver_sql("CREATE TABLE teacher_classes (id INTEGER PRIMARY KEY, deleted_at DATETIME)")
        conn.exec_driver_sql("CREATE TABLE question_bank (id INTEGER PRIMARY KEY, teacher_id INTEGER, is_public BOOLEAN DEFAULT 0)")
        conn.exec_driver_sql(
            "INSERT INTO grading_results (id, student_id, score, image_content_hash) "
            "VALUES (1, 1, 0.8, 'same-image'), (2, 1, 0.9, 'same-image')"
        )

    monkeypatch.setattr(database, "engine", engine)
    with pytest.raises(RuntimeError, match="grading idempotency"):
        database._migrate()

    with engine.connect() as conn:
        index_names = {row[1] for row in conn.exec_driver_sql("PRAGMA index_list(grading_results)")}
    assert "uq_grading_student_image_category_exam_contract" not in index_names
