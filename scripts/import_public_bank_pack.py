"""Operator-only, bounded public-bank data publication (dry-run by default).

No dotenv/config/ORM/app startup, credentials, migration, models or network calls.
Only INSERTs into the existing question_bank, never edits an existing question.
Apply binds pack bytes and a reviewed content-bound plan, takes SQLite online
backup, verifies rows before commit, and emits an immutable ID/provenance receipt.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from services.math_curriculum import catalog_version, get_knowledge_point

OPERATION = "public-bank-pack-v1"
COLUMNS = (
    "subject", "education_stage", "catalog_version", "knowledge_point_id",
    "teacher_id", "is_public", "question", "answer", "question_type", "chapter",
    "topic", "difficulty", "solution_steps", "answer_variants", "grading_method",
    "source", "source_grading_id", "usage_count", "classification_status",
    "classification_source", "classification_confidence", "classification_version",
    "classification_error", "classified_at", "created_at", "updated_at",
)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def file_digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def text_key(value):
    # Remove only an explicit exam-year attribution prefix. Preserve every
    # mathematical character, sign, number and TeX command; no fuzzy threshold.
    value = re.sub(r"^【(?:19|20)\d{2}[^】]*(?:高考|中考)[^】]*】\s*", "", value)
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value)).strip()


def load_pack(path, *, stage, expected_count, expected_sha256, source):
    if not re.fullmatch(r"[a-z0-9_-]{1,20}", source):
        raise ValueError("source must be an explicit safe tag of 1-20 characters")
    if file_digest(path) != expected_sha256:
        raise ValueError("pack SHA-256 mismatch")
    items = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(items, list) or not items or len(items) != expected_count:
        raise ValueError("pack count mismatch or empty pack")
    version = catalog_version(stage)
    prepared = []
    seen = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict) or item.get("education_stage") != stage or item.get("subject") != "数学":
            raise ValueError(f"item {index}: stage/subject mismatch")
        for key in ("question", "answer", "chapter", "topic", "grading_method"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError(f"item {index}: missing string {key}")
        if item.get("question_type") not in {"choice", "fill", "solution"}:
            raise ValueError(f"item {index}: unsupported question type")
        if item["grading_method"] not in {"exact", "contains", "equivalent"}:
            raise ValueError(f"item {index}: unsupported grading method")
        if type(item.get("difficulty")) is not int or not 1 <= item["difficulty"] <= 5:
            raise ValueError(f"item {index}: difficulty must be integer 1-5")
        for key in ("solution_steps", "answer_variants"):
            if not isinstance(item.get(key), list) or any(not isinstance(x, str) for x in item[key]):
                raise ValueError(f"item {index}: {key} must be a string array")
        point = get_knowledge_point(stage, item["chapter"], item["topic"])
        if not point:
            raise ValueError(f"item {index}: exact curriculum mapping missing")
        question, answer = item["question"].strip(), item["answer"].strip()
        key = text_key(question)
        if key in seen:
            raise ValueError(f"item {index}: duplicate question within pack, first index {seen[key]}")
        seen[key] = index
        if item["question_type"] == "choice":
            if any(not re.search(r"(?:^|\s)" + label + r"[.．、:：)）]", question) for label in "ABCD"):
                raise ValueError(f"item {index}: choice options missing")
            if not re.fullmatch(r"[A-D]{1,4}", answer) or len(set(answer)) != len(answer):
                raise ValueError(f"item {index}: choice answer is not a letter set")
        prepared.append({
            "subject": "数学", "education_stage": stage, "catalog_version": version,
            "knowledge_point_id": point["id"], "teacher_id": None, "is_public": 1,
            "question": question, "answer": answer, "question_type": item["question_type"],
            "chapter": item["chapter"], "topic": item["topic"], "difficulty": item["difficulty"],
            "solution_steps": json.dumps(item["solution_steps"], ensure_ascii=False),
            "answer_variants": json.dumps(item["answer_variants"], ensure_ascii=False),
            "grading_method": item["grading_method"], "source": source,
            "source_grading_id": None, "usage_count": 0,
            "classification_status": "confirmed", "classification_source": "import",
            "classification_confidence": "high", "classification_version": version,
            "classification_error": "",
        })
    return prepared


def connect(path, *, writable=False):
    if path.is_symlink() or not path.is_file():
        raise ValueError("explicit existing non-symlink SQLite database required")
    connection = sqlite3.connect(path.resolve().as_uri() + ("?mode=rw" if writable else "?mode=ro"),
                                 uri=True, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=15000")
    if not writable:
        connection.execute("PRAGMA query_only=ON")
    return connection


def validate_schema(connection):
    fields = {row["name"] for row in connection.execute("PRAGMA table_info(question_bank)")}
    if not ({"id"} | set(COLUMNS)).issubset(fields):
        raise ValueError("question_bank schema is incomplete; this tool never migrates it")
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name='question_bank'").fetchone():
        raise ValueError("question_bank triggers require a separate review")


def scope_rows(connection, stage, version):
    return [dict(row) for row in connection.execute(
        "SELECT * FROM question_bank WHERE teacher_id IS NULL AND is_public=1 "
        "AND subject='数学' AND education_stage=? AND catalog_version=? ORDER BY id", (stage, version))]


def immutable_rows(rows):
    # Normal question use can update usage/time concurrently, not math content.
    return [{k: v for k, v in row.items() if k not in {"usage_count", "updated_at"}} for row in rows]


def build_plan(connection, path, prepared, pack_sha256):
    validate_schema(connection)
    stage, version = prepared[0]["education_stage"], prepared[0]["catalog_version"]
    existing = scope_rows(connection, stage, version)
    by_key = {}
    for row in existing:
        by_key.setdefault(text_key(row["question"]), []).append(row)
    matched, new, conflicts = [], [], []
    for index, item in enumerate(prepared):
        rows = by_key.get(text_key(item["question"]), [])
        if not rows:
            new.append(index)
        elif any(text_key(row["answer"]) != text_key(item["answer"]) for row in rows):
            conflicts.append({"input_index": index, "existing_ids": [row["id"] for row in rows]})
        else:
            matched.append({"input_index": index, "existing_id": rows[0]["id"]})
    details = {"operation": OPERATION, "database_path": str(path.resolve()), "pack_sha256": pack_sha256,
               "stage": stage, "catalog_version": version, "input_count": len(prepared),
               "source": prepared[0]["source"], "existing_scope_count": len(existing),
               "insert_count": len(new), "duplicate_count": len(matched), "conflicts": conflicts,
               "new_indexes": new, "existing_matches": matched,
               "scope_content_sha256": digest(immutable_rows(existing)), "prepared_sha256": digest(prepared)}
    return {**details, "plan_sha256": digest(details)}, existing


def backup_database(path, destination):
    if destination.is_symlink():
        raise ValueError("backup cannot be a symlink")
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    source = connect(path)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
        if target.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("online backup failed integrity check")
    finally:
        target.close()
        source.close()
    return file_digest(destination)


def write_new_json(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def inspect_pack(database, prepared, pack_sha256):
    with closing(connect(database)) as connection:
        connection.execute("BEGIN")
        return build_plan(connection, database, prepared, pack_sha256)[0]


def apply_pack(database, prepared, *, pack_sha256, expected_plan_sha256, backup, receipt):
    if backup.exists() or receipt.exists():
        raise ValueError("backup/receipt path already exists; use read-only verification, never overwrite")
    if not backup.parent.is_dir() or not receipt.parent.is_dir():
        raise ValueError("existing operator-owned backup/receipt directories required")
    connection = connect(database, writable=True)
    try:
        connection.execute("BEGIN IMMEDIATE")
        plan, before = build_plan(connection, database, prepared, pack_sha256)
        if plan["plan_sha256"] != expected_plan_sha256:
            raise ValueError("reviewed plan changed; dry-run again before applying")
        if plan["conflicts"]:
            raise ValueError("answer conflict, no rows inserted")
        backup_sha256 = backup_database(database, backup)
        stamp = datetime.now().isoformat(sep=" ", timespec="microseconds")
        inserted = []
        statement = "INSERT INTO question_bank (" + ",".join(COLUMNS) + ") VALUES (" + ",".join("?" for _ in COLUMNS) + ")"
        for index in plan["new_indexes"]:
            item = {**prepared[index], "classified_at": stamp, "created_at": stamp, "updated_at": stamp}
            cursor = connection.execute(statement, [item[key] for key in COLUMNS])
            inserted.append({"input_index": index, "question_id": cursor.lastrowid,
                             "question_sha256": hashlib.sha256(item["question"].encode()).hexdigest()})
            row = dict(connection.execute("SELECT * FROM question_bank WHERE id=?", (cursor.lastrowid,)).fetchone())
            if any(row[key] != item[key] for key in COLUMNS):
                raise ValueError(f"row {cursor.lastrowid}: roundtrip mismatch")
        after = scope_rows(connection, plan["stage"], plan["catalog_version"])
        old_ids = {row["id"] for row in before}
        if digest(immutable_rows([row for row in after if row["id"] in old_ids])) != plan["scope_content_sha256"]:
            raise ValueError("existing public rows changed; rolling back")
        if len(after) != len(before) + len(inserted):
            raise ValueError("scope count mismatch; rolling back")
        result = {"operation": OPERATION, "status": "committed", "plan": plan,
                  "inserted": inserted, "duplicate_matches": plan["existing_matches"],
                  "backup_path": str(backup.resolve()), "backup_sha256": backup_sha256,
                  "stored_scope_count": len(after), "all_inserted_fields_verified": True,
                  "existing_public_content_unchanged": True,
                  "empty_solution_steps_count": sum(prepared[x["input_index"]]["solution_steps"] == "[]" for x in inserted),
                  "committed_at": stamp}
        # Persist a pending receipt before COMMIT; a crash is detectable via IDs.
        pending = receipt.with_name(receipt.name + ".pending")
        write_new_json(pending, {**result, "status": "pending_commit"})
        connection.commit()
        # Keep pending on any post-commit I/O error; never claim rollback then.
        write_new_json(receipt, result)
        pending.unlink()
        return result
    finally:
        connection.close()


def verify_receipt(database, prepared, receipt):
    saved = json.loads(receipt.read_text(encoding="utf-8"))
    if saved.get("operation") != OPERATION or saved.get("status") != "committed":
        raise ValueError("uncommitted or invalid receipt")
    plan = saved["plan"]
    if plan["database_path"] != str(database.resolve()) or plan["prepared_sha256"] != digest(prepared):
        raise ValueError("receipt database/pack identity mismatch")
    failures = []
    with closing(connect(database)) as connection:
        validate_schema(connection)
        for entry in saved["inserted"]:
            expected = prepared[entry["input_index"]]
            fetched = connection.execute("SELECT * FROM question_bank WHERE id=? AND teacher_id IS NULL AND is_public=1",
                                         (entry["question_id"],)).fetchone()
            row = dict(fetched) if fetched else {}
            if any(row.get(key) != value for key, value in expected.items() if key != "usage_count"):
                failures.append(entry["question_id"])
        for entry in saved["duplicate_matches"]:
            expected = prepared[entry["input_index"]]
            row = connection.execute("SELECT question,answer,education_stage,catalog_version FROM question_bank "
                                     "WHERE id=? AND teacher_id IS NULL AND is_public=1", (entry["existing_id"],)).fetchone()
            if (not row or text_key(row["question"]) != text_key(expected["question"])
                    or text_key(row["answer"]) != text_key(expected["answer"])
                    or row["education_stage"] != expected["education_stage"] or row["catalog_version"] != expected["catalog_version"]):
                failures.append(entry["existing_id"])
        counts = [dict(zip(("education_stage", "count"), row)) for row in connection.execute(
            "SELECT education_stage,COUNT(*) FROM question_bank WHERE teacher_id IS NULL AND is_public=1 AND subject='数学' GROUP BY education_stage")]
    result = {"verified": not failures, "inserted_verified": len(saved["inserted"]),
              "existing_duplicates_verified": len(saved["duplicate_matches"]), "failing_ids": failures,
              "public_stage_counts": counts, "pack_sha256": plan["pack_sha256"]}
    if failures:
        raise ValueError("receipt roundtrip verification failed")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--pack", type=Path, required=True)
    parser.add_argument("--stage", choices=("primary", "junior", "senior"), required=True)
    parser.add_argument("--expected-count", type=int, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-public-bank-insert", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--verify-receipt", type=Path)
    args = parser.parse_args()
    prepared = load_pack(args.pack, stage=args.stage, expected_count=args.expected_count,
                         expected_sha256=args.expected_sha256, source=args.source)
    if args.apply and args.verify_receipt:
        parser.error("apply and verify are mutually exclusive")
    if args.apply:
        if not (args.confirm_public_bank_insert and args.expected_plan_sha256 and args.backup and args.receipt):
            parser.error("apply requires explicit confirmation, reviewed digest, backup and receipt")
        result = apply_pack(args.database, prepared, pack_sha256=args.expected_sha256,
                            expected_plan_sha256=args.expected_plan_sha256, backup=args.backup, receipt=args.receipt)
    elif args.verify_receipt:
        result = verify_receipt(args.database, prepared, args.verify_receipt)
    else:
        result = inspect_pack(args.database, prepared, args.expected_sha256)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
