"""图片上传接口"""

import asyncio
import os
import uuid
import logging

from fastapi import APIRouter, UploadFile, File, Depends, HTTPException
from fastapi.responses import JSONResponse

from config import (
    UPLOAD_DIR,
    MAX_UPLOAD_FILE_SIZE,
    MAX_UPLOAD_FILES,
    MAX_UPLOAD_TOTAL_SIZE,
    UPLOAD_CHUNK_SIZE,
)
from models_user import User
from routers.auth import _require_user
from services.security import sanitize_filename, validate_upload_path

router = APIRouter()
logger = logging.getLogger(__name__)
_GENERIC_UPLOAD_ERROR = "文件上传失败，请稍后重试"


def _human_mb(n: int) -> str:
    return f"{n / (1024 * 1024):.0f}MB"


@router.post("/upload/batch")
async def upload_batch(
    files: list[UploadFile] = File(...),
    user: User = Depends(_require_user),
):
    """批量上传试卷图片。

    P1-8：流式分块落盘（不把整文件读进内存），并施加硬限制：
      - 单次文件数 ≤ MAX_UPLOAD_FILES
      - 单文件大小 ≤ MAX_UPLOAD_FILE_SIZE（落盘时边写边计数，超限即中止并删除半成品）
      - 单次总大小 ≤ MAX_UPLOAD_TOTAL_SIZE
    """
    # ── 数量硬限：在落盘任何字节前先拒，避免被超多文件拖垮 ──
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(
            413,
            detail=f"单次最多上传 {MAX_UPLOAD_FILES} 个文件，本次 {len(files)} 个",
        )

    saved = []
    failed = []
    pending_dir = os.path.join(UPLOAD_DIR, "待处理")
    os.makedirs(pending_dir, exist_ok=True)

    total_bytes = 0
    request_paths: list[str] = []
    completed = False
    try:
        for f in files:
            safe_original = sanitize_filename(f.filename or "upload.png")
            ext = os.path.splitext(safe_original)[1].lower() or ".png"
            name = f"{uuid.uuid4()}{ext}"
            path = os.path.join(pending_dir, name)
            # Register before opening so an open/write failure still rolls the
            # path back, including a zero-byte file created by open().
            request_paths.append(path)
            written = 0
            too_big = False
            try:
                # ── 流式分块落盘，单文件超限即中止 ──
                out = await asyncio.to_thread(open, path, "wb")
                try:
                    while True:
                        chunk = await f.read(UPLOAD_CHUNK_SIZE)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > MAX_UPLOAD_FILE_SIZE:
                            too_big = True
                            break
                        if total_bytes + written > MAX_UPLOAD_TOTAL_SIZE:
                            too_big = True
                            break
                        await asyncio.to_thread(out.write, chunk)
                finally:
                    await asyncio.to_thread(out.close)

                if too_big:
                    _safe_remove(path)
                    # 区分单文件超限 vs 总量超限
                    if written > MAX_UPLOAD_FILE_SIZE:
                        failed.append({
                            "filename": safe_original,
                            "error": f"文件超过 {_human_mb(MAX_UPLOAD_FILE_SIZE)} 限制",
                        })
                        continue
                    raise HTTPException(
                        413,
                        detail=f"本次上传总大小超过 {_human_mb(MAX_UPLOAD_TOTAL_SIZE)} 限制",
                    )

                total_bytes += written

                # Validate the bounded, server-owned file without making a
                # second full-size in-memory copy of every uploaded image.
                validation = await asyncio.to_thread(
                    validate_upload_path,
                    f.filename,
                    path,
                )
                if not validation["safe"]:
                    _safe_remove(path)
                    failed.append({"filename": safe_original, "error": f"文件安全检查失败: {validation['reason']}"})
                    continue

                saved.append({"filename": safe_original, "saved_as": name, "path": path})
            except HTTPException:
                # 总量超限：直接向上抛，中止整个批次
                _safe_remove(path)
                raise
            except Exception as e:
                _safe_remove(path)
                logger.warning("Upload failed for %s: %s", f.filename, e, exc_info=True)
                failed.append({"filename": safe_original, "error": _GENERIC_UPLOAD_ERROR})
        completed = True
    finally:
        # A cancelled/disconnected request, or an uncaught exception, must not
        # leave files from earlier items in this batch behind.  Ordinary
        # per-file validation/write failures are converted to `failed` above
        # and therefore still keep their successfully uploaded siblings.
        if not completed:
            for path in request_paths:
                _safe_remove(path)

    return JSONResponse({"saved": saved, "failed": failed, "total": len(saved) + len(failed)})


def _safe_remove(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
