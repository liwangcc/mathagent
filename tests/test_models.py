"""Tests for models.py — _json_field helper and model methods."""

import json

import database
import models
import pytest
from sqlalchemy import create_engine, inspect
from models import _json_field


class TestJsonField:
    def test_valid_json_object(self):
        assert _json_field('{"a": 1}') == {"a": 1}

    def test_valid_json_array(self):
        assert _json_field('[1, 2, 3]') == [1, 2, 3]

    def test_empty_string(self):
        assert _json_field("") == []

    def test_none(self):
        assert _json_field(None) == []

    def test_invalid_json(self):
        assert _json_field("not json") == []

    def test_empty_array(self):
        assert _json_field("[]") == []

    def test_empty_object(self):
        assert _json_field("{}") == {}

    def test_actually_none_value(self):
        # Simulates a Text column that was never set
        assert _json_field(None) == []


def test_init_db_creates_grading_correction_audit_table_idempotently(monkeypatch, tmp_path):
    """A fresh and an existing SQLite file both receive the append-only audit schema."""
    engine = create_engine(f"sqlite:///{tmp_path / 'corrections.db'}")
    monkeypatch.setattr(database, "engine", engine)

    database.init_db()
    database.init_db()

    schema = inspect(engine)
    assert "grading_corrections" in schema.get_table_names()
    columns = {item["name"] for item in schema.get_columns("grading_corrections")}
    assert {
        "id", "grading_result_id", "teacher_id", "paper_id", "question_no", "audit_key",
        "old_result", "new_result", "note", "created_at",
    } <= columns
    indexes = {item["name"] for item in schema.get_indexes("grading_corrections")}
    assert "ix_grading_corrections_grading" in indexes
    assert "ix_grading_corrections_teacher" in indexes
    assert hasattr(models, "GradingCorrection")
    assert models.GradingCorrection.__tablename__ == "grading_corrections"
