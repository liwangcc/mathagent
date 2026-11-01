"""Offline bank adoption using only synthetic SQLite files; no app/config imports."""
import json
import sqlite3

import pytest

from scripts import adopt_existing_bank_senior as migration
from services.math_curriculum import catalog_version, get_math_catalog


def _rows(path, table="question_bank"):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY id')]


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "synthetic.db"
    module = get_math_catalog("senior")[0]
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TABLE question_bank (
            id INTEGER PRIMARY KEY, subject TEXT, question TEXT, answer TEXT,
            teacher_id INTEGER, is_public INTEGER, chapter TEXT, topic TEXT,
            education_stage TEXT, catalog_version TEXT, knowledge_point_id TEXT,
            source TEXT, classification_status TEXT, created_at TEXT
        )""")
        connection.execute("CREATE TABLE knowledge_events (id INTEGER PRIMARY KEY, education_stage TEXT, detail TEXT)")
        connection.execute("INSERT INTO knowledge_events VALUES (1, '', 'synthetic history must stay unchanged')")
        for row_id, stage, chapter, topic in (
            (1, "", module["chapter"], module["topics"][0]),
            (2, None, "old unmatched chapter", "old unmatched topic"),
            (3, "junior", "junior chapter", "junior topic"),
            (4, "senior", "existing senior chapter", "existing senior topic"),
        ):
            connection.execute("INSERT INTO question_bank VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                row_id, "数学", f"synthetic secret question {row_id}", f"synthetic answer {row_id}",
                None if row_id == 1 else 7, 1 if row_id == 1 else 0, chapter, topic, stage,
                "old-version", "old-point-id", "import", "confirmed", "2026-09-09T00:00:00",
            ))
    return path


def _apply_args(database, tmp_path, plan=None):
    plan = plan or migration.inspect_database(database)
    return dict(through_id=plan["through_id"], expected_count=plan["eligible_count"],
                expected_plan_sha256=plan["plan_sha256"], backup_path=tmp_path / "before.db",
                confirm_senior=True, confirm_offline=True)


def test_default_cli_is_read_only_and_reports_no_question_or_answer(database, capsys):
    before = database.read_bytes()
    files = sorted(p.name for p in database.parent.iterdir())
    assert migration.main(["--db", str(database)]) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["mode"] == "dry-run" and report["eligible_count"] == 2
    assert report["through_id"] == 4
    assert report["exact_knowledge_matches"] == 1
    assert report["unmatched_knowledge_count"] == 1
    assert len(report["plan_sha256"]) == 64
    assert "secret question" not in output and "synthetic answer" not in output
    assert database.read_bytes() == before
    assert sorted(p.name for p in database.parent.iterdir()) == files


def test_apply_changes_only_three_bank_fields_and_preserves_archive(database, tmp_path):
    before = _rows(database)
    archive = _rows(database, "knowledge_events")
    args = _apply_args(database, tmp_path)
    result = migration.apply_database(database, **args)
    assert result["mode"] == "applied" and result["updated"] == 2
    after = _rows(database)
    for old, new in zip(before, after):
        if old["education_stage"] in (None, ""):
            assert new["education_stage"] == "senior"
            assert new["catalog_version"] == catalog_version("senior")
            assert {k: v for k, v in old.items() if k not in migration.CHANGED_FIELDS} == {
                k: v for k, v in new.items() if k not in migration.CHANGED_FIELDS}
        else:
            assert old == new
    module = get_math_catalog("senior")[0]
    assert after[0]["knowledge_point_id"] == module["topic_ids"][module["topics"][0]]
    assert after[1]["knowledge_point_id"] == ""  # No guessed mapping or stale ID.
    assert _rows(database, "knowledge_events") == archive
    assert _rows(args["backup_path"]) == before
    assert _rows(args["backup_path"], "knowledge_events") == archive


def test_reviewed_boundary_excludes_future_legacy_and_exact_replay_is_noop(database, tmp_path):
    args = _apply_args(database, tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO question_bank (id,subject,question,answer,education_stage) VALUES (5,'数学','future','future','')")
    assert migration.apply_database(database, **args)["updated"] == 2
    after = _rows(database)
    assert after[-1]["education_stage"] == ""
    replay = migration.apply_database(database, **args)
    assert replay["mode"] == "already-applied" and replay["updated"] == 0
    assert _rows(database) == after


@pytest.mark.parametrize("field", ["question", "answer", "teacher_id", "chapter"])
def test_edit_after_preview_invalidates_plan_without_creating_backup(database, tmp_path, field):
    args = _apply_args(database, tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(f"UPDATE question_bank SET {field}=? WHERE id=1", (99 if field == "teacher_id" else "changed",))
    before = _rows(database)
    with pytest.raises(ValueError, match="dry-run 后变化"):
        migration.apply_database(database, **args)
    assert _rows(database) == before
    assert not args["backup_path"].exists()


@pytest.mark.parametrize("confirmation", ["confirm_senior", "confirm_offline"])
def test_apply_requires_each_confirmation(database, tmp_path, confirmation):
    args = _apply_args(database, tmp_path)
    args[confirmation] = False
    with pytest.raises(ValueError, match="明确确认"):
        migration.apply_database(database, **args)
    assert not args["backup_path"].exists()


@pytest.mark.parametrize("field,value", [("expected_count", 99), ("expected_plan_sha256", "f" * 64), ("through_id", 1)])
def test_apply_requires_exact_reviewed_plan(database, tmp_path, field, value):
    args = _apply_args(database, tmp_path)
    args[field] = value
    before = _rows(database)
    with pytest.raises(ValueError):
        migration.apply_database(database, **args)
    assert _rows(database) == before and not args["backup_path"].exists()


def test_existing_backup_never_overwritten(database, tmp_path):
    args = _apply_args(database, tmp_path)
    args["backup_path"].write_bytes(b"existing unrelated backup")
    with pytest.raises(ValueError, match="拒绝覆盖"):
        migration.apply_database(database, **args)
    assert args["backup_path"].read_bytes() == b"existing unrelated backup"


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_backup_cannot_replace_database_or_sidecars(database, tmp_path, suffix):
    args = _apply_args(database, tmp_path)
    args["backup_path"] = str(database) + suffix
    before = database.read_bytes()
    with pytest.raises(ValueError, match="不能覆盖"):
        migration.apply_database(database, **args)
    assert database.read_bytes() == before


def test_backup_failure_aborts_before_any_update(database, tmp_path, monkeypatch):
    args = _apply_args(database, tmp_path)
    before = _rows(database)
    def fail(*_args):
        raise OSError("synthetic backup error")
    monkeypatch.setattr(migration, "_backup_database", fail)
    with pytest.raises(OSError, match="synthetic"):
        migration.apply_database(database, **args)
    assert _rows(database) == before


@pytest.mark.parametrize("failure", ["write_receipt", "fsync"])
def test_receipt_failure_leaves_original_database_unchanged(database, tmp_path, monkeypatch, failure):
    args = _apply_args(database, tmp_path)
    before = _rows(database)
    def fail(*_args):
        raise OSError("synthetic receipt persistence error")
    if failure == "write_receipt":
        monkeypatch.setattr(migration, "_write_receipt", fail)
    else:
        monkeypatch.setattr(migration.os, "fsync", fail)
    with pytest.raises(OSError, match="receipt persistence"):
        migration.apply_database(database, **args)
    assert _rows(database) == before
    assert _rows(args["backup_path"]) == before


def test_new_candidate_inside_reviewed_boundary_invalidates_plan(database, tmp_path):
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO question_bank (id,subject,education_stage) VALUES (8,'数学','senior')")
    args = _apply_args(database, tmp_path)
    assert args["through_id"] == 8
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO question_bank (id,subject,question,answer,education_stage) VALUES (5,'数学','new inside boundary','answer','')")
    before = _rows(database)
    with pytest.raises(ValueError, match="dry-run 后变化"):
        migration.apply_database(database, **args)
    assert _rows(database) == before
    assert not args["backup_path"].exists()


def test_second_update_failure_rolls_back_first_and_keeps_verified_backup(database, tmp_path, monkeypatch):
    args = _apply_args(database, tmp_path)
    before = _rows(database)
    original_connect = migration._connect
    class FailingConnection(sqlite3.Connection):
        updates = 0
        def execute(self, query, parameters=()):
            if query.startswith("UPDATE question_bank"):
                self.updates += 1
                if self.updates == 2:
                    raise sqlite3.OperationalError("synthetic mid-transaction error")
            return super().execute(query, parameters)
    def connect(path, *, writable=False):
        if not writable:
            return original_connect(path)
        connection = sqlite3.connect(path, factory=FailingConnection)
        connection.row_factory = sqlite3.Row
        return connection
    monkeypatch.setattr(migration, "_connect", connect)
    with pytest.raises(sqlite3.OperationalError, match="mid-transaction"):
        migration.apply_database(database, **args)
    assert _rows(database) == before
    assert _rows(args["backup_path"]) == before
    # A prepared receipt is not accepted as proof that a failed transaction ran.
    with pytest.raises(ValueError, match="不匹配已成功执行"):
        migration.apply_database(database, **args)


def test_replay_verifies_backup_checksum_and_live_rows(database, tmp_path):
    args = _apply_args(database, tmp_path)
    migration.apply_database(database, **args)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE question_bank SET answer='later correction' WHERE id=1")
    before = _rows(database)
    with pytest.raises(ValueError, match="拒绝覆盖"):
        migration.apply_database(database, **args)
    assert _rows(database) == before


def test_replay_rejects_tampered_backup(database, tmp_path):
    args = _apply_args(database, tmp_path)
    migration.apply_database(database, **args)
    with sqlite3.connect(args["backup_path"]) as connection:
        connection.execute("UPDATE question_bank SET answer='changed backup' WHERE id=1")
    before = _rows(database)
    with pytest.raises(ValueError, match="拒绝覆盖"):
        migration.apply_database(database, **args)
    assert _rows(database) == before


def test_catalog_change_after_preview_requires_new_approval(database, tmp_path, monkeypatch):
    args = _apply_args(database, tmp_path)
    monkeypatch.setattr(migration, "catalog_version", lambda stage: "synthetic-new-catalog")
    with pytest.raises(ValueError, match="目录已在 dry-run 后变化"):
        migration.apply_database(database, **args)
    assert not args["backup_path"].exists()


def test_zero_eligible_rows_is_noop_without_backup(database, tmp_path):
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE question_bank SET education_stage='senior'")
    args = _apply_args(database, tmp_path)
    assert args["expected_count"] == 0
    assert migration.apply_database(database, **args)["mode"] == "no-op"
    assert not args["backup_path"].exists()


def test_missing_schema_nonmath_and_triggers_fail_closed(database, tmp_path):
    empty = tmp_path / "empty.db"
    with sqlite3.connect(empty) as connection:
        connection.execute("CREATE TABLE untouched (id INTEGER)")
    with pytest.raises(ValueError, match="字段不完整"):
        migration.inspect_database(empty)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE question_bank SET subject='英语' WHERE id=1")
    with pytest.raises(ValueError, match="非数学题"):
        migration.inspect_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE question_bank SET subject='数学' WHERE id=1")
        connection.execute("CREATE TRIGGER side_effect AFTER UPDATE ON question_bank BEGIN UPDATE knowledge_events SET detail='changed'; END")
    with pytest.raises(ValueError, match="触发器"):
        migration.inspect_database(database)


def test_wal_backup_is_consistent_before_atomic_adoption(database, tmp_path):
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("UPDATE question_bank SET answer='committed WAL answer' WHERE id=1")
        writer.commit()
        before = _rows(database)
        args = _apply_args(database, tmp_path)
        migration.apply_database(database, **args)
        assert _rows(args["backup_path"]) == before
        assert _rows(database)[0]["answer"] == "committed WAL answer"
    finally:
        writer.close()


def test_cli_requires_apply_parameters_and_never_guesses_database(database, capsys):
    with pytest.raises(SystemExit):
        migration.main([])
    assert migration.main(["--db", str(database), "--apply"]) == 2
    assert "--backup" in capsys.readouterr().err
    assert migration.main(["--db", str(database), "--backup", "unused.db"]) == 2
    assert "默认只读" in capsys.readouterr().err
