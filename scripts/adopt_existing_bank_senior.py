"""One-off, operator-approved adoption of the existing unlabeled math bank.

Default: read-only dry-run. Never imported/called by application startup. Apply
requires a reviewed row boundary/count/digest, an unused backup destination and
explicit confirmations. No dotenv, application configuration, ORM or models are
loaded. The caller must stop all app/classification writers before applying.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from services.math_curriculum import catalog_version, get_knowledge_point


OPERATION = "existing-math-bank-to-senior-v1"
CHANGED_FIELDS = frozenset({"education_stage", "catalog_version", "knowledge_point_id"})
REQUIRED_FIELDS = CHANGED_FIELDS | {"id", "subject", "chapter", "topic", "question", "answer", "teacher_id", "is_public"}


def _digest(value) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _file_digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _database_path(value) -> Path:
    path = Path(value).expanduser()
    if path.is_symlink():
        raise ValueError("数据库路径不能是符号链接")
    path = path.resolve(strict=True)
    if not path.is_file():
        raise ValueError("必须显式指定已有 SQLite 数据库文件")
    return path


def _connect(path: Path, *, writable: bool = False):
    connection = sqlite3.connect(path.as_uri() + ("?mode=rw" if writable else "?mode=ro"), uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    if not writable:
        connection.execute("PRAGMA query_only=ON")
    return connection


def _validate_schema(connection):
    fields = {row["name"] for row in connection.execute('PRAGMA table_info("question_bank")')}
    if not REQUIRED_FIELDS.issubset(fields):
        raise ValueError("题库字段不完整；请先通过已审核的应用 schema 迁移，不由本工具建表或补字段")
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name='question_bank'").fetchone():
        raise ValueError("题库存在触发器，无法保证仅修改三个学段字段；须单独审核后处理")


def _read_rows(connection, through_id):
    return [dict(row) for row in connection.execute(
        "SELECT * FROM question_bank WHERE id <= ? ORDER BY id", (through_id,),
    )]


def _plan(connection, path, through_id=None):
    _validate_schema(connection)
    if through_id is None:
        through_id = connection.execute("SELECT COALESCE(MAX(id), 0) FROM question_bank").fetchone()[0]
    if isinstance(through_id, bool) or not isinstance(through_id, int) or through_id < 0:
        raise ValueError("through_id 必须是 dry-run 给出的非负题库 ID 边界")
    rows = _read_rows(connection, through_id)
    candidates = [row for row in rows if row["education_stage"] in (None, "")]
    if any((row["subject"] or "数学").strip() not in ("", "数学") for row in candidates):
        raise ValueError("未标记题库包含非数学题，拒绝整批归为高中数学")
    version = catalog_version("senior")
    changed = []
    matched = 0
    for row in candidates:
        point = get_knowledge_point("senior", row["chapter"] or "", row["topic"] or "")
        matched += int(point is not None)
        changed.append({**row, "education_stage": "senior", "catalog_version": version,
                        "knowledge_point_id": point["id"] if point else ""})
    plan_sha = _digest({"operation": OPERATION, "database": str(path), "through_id": through_id,
                        "catalog_version": version, "before": candidates, "after": changed})
    return {
        "operation": OPERATION, "mode": "dry-run", "through_id": through_id,
        "eligible_count": len(candidates), "already_labeled_count": len(rows) - len(candidates),
        "exact_knowledge_matches": matched, "unmatched_knowledge_count": len(candidates) - matched,
        "catalog_version": version, "plan_sha256": plan_sha,
    }, candidates, changed


def inspect_database(db_path, *, through_id=None) -> dict:
    """Read metadata/counts and a content-bound digest without emitting content."""
    path = _database_path(db_path)
    connection = _connect(path)
    try:
        connection.execute("BEGIN")
        return _plan(connection, path, through_id)[0]
    finally:
        connection.close()


def _backup_database(path: Path, backup: Path):
    # Called while a separate connection owns BEGIN IMMEDIATE, before any UPDATE.
    # A second read-only source avoids sqlite3.backup waiting on its own writer.
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    source = _connect(path)
    destination = sqlite3.connect(str(backup))
    try:
        source.backup(destination)
        if destination.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("备份完整性检查失败，未修改题库")
    finally:
        destination.close()
        source.close()


def _write_receipt(path: Path, receipt: dict):
    # Immutable recovery receipt: a successful replay also verifies actual rows.
    with path.open("x", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        json.dump(receipt, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())


def _replay_result(connection, path, backup, receipt_path, *, through_id, expected_count, expected_plan_sha256):
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if not isinstance(receipt, dict) or any(receipt.get(key) != value for key, value in {
            "operation": OPERATION, "database": str(path), "through_id": through_id,
            "eligible_count": expected_count, "plan_sha256": expected_plan_sha256,
            "catalog_version": catalog_version("senior"),
        }.items()):
            raise ValueError
        ids = receipt["question_ids"]
        if (not isinstance(ids, list) or len(ids) != expected_count
                or any(type(row_id) is not int or row_id < 1 or row_id > through_id for row_id in ids)
                or len(set(ids)) != len(ids) or _file_digest(backup) != receipt["backup_sha256"]):
            raise ValueError
        id_set = set(ids)
        rows = [row for row in _read_rows(connection, through_id) if row["id"] in id_set]
        if len(rows) != len(ids) or _digest(rows) != receipt["after_sha256"]:
            raise ValueError
        return {"mode": "already-applied", "updated": 0, "through_id": through_id,
                "catalog_version": receipt["catalog_version"], "plan_sha256": expected_plan_sha256}
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError("备份路径或回执已存在，但不匹配已成功执行的同一计划；拒绝覆盖或重复迁移") from None


def apply_database(db_path, *, through_id: int, expected_count: int, expected_plan_sha256: str,
                   backup_path, confirm_senior: bool = False, confirm_offline: bool = False) -> dict:
    """Apply precisely the reviewed snapshot, with backup and rollback safety."""
    if not confirm_senior or not confirm_offline:
        raise ValueError("apply 必须明确确认既有题库均为高中数学，并已停止应用及全部后台写入")
    if (type(expected_count) is not int or expected_count < 0 or type(through_id) is not int or through_id < 0
            or not isinstance(expected_plan_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_plan_sha256)):
        raise ValueError("必须提供 dry-run 的 through_id、eligible_count 和完整 plan_sha256")
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
            return _replay_result(connection, path, backup, receipt_path, through_id=through_id,
                                  expected_count=expected_count, expected_plan_sha256=expected_plan_sha256)
        plan, before, after = _plan(connection, path, through_id)
        if plan["eligible_count"] != expected_count or plan["plan_sha256"] != expected_plan_sha256:
            raise ValueError("题库或目录已在 dry-run 后变化；未写入，请停止全部写入后重新审核 dry-run")
        if not before:
            return {**plan, "mode": "no-op", "updated": 0}
        _backup_database(path, backup)
        receipt = {**plan, "database": str(path), "question_ids": [row["id"] for row in before],
                   "backup_sha256": _file_digest(backup), "after_sha256": _digest(after)}
        _write_receipt(receipt_path, receipt)
        for row in after:
            result = connection.execute(
                "UPDATE question_bank SET education_stage=?, catalog_version=?, knowledge_point_id=? "
                "WHERE id=? AND (education_stage IS NULL OR education_stage='')",
                (row["education_stage"], row["catalog_version"], row["knowledge_point_id"], row["id"]),
            )
            if result.rowcount != 1:
                raise ValueError("迁移期间题目状态变化，已回滚")
        expected_ids = {row["id"] for row in after}
        actual = [row for row in _read_rows(connection, through_id) if row["id"] in expected_ids]
        if actual != after:
            raise ValueError("字段保存结果与计划不符，已回滚")
        connection.commit()
        return {**plan, "mode": "applied", "updated": len(after), "backup": str(backup), "receipt": str(receipt_path)}
    finally:
        # No commit on any error (including backup/receipt/update failure).
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="显式指定已有 SQLite 文件；不读取环境变量或 dotenv")
    parser.add_argument("--through-id", type=int, help="固定 dry-run 的题目 ID 上限，避免未来新题被迁入")
    parser.add_argument("--apply", action="store_true", help="缺省只读预览")
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--plan-sha256")
    parser.add_argument("--backup", help="已有目录中的全新备份文件；禁止覆盖")
    parser.add_argument("--confirm-existing-bank-is-senior", action="store_true")
    parser.add_argument("--confirm-offline", action="store_true", help="确认已停止应用及分类队列等所有写入")
    args = parser.parse_args(argv)
    try:
        if args.apply:
            if args.backup is None:
                raise ValueError("apply 必须提供全新 --backup 路径")
            result = apply_database(args.db, through_id=args.through_id, expected_count=args.expected_count,
                                    expected_plan_sha256=args.plan_sha256, backup_path=args.backup,
                                    confirm_senior=args.confirm_existing_bank_is_senior,
                                    confirm_offline=args.confirm_offline)
        else:
            if any(value is not None for value in (args.expected_count, args.plan_sha256, args.backup)) or args.confirm_existing_bank_is_senior or args.confirm_offline:
                raise ValueError("未指定 --apply：不接受执行确认参数；默认只读 dry-run")
            result = inspect_database(args.db, through_id=args.through_id)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"迁移未执行或已回滚：{exc}", file=sys.stderr)
        return 2
    # Reporting failure after commit must not be mislabeled as a DB rollback.
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
