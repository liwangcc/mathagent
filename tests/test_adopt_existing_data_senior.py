"""Eight-table adoption uses isolated synthetic SQLite only; never app data."""
import json
import sqlite3

import pytest

from scripts import adopt_existing_data_senior as migration
from services.math_curriculum import catalog_version, get_math_catalog


def _rows(path, table):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY id')]


def _snapshot(path):
    return {table: _rows(path, table) for table in (*migration.TABLES, "users", "unrelated")}


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "synthetic.db"
    module = get_math_catalog("senior")[0]
    with sqlite3.connect(path) as connection:
        for table in migration.TABLES:
            required = migration.REQUIRED_FIELDS[table] - {"id"}
            extras = {"created_at", "updated_at", "sensitive_payload", "grading_contract_hash",
                      "grading_details", "score", "analytics_included", "image_path"}
            if table in ("question_bank", "student_profiles", "knowledge_events"):
                extras |= {field for target, field, _ in migration.SOURCE_LINKS if target == table}
            fields = sorted(required | extras)
            numeric = {"student_id", "teacher_id", "is_public", "score", "analytics_included",
                       "source_grading_id", "grading_result_id", "question_bank_id"}
            columns = ", ".join(f'"{field}" {"INTEGER" if field in numeric else "TEXT"}' for field in fields)
            connection.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, {columns})')
            for row_id, stage in ((1, ""), (2, None), (3, "junior"), (4, "senior")):
                row = dict.fromkeys(fields, "preserved")
                row.update(id=row_id, sensitive_payload=f"synthetic secret {table} {row_id}",
                           grading_contract_hash="frozen-confirmed-contract", grading_details='[{"education_stage":"","answer":"secret answer"}]',
                           score=63, analytics_included=0, image_path="synthetic/never-read.jpg")
                for field in fields:
                    if field in numeric:
                        row[field] = 7
                row.update(score=63, analytics_included=0)
                for target, field, _ in migration.SOURCE_LINKS:
                    if target == table:
                        row[field] = row_id
                if table in migration.STAGE_TABLES:
                    row.update(education_stage=stage, catalog_version="old-catalog")
                for field in migration.ARCHIVE_JSON_FIELDS.get(table, ()):
                    row[field] = ('[{"education_stage":"","answer":"secret answer"}]'
                                  if field != "ocr_raw_json" else '{"questions":[]}')
                if table in migration.POINT_TABLES:
                    row.update(chapter=module["chapter"] if row_id == 1 else "unmatched",
                               topic=module["topics"][0] if row_id == 1 else "unmatched", knowledge_point_id="old-id")
                if table == "question_bank":
                    row.update(subject="数学", question="synthetic question", answer="synthetic answer")
                if table == "weakness_overrides":
                    row["topic_key"] = f"chapter{row_id}||topic" if not stage else f"{stage}||chapter{row_id}||topic"
                keys = list(row)
                connection.execute(f'INSERT INTO "{table}" (' + ",".join(keys) + ") VALUES ("
                                   + ",".join("?" for _ in keys) + ")", [row[key] for key in keys])
        connection.execute("CREATE UNIQUE INDEX synthetic_override_unique ON weakness_overrides(student_id,topic_key)")
        connection.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, education_stage TEXT, secret TEXT)")
        connection.execute("INSERT INTO users VALUES (7,'','teacher profile must not be filled')")
        connection.execute("CREATE TABLE unrelated(id INTEGER PRIMARY KEY, payload TEXT)")
        connection.execute("INSERT INTO unrelated VALUES (1,'keep unchanged')")
    return path


def _args(database, tmp_path, plan=None):
    plan = plan or migration.inspect_database(database)
    return dict(through_ids=plan["through_ids"], expected_counts=plan["eligible_counts"],
                expected_plan_sha256=plan["plan_sha256"], backup_path=tmp_path / "before.db",
                confirm_senior=True, confirm_offline=True)


def test_dryrun_is_read_only_metadata_only_and_has_fixed_eight_tables(database, capsys):
    before = database.read_bytes()
    files = sorted(path.name for path in database.parent.iterdir())
    assert migration.main(["--db", str(database)]) == 0
    text = capsys.readouterr().out
    plan = json.loads(text)
    assert plan["mode"] == "dry-run"
    assert plan["through_ids"] == dict.fromkeys(migration.TABLES, 4)
    assert plan["eligible_counts"] == dict.fromkeys(migration.TABLES, 2)
    assert plan["tables"]["knowledge_events"]["exact_knowledge_matches"] == 1
    assert plan["tables"]["question_bank"]["unmatched_knowledge_count"] == 1
    assert len(plan["plan_sha256"]) == 64
    assert all(word not in text for word in ("secret", "synthetic question", "image_path", "frozen-confirmed"))
    assert database.read_bytes() == before
    assert sorted(path.name for path in database.parent.iterdir()) == files


def test_apply_preserves_all_nonstage_fields_user_profiles_contracts_and_scores(database, tmp_path):
    before = _snapshot(database)
    args = _args(database, tmp_path)
    result = migration.apply_database(database, **args)
    assert result["mode"] == "applied" and result["updated"] == 16
    after = _snapshot(database)
    for table in migration.TABLES:
        for old, new in zip(before[table], after[table]):
            if old["id"] > 2:
                assert old == new
                continue
            fields = migration.CHANGED_FIELDS[table]
            assert {k: v for k, v in old.items() if k not in fields} == {k: v for k, v in new.items() if k not in fields}
            if table == "weakness_overrides":
                assert new["topic_key"] == "senior||" + old["topic_key"]
            else:
                assert new["education_stage"] == "senior"
                assert new["catalog_version"] == catalog_version("senior")
                if table in migration.POINT_TABLES:
                    assert new["knowledge_point_id"].startswith("senior.math.") if old["id"] == 1 else new["knowledge_point_id"] == ""
    assert after["users"] == before["users"] and after["unrelated"] == before["unrelated"]
    assert _snapshot(args["backup_path"]) == before
    receipt = json.loads((tmp_path / "before.db.adoption.json").read_text("utf-8"))
    assert "secret" not in json.dumps(receipt)
    assert receipt["after_sha256"] == migration._digest({table: after[table] for table in migration.TABLES})


def test_exact_replay_noop_excludes_future_rows_and_future_new_candidates(database, tmp_path):
    args = _args(database, tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO exam_scores (id,education_stage) VALUES (5,'')")
        connection.execute("INSERT INTO weakness_overrides (id,student_id,topic_key) VALUES (5,7,'future||topic')")
    assert migration.apply_database(database, **args)["updated"] == 16
    assert _rows(database, "exam_scores")[-1]["education_stage"] == ""
    assert _rows(database, "weakness_overrides")[-1]["topic_key"] == "future||topic"
    after = _snapshot(database)
    replay = migration.apply_database(database, **args)
    assert replay["mode"] == "already-applied" and replay["updated"] == 0
    assert _snapshot(database) == after


@pytest.mark.parametrize("table", migration.TABLES)
def test_any_table_change_after_preview_invalidates_whole_plan(database, tmp_path, table):
    args = _args(database, tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(f'UPDATE "{table}" SET sensitive_payload=? WHERE id=1', ("changed",))
    before = _snapshot(database)
    with pytest.raises(ValueError, match="dry-run 后变化"):
        migration.apply_database(database, **args)
    assert _snapshot(database) == before and not args["backup_path"].exists()


def test_even_existing_labeled_row_changes_fail_closed(database, tmp_path):
    args = _args(database, tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE grading_results SET score=100 WHERE id=3")
    with pytest.raises(ValueError, match="dry-run 后变化"):
        migration.apply_database(database, **args)
    assert not args["backup_path"].exists()


@pytest.mark.parametrize("table,field,target", migration.SOURCE_LINKS)
def test_legacy_candidate_cannot_reference_explicit_junior_source(database, table, field, target):
    with sqlite3.connect(database) as connection:
        connection.execute(f'UPDATE "{table}" SET "{field}"=3 WHERE id=1')
    with pytest.raises(ValueError, match="明确非高中"):
        migration.inspect_database(database)


def test_source_outside_boundary_requires_new_review_and_orphans_are_preserved(database, tmp_path):
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE student_profiles SET grading_result_id=999 WHERE id=2")
    args = _args(database, tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO grading_results (id,education_stage) VALUES (10,'')")
        connection.execute("UPDATE student_profiles SET grading_result_id=10 WHERE id=1")
    with pytest.raises(ValueError, match="边界外"):
        migration.apply_database(database, **args)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE student_profiles SET grading_result_id=1 WHERE id=1")
    migration.apply_database(database, **args)
    assert _rows(database, "student_profiles")[1]["grading_result_id"] == 999


@pytest.mark.parametrize("before_preview", [True, False])
def test_override_collision_fails_closed_including_target_outside_boundary(database, tmp_path, before_preview):
    args = None if before_preview else _args(database, tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO weakness_overrides(id,student_id,topic_key) VALUES (10,7,'senior||chapter1||topic')")
    before = _snapshot(database)
    with pytest.raises(ValueError, match="目标键冲突"):
        if args is None:
            migration.inspect_database(database)
        else:
            migration.apply_database(database, **args)
    assert _snapshot(database) == before and not (tmp_path / "before.db").exists()


@pytest.mark.parametrize("table", migration.TABLES)
def test_new_row_within_fixed_boundary_invalidates_plan(database, tmp_path, table):
    with sqlite3.connect(database) as connection:
        connection.execute(f'UPDATE "{table}" SET id=8 WHERE id=4')
    args = _args(database, tmp_path)
    with sqlite3.connect(database) as connection:
        if table == "weakness_overrides":
            connection.execute("INSERT INTO weakness_overrides (id,student_id,topic_key) VALUES (5,7,'new||topic')")
        else:
            connection.execute(f'INSERT INTO "{table}" (id,education_stage) VALUES (5,\'\')')
    with pytest.raises(ValueError, match="dry-run 后变化"):
        migration.apply_database(database, **args)
    assert not args["backup_path"].exists()


@pytest.mark.parametrize("table", migration.TABLES)
def test_schema_and_trigger_on_each_table_fail_closed(database, table):
    with sqlite3.connect(database) as connection:
        connection.execute(f'CREATE TRIGGER synthetic_side_effect AFTER UPDATE ON "{table}" BEGIN UPDATE unrelated SET payload=\'changed\'; END')
    with pytest.raises(ValueError, match="触发器"):
        migration.inspect_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER synthetic_side_effect")
        connection.execute(f'ALTER TABLE "{table}" RENAME TO moved')
    with pytest.raises(ValueError, match="字段不完整"):
        migration.inspect_database(database)


@pytest.mark.parametrize("confirmation", ["confirm_senior", "confirm_offline"])
def test_apply_requires_confirmation(database, tmp_path, confirmation):
    args = _args(database, tmp_path)
    args[confirmation] = False
    with pytest.raises(ValueError, match="明确确认"):
        migration.apply_database(database, **args)
    assert not args["backup_path"].exists()


@pytest.mark.parametrize("field,change", [
    ("through_ids", lambda value: {**value, "question_bank": True}),
    ("through_ids", lambda value: {**value, "question_bank": -1}),
    ("through_ids", lambda value: {**value, "users": 1}),
    ("expected_counts", lambda value: {key: item for key, item in value.items() if key != "exam_scores"}),
    ("expected_counts", lambda value: {**value, "exam_scores": 100}),
    ("expected_plan_sha256", lambda value: "f" * 64),
])
def test_incomplete_or_changed_approval_refused(database, tmp_path, field, change):
    args = _args(database, tmp_path)
    args[field] = change(args[field])
    before = _snapshot(database)
    with pytest.raises(ValueError):
        migration.apply_database(database, **args)
    assert _snapshot(database) == before and not args["backup_path"].exists()


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_backup_protects_database_and_sidecars(database, tmp_path, suffix):
    args = _args(database, tmp_path)
    args["backup_path"] = str(database) + suffix
    before = _snapshot(database)
    with pytest.raises(ValueError, match="不能覆盖"):
        migration.apply_database(database, **args)
    assert _snapshot(database) == before


def test_existing_backup_is_not_overwritten(database, tmp_path):
    args = _args(database, tmp_path)
    args["backup_path"].write_bytes(b"unrelated existing backup")
    with pytest.raises(ValueError, match="拒绝覆盖"):
        migration.apply_database(database, **args)
    assert args["backup_path"].read_bytes() == b"unrelated existing backup"


@pytest.mark.parametrize("helper", ["_backup_database", "_write_receipt"])
def test_prewrite_failure_preserves_all_tables(database, tmp_path, monkeypatch, helper):
    args = _args(database, tmp_path)
    before = _snapshot(database)
    def fail(*args):
        raise OSError("synthetic persistence failure")
    monkeypatch.setattr(migration, helper, fail)
    with pytest.raises(OSError, match="persistence"):
        migration.apply_database(database, **args)
    assert _snapshot(database) == before


@pytest.mark.parametrize("last_table", ["exam_scores", "weakness_overrides"])
def test_failure_in_later_table_rolls_back_every_preceding_table(database, tmp_path, monkeypatch, last_table):
    args = _args(database, tmp_path)
    before = _snapshot(database)
    original = migration._update_row
    def update(connection, table, row):
        if table == last_table:
            raise sqlite3.OperationalError("synthetic late failure")
        return original(connection, table, row)
    monkeypatch.setattr(migration, "_update_row", update)
    with pytest.raises(sqlite3.OperationalError, match="late failure"):
        migration.apply_database(database, **args)
    assert _snapshot(database) == before
    assert _snapshot(args["backup_path"]) == before
    with pytest.raises(ValueError, match="不匹配已成功执行"):
        migration.apply_database(database, **args)


@pytest.mark.parametrize("tamper", ["live", "backup", "receipt", "new_inside_boundary"])
def test_replay_verifies_all_tables_receipt_and_backup(database, tmp_path, tamper):
    args = _args(database, tmp_path)
    migration.apply_database(database, **args)
    if tamper in ("live", "backup"):
        with sqlite3.connect(database if tamper == "live" else args["backup_path"]) as connection:
            connection.execute("UPDATE student_profiles SET score=0 WHERE id=1")
    elif tamper == "receipt":
        receipt_path = tmp_path / "before.db.adoption.json"
        receipt = json.loads(receipt_path.read_text("utf-8"))
        receipt["eligible_counts"]["exam_scores"] = 100
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    else:
        with sqlite3.connect(database) as connection:
            connection.execute("DELETE FROM exam_scores WHERE id=4")
            connection.execute("INSERT INTO exam_scores (id,education_stage) VALUES (4,'')")
    before = _snapshot(database)
    with pytest.raises(ValueError, match="拒绝覆盖"):
        migration.apply_database(database, **args)
    assert _snapshot(database) == before


def test_catalog_changed_after_dryrun_requires_reapproval(database, tmp_path, monkeypatch):
    args = _args(database, tmp_path)
    monkeypatch.setattr(migration, "catalog_version", lambda stage: "new-catalog")
    with pytest.raises(ValueError, match="目录已在 dry-run 后变化"):
        migration.apply_database(database, **args)
    assert not args["backup_path"].exists()


def test_wal_backup_consistent_and_noop_has_no_new_backup(database, tmp_path):
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("UPDATE student_profiles SET score=88 WHERE id=1")
        writer.commit()
        before = _snapshot(database)
        args = _args(database, tmp_path)
        migration.apply_database(database, **args)
        assert _snapshot(args["backup_path"]) == before
        fresh = _args(database, tmp_path)
        fresh["backup_path"] = tmp_path / "unused.db"
        assert migration.apply_database(database, **fresh)["mode"] == "no-op"
        assert not fresh["backup_path"].exists()
    finally:
        writer.close()


def test_nonmath_and_malformed_override_are_not_guessed(database):
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE question_bank SET subject='英语' WHERE id=1")
    with pytest.raises(ValueError, match="非数学题"):
        migration.inspect_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE question_bank SET subject='数学' WHERE id=1")
        connection.execute("UPDATE weakness_overrides SET topic_key='malformed' WHERE id=1")
    with pytest.raises(ValueError, match="无法解释"):
        migration.inspect_database(database)


def test_cli_never_guesses_db_rejects_duplicate_keys_and_requires_all_flags(database, capsys, tmp_path):
    with pytest.raises(SystemExit):
        migration.main([])
    with pytest.raises(SystemExit):
        migration.main(["--db", str(database), "--through-ids", '{"question_bank":1,"question_bank":2}'])
    assert migration.main(["--db", str(database), "--apply"]) == 2
    assert migration.main(["--db", str(database), "--backup", "unused.db"]) == 2
    plan = migration.inspect_database(database)
    assert migration.main(["--db", str(database), "--apply", "--through-ids", json.dumps(plan["through_ids"]),
                           "--expected-counts", json.dumps(plan["eligible_counts"]), "--plan-sha256", plan["plan_sha256"],
                           "--backup", str(tmp_path / "cli-before.db"), "--confirm-existing-data-is-senior", "--confirm-offline"]) == 0
    assert '"mode": "applied"' in capsys.readouterr().out


@pytest.mark.parametrize("table,field", [
    ("grading_results", "grading_details"), ("grading_results", "ocr_raw_json"), ("student_profiles", "details_json"),
])
@pytest.mark.parametrize("payload", [
    '{"education_stage":"junior"}', '[{"teaching_context":{"education_stage":"junior"}}]',
    '{"questions":[{"education_stage":"junior"}]}', 'invalid-json', '"unexpected primitive"',
    '{"teaching_context":["not-a-context"]}', '{"teaching_context":{"rules_version":"no-stage"}}',
    '{"teaching_context":{"education_stage":"senior","curriculum_version":"old"}}',
    '{"catalog_version":"old-catalog"}',
    '{"education_stage":"junior","education_stage":""}', '{"question":NaN}',
])
def test_conflicting_or_broken_archive_json_fails_closed_without_logging_data(database, tmp_path, table, field, payload):
    with sqlite3.connect(database) as connection:
        connection.execute(f'UPDATE "{table}" SET "{field}"=? WHERE id=1', (payload,))
    before = _snapshot(database)
    with pytest.raises(ValueError) as error:
        migration.inspect_database(database)
    assert payload not in str(error.value)
    assert _snapshot(database) == before and not (tmp_path / "before.db").exists()


def _add_attempts(database):
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE question_attempt_events(id INTEGER PRIMARY KEY, grading_result_id INTEGER, question_bank_id INTEGER, question_snapshot_json TEXT, question_fingerprint TEXT, student_answer_snapshot_json TEXT)")
        connection.execute("INSERT INTO question_attempt_events VALUES (1,1,1,?, 'frozen fingerprint',?)", (
            '{"question":"preserved question", "education_stage":""}', '{"answer":"preserved student answer"}'))


def test_attempt_snapshots_and_fingerprints_not_rewritten_but_bound_to_plan_and_receipt(database, tmp_path):
    _add_attempts(database)
    args = _args(database, tmp_path)
    attempts = _rows(database, "question_attempt_events")
    migration.apply_database(database, **args)
    assert _rows(database, "question_attempt_events") == attempts
    assert _rows(args["backup_path"], "question_attempt_events") == attempts
    assert migration.apply_database(database, **args)["mode"] == "already-applied"
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE question_attempt_events SET question_fingerprint='changed' WHERE id=1")
    with pytest.raises(ValueError, match="拒绝覆盖"):
        migration.apply_database(database, **args)


@pytest.mark.parametrize("payload", ['{"teaching_context":{"education_stage":"junior"}}', '{"education_stage":"junior"}', 'broken-json'])
def test_associated_attempt_conflict_or_bad_json_rejects_adoption(database, payload):
    _add_attempts(database)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE question_attempt_events SET question_snapshot_json=? WHERE id=1", (payload,))
    with pytest.raises(ValueError):
        migration.inspect_database(database)


def test_attempt_dependency_edit_after_preview_invalidates_plan_even_without_stage_change(database, tmp_path):
    _add_attempts(database)
    args = _args(database, tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE question_attempt_events SET question_fingerprint='edited' WHERE id=1")
    with pytest.raises(ValueError, match="dry-run 后变化"):
        migration.apply_database(database, **args)
    assert not args["backup_path"].exists()


def test_missing_archive_schema_and_attempt_schema_fail_closed(database):
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE grading_results DROP COLUMN ocr_raw_json")
    with pytest.raises(ValueError, match="字段不完整"):
        migration.inspect_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE grading_results ADD COLUMN ocr_raw_json TEXT")
        connection.execute("CREATE TABLE question_attempt_events(id INTEGER PRIMARY KEY)")
    with pytest.raises(ValueError, match="question_attempt_events 字段不完整"):
        migration.inspect_database(database)


def test_unrelated_junior_attempt_is_untouched_and_does_not_block(database, tmp_path):
    _add_attempts(database)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO question_attempt_events(id,grading_result_id,question_bank_id,question_snapshot_json) VALUES (2,3,3,?)", ('{"education_stage":"junior"}',))
    before = _rows(database, "question_attempt_events")
    migration.apply_database(database, **_args(database, tmp_path))
    assert _rows(database, "question_attempt_events") == before


def test_only_actual_metadata_is_checked_not_text_containing_stage_keys(database, tmp_path):
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE grading_results SET grading_details=? WHERE id=1", (
            '[{"question":"education_stage=junior is literal text", "student_answer":"teaching_context junior", "education_stage":"senior"}]',))
    before = _rows(database, "grading_results")[0]["grading_details"]
    migration.apply_database(database, **_args(database, tmp_path))
    assert _rows(database, "grading_results")[0]["grading_details"] == before


@pytest.mark.parametrize("field", ["grading_result_id", "question_bank_id"])
def test_attempt_cannot_connect_legacy_candidate_to_explicit_junior_parent(database, field):
    _add_attempts(database)
    with sqlite3.connect(database) as connection:
        connection.execute(f'UPDATE question_attempt_events SET "{field}"=3 WHERE id=1')
    with pytest.raises(ValueError, match="关联明确非高中"):
        migration.inspect_database(database)


def test_existing_valid_full_senior_context_is_preserved(database, tmp_path):
    from services.teaching_context import make_teaching_context
    snapshot = json.dumps([{"teaching_context": make_teaching_context("senior"),
                            "education_stage": "senior", "catalog_version": catalog_version("senior")}])
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE grading_results SET grading_details=? WHERE id=1", (snapshot,))
    migration.apply_database(database, **_args(database, tmp_path))
    assert _rows(database, "grading_results")[0]["grading_details"] == snapshot
