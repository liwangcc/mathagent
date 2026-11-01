"""文件保留策略清理器。

默认策略：
  - 学生试卷原图及其派生图像：终态后保留 36 小时。
  - 报告、成绩表、错题条等生成下载文件：保留 24 小时。
  - 逐题结果、错题、得分、正确率、知识点与审计信息：不由本脚本删除。

快速批阅仅清理已归档/已作废的有身份批次，以及已生成结果的
无身份批次。答题卡仅清理已归档会话。处理中、中断可恢复、待人工
复核的有身份批次均受保护。

用法：
    venv/bin/python scripts/cleanup_files.py --dry-run
    venv/bin/python scripts/cleanup_files.py --output-hours 24 --hours 36

退出码：0 成功；1 有文件删除失败；2 数据库或元数据处理失败。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, ContextManager

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import FAILED_DIR, UPLOAD_DIR  # noqa: E402
from database import SessionLocal  # noqa: E402
import models  # noqa: E402,F401  注册全部 ORM 表
import models_user  # noqa: E402,F401  GeneratedFile.owner_id 外键依赖 users 表
from services.answer_sheet_session import (  # noqa: E402
    AnswerSheetOperationBusy,
    answer_sheet_operation_lock,
)
from services.generated_files import cleanup_generated_files  # noqa: E402
from services.quick_grader import (  # noqa: E402
    BatchOperationBusy,
    batch_operation_lock,
)

PENDING_DIR = os.path.join(UPLOAD_DIR, "待处理")
RAW_ASSET_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif",
    ".tif", ".tiff", ".heic", ".heif", ".avif", ".pdf",
}
_SAFE_GROUP_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
_PATH_VALUE_KEYS = {"path", "directory", "template_image", "source_image"}
_PATH_LIST_KEYS = {"paths", "sample_paths", "image_paths"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _meta_mtime(meta_path: Path) -> datetime:
    return datetime.fromtimestamp(meta_path.stat().st_mtime, tz=timezone.utc)


def _latest_timestamp(values: list[object], fallback: datetime) -> datetime:
    parsed = [item for item in (_parse_timestamp(value) for value in values) if item]
    return max(parsed) if parsed else fallback


def _nested_timestamps(items: object, key: str) -> list[object]:
    if not isinstance(items, list):
        return []
    return [item.get(key) for item in items if isinstance(item, dict) and item.get(key)]


def _classify_quick_batch(meta: dict, meta_path: Path, cutoff: datetime) -> str:
    lifecycle = meta.get("asset_lifecycle")
    if isinstance(lifecycle, dict) and lifecycle.get("status") == "expired":
        return "already_expired"

    status = str(meta.get("status") or "")
    identity_mode = (
        "anonymous" if str(meta.get("identity_mode") or "") == "anonymous" else "identified"
    )
    fallback = _meta_mtime(meta_path)
    timestamps: list[object]
    if status == "archived":
        timestamps = [meta.get("review_archived_at")]
        timestamps.extend(_nested_timestamps(meta.get("review_results"), "archived_at"))
    elif status == "voided":
        timestamps = _nested_timestamps(meta.get("review_results"), "voided_at")
    elif status == "review_ready" and identity_mode == "anonymous":
        timestamps = [meta.get("review_ready_at"), meta.get("report_updated_at")]
    elif status == "done":
        timestamps = [
            meta.get("review_archived_at"),
            meta.get("review_ready_at"),
            meta.get("report_updated_at"),
            meta.get("created_at"),
        ]
    elif status == "failed":
        timestamps = [meta.get("created_at")]
    else:
        return "protected_active"

    anchor = _latest_timestamp(timestamps, fallback)
    return "eligible" if anchor < cutoff else "recent"


def _classify_answer_sheet(meta: dict, meta_path: Path, cutoff: datetime) -> str:
    lifecycle = meta.get("asset_lifecycle")
    lifecycle = lifecycle if isinstance(lifecycle, dict) else {}
    if lifecycle.get("status") == "expired":
        return "already_expired"
    if str(meta.get("status") or "") != "archived":
        return "protected_active"
    anchor = _latest_timestamp(
        [lifecycle.get("archived_at"), meta.get("updated_at"), meta.get("created_at")],
        _meta_mtime(meta_path),
    )
    return "eligible" if anchor < cutoff else "recent"


def _raw_asset_paths(root: Path) -> tuple[list[Path], int]:
    """Return regular image assets strictly inside root without following links."""
    root = root.resolve()
    assets: list[Path] = []
    skipped_unsafe = 0
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        safe_dirs: list[str] = []
        for dirname in dirnames:
            candidate = current_path / dirname
            if candidate.is_symlink():
                skipped_unsafe += 1
                continue
            try:
                candidate.resolve().relative_to(root)
            except (OSError, RuntimeError, ValueError):
                skipped_unsafe += 1
                continue
            safe_dirs.append(dirname)
        dirnames[:] = safe_dirs

        for filename in filenames:
            candidate = current_path / filename
            if candidate.suffix.lower() not in RAW_ASSET_SUFFIXES:
                continue
            if candidate.is_symlink():
                skipped_unsafe += 1
                continue
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(root)
            except (FileNotFoundError, OSError, RuntimeError, ValueError):
                skipped_unsafe += 1
                continue
            if resolved.is_file():
                assets.append(resolved)
    return assets, skipped_unsafe


def _path_is_inside_root(value: str, root: Path) -> bool:
    if not value or "\x00" in value:
        return False
    try:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate.resolve(strict=False).relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _is_path_value_key(key: str) -> bool:
    return key in _PATH_VALUE_KEYS or key.endswith("_path")


def _is_path_list_key(key: str) -> bool:
    return key in _PATH_LIST_KEYS or key.endswith("_paths")


def _scrub_asset_paths(value: object, root: Path) -> int:
    """Blank only batch-local path pointers; preserve all grading information."""
    scrubbed = 0
    if isinstance(value, dict):
        for key, item in list(value.items()):
            key_text = str(key)
            if _is_path_value_key(key_text) and isinstance(item, str):
                if _path_is_inside_root(item, root):
                    value[key] = ""
                    scrubbed += 1
                continue
            if _is_path_list_key(key_text) and isinstance(item, list):
                kept = []
                for entry in item:
                    if isinstance(entry, str) and _path_is_inside_root(entry, root):
                        scrubbed += 1
                    else:
                        kept.append(entry)
                value[key] = kept
                continue
            scrubbed += _scrub_asset_paths(item, root)
    elif isinstance(value, list):
        for item in value:
            scrubbed += _scrub_asset_paths(item, root)
    return scrubbed


def _write_json_atomic(path: Path, payload: dict) -> None:
    temporary = path.with_name(path.name + ".cleanup.tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def _prune_empty_directories(root: Path) -> None:
    for current, dirnames, _filenames in os.walk(root, topdown=False, followlinks=False):
        current_path = Path(current)
        for dirname in dirnames:
            candidate = current_path / dirname
            if candidate.is_symlink():
                continue
            try:
                candidate.rmdir()
            except OSError:
                pass


def _mark_assets_expired(
    meta: dict,
    root: Path,
    *,
    hours: int,
    deleted_files: int,
    expired_at: datetime,
) -> None:
    scrubbed = _scrub_asset_paths(meta, root.resolve())
    lifecycle = meta.get("asset_lifecycle")
    lifecycle = dict(lifecycle) if isinstance(lifecycle, dict) else {}
    try:
        previous_deleted = max(0, int(lifecycle.get("deleted_files") or 0))
    except (TypeError, ValueError):
        previous_deleted = 0
    lifecycle.update({
        "policy": f"raw_images_expire_after_{hours}h",
        "status": "expired",
        "retention_hours": hours,
        "expired_at": expired_at.isoformat(),
        "deleted_files": previous_deleted + deleted_files,
        "scrubbed_path_fields": scrubbed,
    })
    meta["asset_lifecycle"] = lifecycle


def _new_group_summary() -> dict:
    return {
        "scanned": 0,
        "candidates": 0,
        "would_delete_files": 0,
        "deleted_files": 0,
        "expired_groups": 0,
        "protected_active": 0,
        "recent": 0,
        "already_expired": 0,
        "busy": 0,
        "failed_files": 0,
        "metadata_failures": 0,
        "missing_metadata": 0,
        "skipped_unsafe": 0,
        "changed_before_cleanup": 0,
    }


def _cleanup_persisted_asset_groups(
    *,
    upload_dir: str,
    prefix: str,
    hours: int,
    dry_run: bool,
    classifier: Callable[[dict, Path, datetime], str],
    lock_factory: Callable[[str, str], ContextManager],
    now: datetime | None = None,
) -> dict:
    summary = _new_group_summary()
    root = Path(upload_dir)
    if not root.is_dir():
        summary["exists"] = False
        return summary
    summary["exists"] = True
    now = now or _utc_now()
    cutoff = now - timedelta(hours=hours)

    for entry in os.scandir(root):
        if not entry.is_dir(follow_symlinks=False) or not entry.name.startswith(prefix):
            continue
        if entry.is_symlink():
            summary["skipped_unsafe"] += 1
            continue
        group_id = entry.name[len(prefix):]
        if not _SAFE_GROUP_ID.fullmatch(group_id):
            summary["skipped_unsafe"] += 1
            continue
        group_root = Path(entry.path)
        meta_path = group_root / "meta.json"
        if not meta_path.is_file() or meta_path.is_symlink():
            summary["missing_metadata"] += 1
            continue
        summary["scanned"] += 1

        try:
            with open(meta_path, "r", encoding="utf-8") as handle:
                initial_meta = json.load(handle)
            if not isinstance(initial_meta, dict):
                raise ValueError("metadata root must be an object")
            initial_state = classifier(initial_meta, meta_path, cutoff)
        except (OSError, ValueError, json.JSONDecodeError):
            summary["metadata_failures"] += 1
            continue

        if initial_state != "eligible":
            summary[initial_state] += 1
            continue

        context = nullcontext() if dry_run else lock_factory(group_id, upload_dir)
        try:
            with context:
                if dry_run:
                    meta = initial_meta
                else:
                    with open(meta_path, "r", encoding="utf-8") as handle:
                        meta = json.load(handle)
                    if not isinstance(meta, dict):
                        raise ValueError("metadata root must be an object")
                    if classifier(meta, meta_path, cutoff) != "eligible":
                        summary["changed_before_cleanup"] += 1
                        continue

                assets, skipped_unsafe = _raw_asset_paths(group_root)
                summary["skipped_unsafe"] += skipped_unsafe
                summary["candidates"] += 1
                summary["would_delete_files"] += len(assets)
                if dry_run:
                    continue

                deleted = 0
                failed = 0
                for asset in assets:
                    try:
                        asset.unlink()
                        deleted += 1
                    except OSError:
                        failed += 1
                summary["deleted_files"] += deleted
                summary["failed_files"] += failed
                if failed:
                    continue

                _prune_empty_directories(group_root)
                _mark_assets_expired(
                    meta,
                    group_root,
                    hours=hours,
                    deleted_files=deleted,
                    expired_at=now,
                )
                _write_json_atomic(meta_path, meta)
                summary["expired_groups"] += 1
        except (BatchOperationBusy, AnswerSheetOperationBusy):
            summary["busy"] += 1
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            summary["metadata_failures"] += 1

    summary["retention_hours"] = hours
    return summary


def cleanup_quick_batch_images(
    hours: int,
    dry_run: bool,
    *,
    upload_dir: str | None = None,
    now: datetime | None = None,
) -> dict:
    return _cleanup_persisted_asset_groups(
        upload_dir=upload_dir or UPLOAD_DIR,
        prefix="quick_batch_",
        hours=hours,
        dry_run=dry_run,
        classifier=_classify_quick_batch,
        lock_factory=batch_operation_lock,
        now=now,
    )


def cleanup_answer_sheet_images(
    hours: int,
    dry_run: bool,
    *,
    upload_dir: str | None = None,
    now: datetime | None = None,
) -> dict:
    return _cleanup_persisted_asset_groups(
        upload_dir=upload_dir or UPLOAD_DIR,
        prefix="answer_sheet_",
        hours=hours,
        dry_run=dry_run,
        classifier=_classify_answer_sheet,
        lock_factory=answer_sheet_operation_lock,
        now=now,
    )


def _cleanup_untracked_image_tree(
    root_dir: str,
    hours: int,
    dry_run: bool,
    *,
    remove_reason_sidecars: bool = False,
    now: datetime | None = None,
) -> dict:
    summary = {
        "exists": os.path.isdir(root_dir),
        "would_delete_files": 0,
        "deleted_files": 0,
        "deleted_sidecars": 0,
        "failed": 0,
        "skipped_unsafe": 0,
        "retention_hours": hours,
    }
    if not summary["exists"]:
        return summary
    now = now or _utc_now()
    cutoff_ts = (now - timedelta(hours=hours)).timestamp()
    root = Path(root_dir).resolve()
    assets, skipped_unsafe = _raw_asset_paths(root)
    summary["skipped_unsafe"] += skipped_unsafe
    for asset in assets:
        try:
            if asset.stat().st_mtime >= cutoff_ts:
                continue
            summary["would_delete_files"] += 1
            if dry_run:
                continue
            asset.unlink()
            summary["deleted_files"] += 1
            if remove_reason_sidecars:
                sidecar = Path(str(asset) + ".txt")
                try:
                    sidecar.resolve(strict=False).relative_to(root)
                    if sidecar.is_file() and not sidecar.is_symlink():
                        sidecar.unlink()
                        summary["deleted_sidecars"] += 1
                except (OSError, RuntimeError, ValueError):
                    summary["failed"] += 1
        except OSError:
            summary["failed"] += 1
    if not dry_run:
        _prune_empty_directories(root)
    return summary


def cleanup_pending_images(hours: int, dry_run: bool, *, now: datetime | None = None) -> dict:
    return _cleanup_untracked_image_tree(PENDING_DIR, hours, dry_run, now=now)


def cleanup_failed_images(hours: int, dry_run: bool, *, now: datetime | None = None) -> dict:
    return _cleanup_untracked_image_tree(
        FAILED_DIR,
        hours,
        dry_run,
        remove_reason_sidecars=True,
        now=now,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="清理过期生成文件与学生试卷图像")
    parser.add_argument(
        "--output-hours",
        type=int,
        default=24,
        help="output/ 生成文件保留小时数（默认 24）",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=36,
        help="学生试卷图像在终态后的保留小时数（默认 36）",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="兼容旧调度：以天指定生成文件保留期",
    )
    parser.add_argument("--dry-run", action="store_true", help="只统计候选项，不删除也不改写元数据")
    args = parser.parse_args()
    output_hours = args.days * 24 if args.days is not None else args.output_hours
    if output_hours <= 0 or args.hours <= 0:
        parser.error("保留时长必须大于 0")

    now = _utc_now()
    summary = {
        "started_at": now.isoformat(timespec="seconds"),
        "dry_run": args.dry_run,
        "policy": {
            "generated_file_hours": output_hours,
            "raw_paper_hours_after_terminal_state": args.hours,
            "structured_records": "retained",
        },
        "generated_files": None,
        "quick_batches": None,
        "answer_sheet_sessions": None,
        "pending_images": None,
        "failed_import_images": None,
    }

    db = SessionLocal()
    try:
        if args.dry_run:
            from models import GeneratedFile

            cutoff = datetime.now() - timedelta(hours=output_hours)
            count = db.query(GeneratedFile).filter(GeneratedFile.created_at < cutoff).count()
            summary["generated_files"] = {
                "would_delete_rows": count,
                "retention_hours": output_hours,
            }
        else:
            result = cleanup_generated_files(db, older_than_days=output_hours / 24)
            db.commit()
            result["retention_hours"] = output_hours
            summary["generated_files"] = result
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        summary["generated_files"] = {
            "error": type(exc).__name__,
            "retention_hours": output_hours,
        }
    finally:
        db.close()

    summary["quick_batches"] = cleanup_quick_batch_images(
        args.hours, args.dry_run, now=now,
    )
    summary["answer_sheet_sessions"] = cleanup_answer_sheet_images(
        args.hours, args.dry_run, now=now,
    )
    summary["pending_images"] = cleanup_pending_images(args.hours, args.dry_run, now=now)
    summary["failed_import_images"] = cleanup_failed_images(args.hours, args.dry_run, now=now)

    print(json.dumps(summary, ensure_ascii=False, indent=2))

    generated = summary["generated_files"] or {}
    group_summaries = [summary["quick_batches"], summary["answer_sheet_sessions"]]
    metadata_failures = sum(int(item.get("metadata_failures") or 0) for item in group_summaries)
    failed_files = int(generated.get("failed_files") or 0)
    failed_files += sum(int(item.get("failed_files") or 0) for item in group_summaries)
    failed_files += int(summary["pending_images"].get("failed") or 0)
    failed_files += int(summary["failed_import_images"].get("failed") or 0)
    if generated.get("error") or metadata_failures:
        return 2
    return 1 if failed_files else 0


if __name__ == "__main__":
    sys.exit(main())
