"""A session's files on disk: stored uploads, session asset paths, and their removal.

- ``_save_images`` streams uploads into the session within the count and size
  limits (``_ALLOWED_EXTENSIONS``), validates each image and records its hash; a
  failed upload removes every file it started (``_remove_upload_path``).  The
  ``_CLIENT_OCR_READY_*`` constants name the browser's OCR-ready upload format;
  no code reads them.
- ``_session_asset_path`` resolves a stored path only while it stays inside the
  session directory; ``_layout_asset_paths`` and ``_remove_layout_assets`` find
  and delete the files a replaced layout leaves behind.
- ``_require_raw_assets_available`` answers 410 once the privacy policy has
  removed the raw papers (``_RAW_PAPER_EXPIRED_MESSAGE``).

Moved unchanged from routers/answer_sheet.py, which still exposes these names.
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile

from config import MAX_UPLOAD_FILES, MAX_UPLOAD_FILE_SIZE, MAX_UPLOAD_TOTAL_SIZE
from routers.answer_sheet_common import _run_answer_sheet_image_work
from services.answer_sheet_session import AnswerSheetSession
from services.paper_orientation import sha256_file
from services.preprocess import ocr_ready_storage_suffix
from services.security import stream_to_disk_capped, validate_upload_path
from services.system_readiness import get_upload_storage_readiness_async

# routers.answer_sheet's logger, so these records keep their logger name.
logger = logging.getLogger("routers.answer_sheet")
_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png"}
_CLIENT_OCR_READY_SUFFIX = ".ocr-ready-v1.jpg"
_CLIENT_OCR_READY_MAX_EDGE = 1920


_RAW_PAPER_EXPIRED_MESSAGE = "原卷已按隐私保留策略清理，错题、得分和正确率记录仍保留"


def _remove_upload_path(path: str | os.PathLike[str] | None) -> None:
    """Best-effort removal for a file that may have been only partly written."""
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        logger.warning("failed to remove incomplete upload asset")


async def _save_images(
    files: list[UploadFile],
    target: str,
    prefix: str,
    *,
    total_byte_budget: int | None = None,
) -> list[dict]:
    if not files:
        raise HTTPException(400, detail="未上传图片")
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(413, detail=f"单次最多上传 {MAX_UPLOAD_FILES} 个文件")
    if total_byte_budget is None:
        total_byte_budget = MAX_UPLOAD_TOTAL_SIZE
    if total_byte_budget <= 0 or total_byte_budget > MAX_UPLOAD_TOTAL_SIZE:
        raise HTTPException(413, detail="上传文件总大小超过限制")
    storage = await get_upload_storage_readiness_async(
        target,
        min(total_byte_budget, len(files) * MAX_UPLOAD_FILE_SIZE),
        required_copies=1,
    )
    if not storage.get("ready"):
        raise HTTPException(
            503,
            detail={
                "error": "SERVER_STORAGE_UNAVAILABLE",
                "message": "服务器存储空间不足或不可写，当前无法接收新的答题卡批阅任务。",
            },
        )
    os.makedirs(target, exist_ok=True)
    saved: list[dict] = []
    attempted_paths: list[str] = []
    total_bytes = 0
    completed = False
    try:
        for index, file in enumerate(files, 1):
            suffix = Path(file.filename or "").suffix.lower()
            if suffix not in _ALLOWED_EXTENSIONS:
                raise HTTPException(400, detail="仅支持 JPG、JPEG、PNG 图片")
            remaining_bytes = total_byte_budget - total_bytes
            if remaining_bytes <= 0:
                raise HTTPException(413, detail="上传文件总大小超过限制")
            storage_suffix = (
                ocr_ready_storage_suffix(file.filename or "") or suffix
            )
            name = f"{prefix}_{index}_{uuid.uuid4().hex}{storage_suffix}"
            path = os.path.join(target, name)
            attempted_paths.append(path)
            byte_limit = min(MAX_UPLOAD_FILE_SIZE, remaining_bytes)
            size = await stream_to_disk_capped(file, path, byte_limit)
            if size < 0:
                _remove_upload_path(path)
                if remaining_bytes < MAX_UPLOAD_FILE_SIZE:
                    raise HTTPException(413, detail="上传文件总大小超过限制")
                raise HTTPException(413, detail="单个上传文件大小超过限制")
            if size == 0:
                raise HTTPException(400, detail="上传图片为空")
            validation = await _run_answer_sheet_image_work(
                validate_upload_path,
                file.filename or f"upload{suffix}",
                path,
            )
            if not validation.get("safe"):
                raise HTTPException(
                    400,
                    detail=validation.get("reason") or "图片校验失败",
                )
            digest = await _run_answer_sheet_image_work(sha256_file, path)
            saved.append({
                "page_id": f"page_{index}",
                "index": index - 1,
                "path": path,
                "sha256": digest,
                "filename": file.filename or name,
            })
            total_bytes += size
        completed = True
    finally:
        if not completed:
            for path in attempted_paths:
                _remove_upload_path(path)
    return saved


def _session_asset_path(root: Path, value: object, *, relative: bool) -> Path | None:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    try:
        candidate = (root / value if relative else Path(value)).resolve(strict=False)
        candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate if candidate != root else None


def _require_raw_assets_available(session: AnswerSheetSession) -> None:
    lifecycle = session.asset_lifecycle if isinstance(session.asset_lifecycle, dict) else {}
    if lifecycle.get("status") == "expired":
        raise HTTPException(410, detail=_RAW_PAPER_EXPIRED_MESSAGE)


def _layout_asset_paths(
    session_dir: str,
    pages: list[dict],
    artifacts: dict[str, dict],
) -> set[Path]:
    from services.answer_sheet_page_registration import registered_image_path

    root = Path(session_dir).resolve()
    paths: set[Path] = set()
    for item in [*pages, *artifacts.values()]:
        if not isinstance(item, dict):
            continue
        for field in ("path", "working_path", "orientation_working_path"):
            candidate = _session_asset_path(root, item.get(field), relative=False)
            if candidate is not None:
                paths.add(candidate)
        template_image = _session_asset_path(
            root,
            item.get("template_image"),
            relative=True,
        )
        if template_image is not None:
            paths.add(template_image)
        canonicalization = item.get("canonicalization")
        if isinstance(canonicalization, dict):
            source_image = _session_asset_path(
                root,
                canonicalization.get("source_image"),
                relative=True,
            )
            if source_image is not None:
                paths.add(source_image)
        registered = item.get("registered_image")
        if isinstance(registered, dict) and registered.get("file"):
            try:
                paths.add(registered_image_path(root, registered))
            except ValueError:
                pass
    return paths


def _remove_layout_assets(
    session_dir: str,
    pages: list[dict],
    artifacts: dict[str, dict],
    *,
    preserve_pages: list[dict] | None = None,
    preserve_artifacts: dict[str, dict] | None = None,
) -> None:
    root = Path(session_dir).resolve()
    paths = _layout_asset_paths(session_dir, pages, artifacts)
    preserved = _layout_asset_paths(
        session_dir,
        preserve_pages or [],
        preserve_artifacts or {},
    )
    removed_parents: set[Path] = set()
    for path in paths - preserved:
        try:
            path.unlink(missing_ok=True)
            removed_parents.add(path.parent)
        except OSError:
            continue
    for parent in sorted(removed_parents, key=lambda item: len(item.parts), reverse=True):
        while parent != root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
