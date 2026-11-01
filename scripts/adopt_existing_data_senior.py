"""Explicit, offline adoption of confirmed historical mathematics data as senior.

Default dry-run. No application/ORM/config imports, model requests or automatic
startup hook. Eight fixed tables share one reviewed plan and one transaction.
Scores, ownership, answer contracts, archived JSON and user profiles are never
rewritten. Stop every writer and retain the verified backup before applying.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sqlite3
import sys

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from scripts.adopt_existing_bank_senior import (
    _backup_database, _connect, _database_path, _digest, _file_digest, _write_receipt,
)
from services.math_curriculum import catalog_version, get_knowledge_point
from services.teaching_context import validate_teaching_context


OPERATION = "existing-math-data-to-senior-v1"
STAGE_TABLES = (
    "question_bank", "grading_results", "student_profiles", "knowledge_events",
    "exam_scores", "weaknesses", "parent_letter_shares",
)
TABLES = (*STAGE_TABLES, "weakness_overrides")
POINT_TABLES = frozenset({"question_bank", "knowledge_events"})
CHANGED_FIELDS = {
    table: frozenset({"education_stage", "catalog_version"})
    | (frozenset({"knowledge_point_id"}) if table in POINT_TABLES else frozenset())
    for table in STAGE_TABLES
}
CHANGED_FIELDS["weakness_overrides"] = frozenset({"topic_key"})
REQUIRED_FIELDS = {
    table: fields | {"id"} for table, fields in CHANGED_FIELDS.items()
}
REQUIRED_FIELDS["question_bank"] |= {"subject", "chapter", "topic", "question", "answer", "teacher_id", "is_public"}
REQUIRED_FIELDS["knowledge_events"] |= {"chapter", "topic", "student_id", "teacher_id"}
REQUIRED_FIELDS["weakness_overrides"] |= {"student_id", "teacher_id", "status"}
REQUIRED_FIELDS["grading_results"] |= {"grading_details", "ocr_raw_json"}
REQUIRED_FIELDS["student_profiles"] |= {"details_json"}
ARCHIVE_JSON_FIELDS = {
    "grading_results": ("grading_details", "ocr_raw_json"),
    "student_profiles": ("details_json",),
}
SOURCE_LINKS = (
    ("question_bank", "source_grading_id", "grading_results"),
    ("student_profiles", "grading_result_id", "grading_results"),
    ("knowledge_events", "grading_result_id", "grading_results"),
    ("knowledge_events", "question_bank_id", "question_bank"),
)


def _validate_map(value, name):
    if (not isinstance(value, dict) or set(value) != set(TABLES)
            or any(type(item) is not int or item < 0 for item in value.values())):
        raise ValueError(f"{name} 必须包含且仅包含全部八个固定表名及其非负整数")
    return {table: value[table] for table in TABLES}


def _validate_schema(connection):
    for table in TABLES:
        definition = connection.execute(
            "SELECT type, sql FROM sqlite_master WHERE name=?", (table,),
        ).fetchone()
        fields = list(connection.execute(f'PRAGMA table_info("{table}")'))
        id_fields = [field for field in fields if field["name"] == "id"]
        if (not definition or definition["type"] != "table"
                or not REQUIRED_FIELDS[table].issubset({field["name"] for field in fields})
                or len(id_fields) != 1 or id_fields[0]["type"].upper() != "INTEGER"
                or id_fields[0]["pk"] != 1 or sum(bool(field["pk"]) for field in fields) != 1):
            raise ValueError(f"{table} 字段不完整或主键不兼容；须先完成已审核的 schema 迁移")
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,),
        ).fetchone():
            raise ValueError(f"{table} 存在触发器，须单独审核，不允许迁移附带其他写入")


def _read_rows(connection, table, through_id=None):
    if table not in TABLES:
        raise ValueError("不允许读取固定范围之外的表")
    where = "" if through_id is None else " WHERE id <= ?"
    params = () if through_id is None else (through_id,)
    return [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"{where} ORDER BY id', params)]


def _is_candidate(table, row):
    if table == "weakness_overrides":
        value = row["topic_key"]
        if not isinstance(value, str) or len(value.split("||")) not in (2, 3):
            raise ValueError("weakness_overrides 存在无法解释的 topic_key，拒绝猜测归属")
        return len(value.split("||")) == 2
    return row["education_stage"] in (None, "")


def _check_archive_json(raw, label):
    """Inspect known metadata containers; never interpret or rewrite answer text."""
    if raw in (None, ""):
        return
    def unique_pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    def reject_constant(_value):
        raise ValueError("nonfinite JSON value")
    try:
        value = json.loads(raw, object_pairs_hook=unique_pairs, parse_constant=reject_constant)
    except (TypeError, ValueError):
        raise ValueError(f"{label} JSON 损坏，无法可靠核查学段；未写入") from None
    containers = {"questions", "details", "grading_details", "pages", "results", "data", "question_snapshot"}
    def inspect(item):
        if item is None:
            return
        if isinstance(item, list):
            for child in item:
                inspect(child)
            return
        if not isinstance(item, dict):
            raise ValueError(f"{label} JSON 结构不兼容，无法可靠核查学段；未写入")
        if "education_stage" in item and item["education_stage"] not in (None, "", "senior"):
            raise ValueError(f"{label} 包含明确非高中学段快照，须人工核查")
        if item.get("catalog_version") not in (None, "", catalog_version("senior")):
            raise ValueError(f"{label} 包含不兼容的既有目录版本，须人工核查")
        context = item.get("teaching_context")
        if context is not None:
            if not isinstance(context, dict):
                raise ValueError(f"{label} teaching_context 结构不兼容，须人工核查")
            if context.get("education_stage") not in (None, "", "senior"):
                raise ValueError(f"{label} 包含明确非高中教学快照，须人工核查")
            try:
                validate_teaching_context(context)
            except ValueError:
                raise ValueError(f"{label} 包含不完整或不兼容的既有教学快照，须人工核查") from None
        for key in containers & item.keys():
            if isinstance(item[key], (dict, list)):
                inspect(item[key])
    inspect(value)


def _archive_dependencies(connection, candidates):
    for table, fields in ARCHIVE_JSON_FIELDS.items():
        for row in candidates[table]:
            for field in fields:
                _check_archive_json(row[field], f"{table}.{field}")
    definition = connection.execute(
        "SELECT type FROM sqlite_master WHERE name='question_attempt_events'",
    ).fetchone()
    if definition is None:
        return {"attempt_table_present": False, "attempts": []}
    fields = {row["name"] for row in connection.execute('PRAGMA table_info("question_attempt_events")')}
    required = {"id", "grading_result_id", "question_bank_id", "question_snapshot_json"}
    if definition["type"] != "table" or not required.issubset(fields):
        raise ValueError("question_attempt_events 字段不完整，无法核查关联快照")
    grading_ids = {row["id"] for row in candidates["grading_results"]}
    bank_ids = {row["id"] for row in candidates["question_bank"]}
    attempts = []
    for row in connection.execute("SELECT * FROM question_attempt_events ORDER BY id"):
        if row["grading_result_id"] not in grading_ids and row["question_bank_id"] not in bank_ids:
            continue
        item = dict(row)
        _check_archive_json(item["question_snapshot_json"], "question_attempt_events.question_snapshot_json")
        sources = []
        for field, table, adopted_ids in (("grading_result_id", "grading_results", grading_ids),
                                          ("question_bank_id", "question_bank", bank_ids)):
            source_id = item[field]
            source = connection.execute(f'SELECT education_stage FROM "{table}" WHERE id=?', (source_id,)).fetchone()
            if source is not None and source["education_stage"] not in (None, "", "senior"):
                raise ValueError(f"question_attempt_events 关联明确非高中的 {table}，须人工核查")
            stage = ("senior" if source_id in adopted_ids else source["education_stage"]) if source else "missing"
            sources.append([field, source_id, stage])
        attempts.append({"row": item, "sources": sources})
    return {"attempt_table_present": True, "attempts": attempts}


def _dependencies(connection, candidates, through_ids):
    """Fail closed on conflicting explicit sources or out-of-bound legacy links."""
    context = []
    for table, field, source_table in SOURCE_LINKS:
        for row in candidates[table]:
            source_id = row.get(field)
            if source_id is None:
                continue
            source = connection.execute(
                f'SELECT id, education_stage FROM "{source_table}" WHERE id=?', (source_id,),
            ).fetchone()
            # Preexisting orphan references are not repaired by this adoption.
            if source is None:
                context.append([table, row["id"], field, source_id, "missing"])
                continue
            stage = source["education_stage"]
            if stage not in (None, "", "senior"):
                raise ValueError(f"{table} 的未标记记录引用明确非高中的 {source_table}，须人工核查")
            if stage in (None, "") and source["id"] > through_ids[source_table]:
                raise ValueError(f"{table} 引用固定边界外的未标记 {source_table}，须重新审核完整计划")
            context.append([table, row["id"], field, source_id, stage])
    override_rows = _read_rows(connection, "weakness_overrides")
    keys = {(row["student_id"], row["topic_key"]): row["id"] for row in override_rows}
    if len(keys) != len(override_rows):
        raise ValueError("weakness_overrides 存在重复学生知识点键，拒绝覆盖")
    for row in candidates["weakness_overrides"]:
        key = (row["student_id"], "senior||" + row["topic_key"])
        if key in keys:
            raise ValueError("weakness_overrides 高中目标键冲突，拒绝删除或覆盖已有人工判定")
        context.append(["weakness_overrides", row["id"], "target_absent", *key])
    return {"source_links": context, "archives": _archive_dependencies(connection, candidates)}


def _plan(connection, path, through_ids=None):
    _validate_schema(connection)
    if through_ids is None:
        through_ids = {table: connection.execute(
            f'SELECT COALESCE(MAX(id), 0) FROM "{table}"',
        ).fetchone()[0] for table in TABLES}
    through_ids = _validate_map(through_ids, "through_ids")
    before = {table: _read_rows(connection, table, through_ids[table]) for table in TABLES}
    if any(type(row["id"]) is not int or row["id"] < 1 for rows in before.values() for row in rows):
        raise ValueError("候选范围存在非正整数主键，须人工核查")
    candidates = {table: [row for row in rows if _is_candidate(table, row)] for table, rows in before.items()}
    if any((row["subject"] or "数学").strip() not in ("", "数学") for row in candidates["question_bank"]):
        raise ValueError("未标记题库包含非数学题，拒绝整批归为高中数学")
    dependencies = _dependencies(connection, candidates, through_ids)
    version = catalog_version("senior")
    changed, reports, after = {}, {}, {}
    for table in TABLES:
        changed[table] = []
        matches = 0
        for row in candidates[table]:
            if table == "weakness_overrides":
                new = {**row, "topic_key": "senior||" + row["topic_key"]}
            else:
                new = {**row, "education_stage": "senior", "catalog_version": version}
                if table in POINT_TABLES:
                    point = get_knowledge_point("senior", row["chapter"] or "", row["topic"] or "")
                    matches += int(point is not None)
                    new["knowledge_point_id"] = point["id"] if point else ""
            changed[table].append(new)
        by_id = {row["id"]: row for row in changed[table]}
        after[table] = [by_id.get(row["id"], row) for row in before[table]]
        reports[table] = {"through_id": through_ids[table], "eligible_count": len(changed[table]),
                          "already_labeled_count": len(before[table]) - len(changed[table])}
        if table in POINT_TABLES:
            reports[table].update(exact_knowledge_matches=matches,
                                  unmatched_knowledge_count=len(changed[table]) - matches)
    plan_sha = _digest({"operation": OPERATION, "database": str(path), "through_ids": through_ids,
                        "catalog_version": version, "before": before, "after": after, "dependencies": dependencies})
    report = {"operation": OPERATION, "mode": "dry-run", "through_ids": through_ids,
              "eligible_counts": {table: len(changed[table]) for table in TABLES},
              "tables": reports, "catalog_version": version, "plan_sha256": plan_sha}
    return report, before, after, changed


def inspect_database(db_path, *, through_ids=None):
    path = _database_path(db_path)
    connection = _connect(path)
    try:
        connection.execute("BEGIN")
        return _plan(connection, path, through_ids)[0]
    finally:
        connection.close()


def _replay_result(connection, path, backup, receipt_path, *, through_ids, expected_counts, expected_plan_sha256):
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        expected = {"operation": OPERATION, "database": str(path), "through_ids": through_ids,
                    "eligible_counts": expected_counts, "plan_sha256": expected_plan_sha256,
                    "catalog_version": catalog_version("senior")}
        if not isinstance(receipt, dict) or any(receipt.get(key) != value for key, value in expected.items()):
            raise ValueError
        if _file_digest(backup) != receipt["backup_sha256"]:
            raise ValueError
        actual = {table: _read_rows(connection, table, through_ids[table]) for table in TABLES}
        if _digest(actual) != receipt["after_sha256"]:
            raise ValueError
        changed_ids = receipt["changed_ids"]
        if not isinstance(changed_ids, dict) or set(changed_ids) != set(TABLES):
            raise ValueError
        selected = {}
        for table in TABLES:
            ids = changed_ids[table]
            if (not isinstance(ids, list) or len(ids) != expected_counts[table]
                    or any(type(row_id) is not int or not 0 < row_id <= through_ids[table] for row_id in ids)
                    or len(set(ids)) != len(ids)):
                raise ValueError
            selected[table] = [row for row in actual[table] if row["id"] in set(ids)]
            if len(selected[table]) != len(ids):
                raise ValueError
        if _digest(_archive_dependencies(connection, selected)) != receipt["archive_dependencies_sha256"]:
            raise ValueError
        return {**expected, "mode": "already-applied", "updated": 0,
                "updated_counts": {table: 0 for table in TABLES}}
    except (OSError, ValueError, TypeError, KeyError):
        raise ValueError("备份路径或回执已存在，但不匹配已成功执行的同一计划；拒绝覆盖或重复迁移") from None


def _update_row(connection, table, row):
    if table == "weakness_overrides":
        return connection.execute(
            "UPDATE weakness_overrides SET topic_key=? WHERE id=? AND topic_key=?",
            (row["topic_key"], row["id"], row["topic_key"][len("senior||"):]),
        )
    fields = sorted(CHANGED_FIELDS[table])
    return connection.execute(
        f'UPDATE "{table}" SET ' + ", ".join(f'"{field}"=?' for field in fields)
        + " WHERE id=? AND (education_stage IS NULL OR education_stage='')",
        (*[row[field] for field in fields], row["id"]),
    )


def apply_database(db_path, *, through_ids, expected_counts, expected_plan_sha256,
                   backup_path, confirm_senior=False, confirm_offline=False):
    if not confirm_senior or not confirm_offline:
        raise ValueError("apply 必须明确确认既有旧数据均为高中数学，并已停止应用及全部后台写入")
    through_ids = _validate_map(through_ids, "through_ids")
    expected_counts = _validate_map(expected_counts, "expected_counts")
    if not isinstance(expected_plan_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_plan_sha256):
        raise ValueError("必须提供 dry-run 的完整 plan_sha256")
    path = _database_path(db_path)
    backup_input = Path(backup_path).expanduser()
    if backup_input.is_symlink():
        raise ValueError("备份不能是符号链接")
    backup = backup_input.resolve()
    receipt_path = backup.with_name(backup.name + ".adoption.json")
    protected = {path, *(Path(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal"))}
    if backup in protected or receipt_path in protected or not backup.parent.is_dir() or receipt_path.is_symlink():
        raise ValueError("备份必须使用已有目录中的全新文件，不能覆盖原数据库")
    connection = _connect(path, writable=True)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _validate_schema(connection)
        if backup.exists() or receipt_path.exists():
            return _replay_result(connection, path, backup, receipt_path, through_ids=through_ids,
                                  expected_counts=expected_counts, expected_plan_sha256=expected_plan_sha256)
        plan, before, after, changed = _plan(connection, path, through_ids)
        if plan["eligible_counts"] != expected_counts or plan["plan_sha256"] != expected_plan_sha256:
            raise ValueError("数据、依赖或目录已在 dry-run 后变化；未写入，请停止全部写入后重新审核")
        total = sum(expected_counts.values())
        if not total:
            return {**plan, "mode": "no-op", "updated": 0, "updated_counts": expected_counts}
        _backup_database(path, backup)
        candidates = {table: [row for row in before[table] if _is_candidate(table, row)] for table in TABLES}
        receipt = {**plan, "database": str(path), "backup_sha256": _file_digest(backup),
                   "after_sha256": _digest(after),
                   "changed_ids": {table: [row["id"] for row in rows] for table, rows in changed.items()},
                   "archive_dependencies_sha256": _digest(_archive_dependencies(connection, candidates))}
        _write_receipt(receipt_path, receipt)
        for table in TABLES:
            for row in changed[table]:
                if _update_row(connection, table, row).rowcount != 1:
                    raise ValueError(f"{table} 迁移期间状态变化，已回滚全部表")
        actual = {table: _read_rows(connection, table, through_ids[table]) for table in TABLES}
        if actual != after:
            raise ValueError("字段保存结果与计划不符，已回滚全部表")
        connection.commit()
        return {**plan, "mode": "applied", "updated": total, "updated_counts": expected_counts,
                "backup": str(backup), "receipt": str(receipt_path)}
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def _json_object(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("JSON 表名重复")
            result[key] = item
        return result
    try:
        return json.loads(value, object_pairs_hook=pairs)
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError("须提供无重复键的 JSON 对象") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="显式已有 SQLite 文件；不读取环境变量或 dotenv")
    parser.add_argument("--through-ids", type=_json_object)
    parser.add_argument("--expected-counts", type=_json_object)
    parser.add_argument("--plan-sha256")
    parser.add_argument("--backup")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-existing-data-is-senior", action="store_true")
    parser.add_argument("--confirm-offline", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.apply:
            if args.backup is None:
                raise ValueError("apply 必须提供全新 --backup 路径")
            result = apply_database(args.db, through_ids=args.through_ids, expected_counts=args.expected_counts,
                                    expected_plan_sha256=args.plan_sha256, backup_path=args.backup,
                                    confirm_senior=args.confirm_existing_data_is_senior, confirm_offline=args.confirm_offline)
        else:
            if (any(value is not None for value in (args.expected_counts, args.plan_sha256, args.backup))
                    or args.confirm_existing_data_is_senior or args.confirm_offline):
                raise ValueError("未指定 --apply：不接受执行确认参数；默认只读 dry-run")
            result = inspect_database(args.db, through_ids=args.through_ids)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"迁移未执行或已回滚：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
