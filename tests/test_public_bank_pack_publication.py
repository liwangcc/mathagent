"""Hermetic operator publication, real SQLite/ORM schema, no business data."""
import copy
import json
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy import create_engine
from database import Base
import models_user  # noqa: F401
from models import QuestionBank
from scripts import import_public_bank_pack as publisher
from services.math_curriculum import get_math_catalog


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "isolated.db"
    engine = create_engine("sqlite:///" + path.as_posix())
    Base.metadata.create_all(engine)
    engine.dispose()
    return path


def item(stage="junior", question="甲数与乙数的和为 7，差为 1，求甲数。", answer="4"):
    module = get_math_catalog(stage)[0]
    return {"question": question, "answer": answer, "education_stage": stage, "subject": "数学",
            "chapter": module["chapter"], "topic": module["topics"][0], "question_type": "fill",
            "difficulty": 2, "solution_steps": ["甲数为 $(7+1)/2=4$。"],
            "answer_variants": ["$4$"], "grading_method": "exact"}


def prepare(tmp_path, items, name="pack.json", stage="junior"):
    path = tmp_path / name
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    sha = publisher.file_digest(path)
    prepared = publisher.load_pack(path, stage=stage, expected_count=len(items),
                                   expected_sha256=sha, source="test_pack")
    return prepared, sha


def apply(database, tmp_path, prepared, sha, suffix="first"):
    plan = publisher.inspect_pack(database, prepared, sha)
    receipt = tmp_path / (suffix + ".json")
    backup = tmp_path / (suffix + ".db")
    result = publisher.apply_pack(database, prepared, pack_sha256=sha, expected_plan_sha256=plan["plan_sha256"],
                                  backup=backup, receipt=receipt)
    return result, receipt, backup


def test_public_insert_preserves_every_payload_field_and_empty_analysis(database, tmp_path):
    second = item(question="计算 $5-3$。", answer="2")
    second["solution_steps"] = []
    prepared, sha = prepare(tmp_path, [item(), second])
    plan = publisher.inspect_pack(database, prepared, sha)
    assert plan["insert_count"] == 2 and plan["duplicate_count"] == 0
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM question_bank").fetchone()[0] == 0
    result, receipt, backup = apply(database, tmp_path, prepared, sha)
    assert result["all_inserted_fields_verified"] and result["empty_solution_steps_count"] == 1
    assert publisher.verify_receipt(database, prepared, receipt)["inserted_verified"] == 2
    with sqlite3.connect(backup) as connection:
        assert connection.execute("SELECT COUNT(*) FROM question_bank").fetchone()[0] == 0
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
    again = publisher.inspect_pack(database, prepared, sha)
    assert (again["insert_count"], again["duplicate_count"]) == (0, 2)
    assert result["backup_sha256"] == publisher.file_digest(backup)


@pytest.mark.parametrize("stage", ["primary", "junior", "senior"])
def test_stage_ids_and_teacher_visible_filter(database, tmp_path, stage):
    from sqlalchemy.orm import Session
    from services.bank_scope import visible_filter
    from services.teaching_context import make_teaching_context, use_teaching_context
    prepared, sha = prepare(tmp_path, [item(stage)], stage=stage)
    result, receipt, _ = apply(database, tmp_path, prepared, sha)
    engine = create_engine("sqlite:///" + database.as_posix())
    try:
        for other in ("primary", "junior", "senior"):
            with Session(engine) as session, use_teaching_context(make_teaching_context(other)):
                rows = session.query(QuestionBank).filter(visible_filter(99999)).all()
                assert len(rows) == int(other == stage)
                if rows:
                    assert rows[0].get_solution_steps() == item(stage)["solution_steps"]
                    assert rows[0].get_variants() == item(stage)["answer_variants"]
                    assert rows[0].knowledge_point_id == prepared[0]["knowledge_point_id"]
    finally:
        engine.dispose()
    assert publisher.verify_receipt(database, prepared, receipt)["verified"]


def test_equivalent_rubric_roundtrips_without_downgrading_to_exact(database, tmp_path):
    """Current mathematical-equivalence rubrics are existing domain values."""
    question = item()
    question["question_type"] = "solution"
    question["grading_method"] = "equivalent"
    prepared, sha = prepare(tmp_path, [question])
    result, receipt, _ = apply(database, tmp_path, prepared, sha)
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT grading_method,question_type FROM question_bank WHERE id=?",
            (result["inserted"][0]["question_id"],),
        ).fetchone()
    assert row == ("equivalent", "solution")
    assert publisher.verify_receipt(database, prepared, receipt)["verified"]


def test_math_signs_and_parameters_are_not_fuzzy_deduplicated(database, tmp_path):
    items = [item(question="计算 $x-1$ 在 $x=2$ 时的值。", answer="1"),
             item(question="计算 $x+1$ 在 $x=2$ 时的值。", answer="3")]
    prepared, sha = prepare(tmp_path, items)
    result, _, _ = apply(database, tmp_path, prepared, sha)
    assert len(result["inserted"]) == 2


def test_cross_stage_and_private_question_are_not_overwritten(database, tmp_path):
    prepared, sha = prepare(tmp_path, [item()])
    result, _, _ = apply(database, tmp_path, prepared, sha)
    existing_id = result["inserted"][0]["question_id"]
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE question_bank SET is_public=0 WHERE id=?", (existing_id,))
    senior, senior_sha = prepare(tmp_path, [item("senior")], name="senior-pack.json", stage="senior")
    apply(database, tmp_path, senior, senior_sha, suffix="senior")
    plan = publisher.inspect_pack(database, prepared, sha)
    assert plan["insert_count"] == 1 and plan["duplicate_count"] == 0
    apply(database, tmp_path, prepared, sha, suffix="junior")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT is_public FROM question_bank WHERE id=?", (existing_id,)).fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM question_bank").fetchone() == (3,)


def test_answer_conflict_rejects_entire_batch_without_backup(database, tmp_path):
    prepared, sha = prepare(tmp_path, [item()])
    apply(database, tmp_path, prepared, sha)
    changed, changed_sha = prepare(tmp_path, [item(answer="5"), item(question="求 $1+1$。", answer="2")], name="changed.json")
    plan = publisher.inspect_pack(database, changed, changed_sha)
    assert plan["conflicts"] and plan["insert_count"] == 1
    backup, receipt = tmp_path / "conflict.db", tmp_path / "conflict.json"
    with pytest.raises(ValueError, match="conflict"):
        publisher.apply_pack(database, changed, pack_sha256=changed_sha, expected_plan_sha256=plan["plan_sha256"],
                             backup=backup, receipt=receipt)
    assert not backup.exists() and not receipt.exists()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM question_bank").fetchone() == (1,)


def test_stale_plan_rejected_before_backup(database, tmp_path):
    prepared, sha = prepare(tmp_path, [item()])
    old = publisher.inspect_pack(database, prepared, sha)
    apply(database, tmp_path, prepared, sha)
    with pytest.raises(ValueError, match="plan changed"):
        publisher.apply_pack(database, prepared, pack_sha256=sha, expected_plan_sha256=old["plan_sha256"],
                             backup=tmp_path / "stale.db", receipt=tmp_path / "stale.json")
    assert not (tmp_path / "stale.db").exists()


def test_duplicate_receipt_paths_never_overwrite(database, tmp_path):
    prepared, sha = prepare(tmp_path, [item()])
    _, receipt, backup = apply(database, tmp_path, prepared, sha)
    sha_before = publisher.file_digest(backup)
    with pytest.raises(ValueError, match="already exists"):
        publisher.apply_pack(database, prepared, pack_sha256=sha, expected_plan_sha256="invalid",
                             backup=backup, receipt=receipt)
    assert publisher.file_digest(backup) == sha_before


@pytest.mark.parametrize("field,value", [
    ("education_stage", "senior"), ("subject", "语文"), ("difficulty", True), ("difficulty", 0),
    ("difficulty", 6), ("question_type", "unknown"), ("topic", "不存在"),
    ("grading_method", "process"), ("solution_steps", "伪数组"), ("answer_variants", [1]),
    ("answer", ""), ("question", ""),
])
def test_invalid_batch_rejected_before_database_access(tmp_path, field, value):
    invalid = item()
    invalid[field] = value
    with pytest.raises(ValueError):
        prepare(tmp_path, [item(), invalid])


def test_choice_letters_and_all_options_required(tmp_path):
    valid = item(question="1+1=？\nA. 1 B. 2 C. 3 D. 4", answer="B")
    valid["question_type"] = "choice"
    prepare(tmp_path, [valid])
    invalid = copy.deepcopy(valid)
    invalid["answer"] = "2"
    with pytest.raises(ValueError, match="letter set"):
        prepare(tmp_path, [invalid])
    invalid = copy.deepcopy(valid)
    invalid["question"] = "1+1=？ A. 1 B. 2 C. 3"
    with pytest.raises(ValueError, match="options missing"):
        prepare(tmp_path, [invalid])


def test_sha_and_count_must_match(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps([item()]), encoding="utf-8")
    kwargs = dict(stage="junior", source="test_pack", expected_count=1)
    with pytest.raises(ValueError, match="SHA-256"):
        publisher.load_pack(path, expected_sha256="0" * 64, **kwargs)
    kwargs["expected_count"] = 2
    with pytest.raises(ValueError, match="count mismatch"):
        publisher.load_pack(path, expected_sha256=publisher.file_digest(path), **kwargs)


def test_trigger_rejected_without_app_migration(database, tmp_path):
    prepared, sha = prepare(tmp_path, [item()])
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TRIGGER unreviewed AFTER INSERT ON question_bank BEGIN SELECT 1; END")
    with pytest.raises(ValueError, match="triggers"):
        publisher.inspect_pack(database, prepared, sha)


def test_source_tag_validation(tmp_path):
    path = tmp_path / "source.json"
    path.write_text(json.dumps([item()]), encoding="utf-8")
    with pytest.raises(ValueError, match="source"):
        publisher.load_pack(path, stage="junior", expected_count=1,
                            expected_sha256=publisher.file_digest(path), source="x'; DROP TABLE question_bank")
