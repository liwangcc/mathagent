"""Student and template uploads of the batch-grading routes: limits, saving, contracts.

- ``_check_limits_or_raise``: the teacher's daily allowance of student images.
- ``_save_batch_uploads``: stream an upload to disk within the size and count
  limits, hashing it on the way, and check the browser's ``.ocr-ready-v1.jpg``
  contract (``_validate_client_ocr_ready_upload``) from its headers alone.
- ``_client_ocr_ready_orientation`` / ``_orientation_diagnostics``: the
  orientation fields an upload carries; the server never guesses orientation.
- ``_resolve_safe_image_path``: a caller-supplied path, only inside UPLOAD_DIR.

Moved unchanged from routers/grade.py, which still exposes these names.
"""

import asyncio
import hashlib
import os
import uuid

from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session

from config import (
    MAX_UPLOAD_FILES,
    MAX_UPLOAD_FILE_SIZE,
    MAX_UPLOAD_TOTAL_SIZE,
    UPLOAD_DIR,
)
from services.paper_orientation import sha256_file
from services.preprocess import ocr_ready_storage_suffix
from services.rate_limiter import RateLimiter
from services.security import stream_to_disk_capped, validate_upload_path
from services.system_readiness import get_upload_storage_readiness_async


# ── Daily image allowance ────────────────────────────────────────────────
def _check_limits_or_raise(
    db: Session,
    user_id: int,
    requested_images: int = 0,
) -> "RateLimiter":
    """Check the natural-day original student-image allowance."""
    limiter = RateLimiter(db)
    daily = limiter.check_daily_image_limit(user_id, requested_images)
    if not daily["allowed"]:
        raise HTTPException(403, detail={
            "error": "DAILY_IMAGE_LIMIT_EXCEEDED",
            "message": (
                f"今日学生试卷图片额度不足（已用 {daily['current']} / "
                f"{daily['limit']} 张，本次 {daily['requested']} 张）"
            ),
            "current": daily["current"],
            "limit": daily["limit"],
            "requested": daily["requested"],
            "remaining": daily["remaining"],
            "reset_time": daily["reset_time"],
        })
    return limiter
# ── Saving uploads ───────────────────────────────────────────────────────
def _remove_batch_uploads(saved_uploads: list[tuple[str, str]]) -> None:
    for item in saved_uploads:
        path = item[1]
        try:
            os.remove(path)
        except OSError:
            pass


async def _stream_to_disk_capped_with_sha256(
    upload: UploadFile,
    destination: str,
    max_bytes: int,
) -> tuple[int, str]:
    """Persist one upload and compute its identity in the same bounded pass."""
    digest = hashlib.sha256()
    total = 0
    output = await asyncio.to_thread(open, destination, "wb")
    try:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                return -1, ""
            digest.update(chunk)
            await asyncio.to_thread(output.write, chunk)
    finally:
        await asyncio.to_thread(output.close)
    return total, digest.hexdigest()


async def _save_batch_uploads(
    files: list[UploadFile],
    destination_dir: str,
    *,
    total_byte_budget: int | None = None,
    require_client_ocr_ready: bool = False,
    capture_sha256: bool = False,
) -> list[tuple]:
    """Persist a grade/template batch under count, per-file, and total limits.

    The generic upload endpoint already enforces these limits.  Grade and
    template imports must use the same server-side gate rather than relying on
    nginx's request cap or client-side validation.  Any rejected batch removes
    all files already written for that request.
    """
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(
            413,
            detail=f"单次最多上传 {MAX_UPLOAD_FILES} 个文件",
        )

    if total_byte_budget is None:
        total_byte_budget = MAX_UPLOAD_TOTAL_SIZE
    if total_byte_budget <= 0 or total_byte_budget > MAX_UPLOAD_TOTAL_SIZE:
        raise HTTPException(413, detail="上传文件总大小超过限制")
    storage = await get_upload_storage_readiness_async(
        destination_dir,
        total_byte_budget,
        required_copies=1,
    )
    if not storage.get("ready"):
        raise HTTPException(
            503,
            detail={
                "error": "SERVER_STORAGE_UNAVAILABLE",
                "message": "服务器存储空间不足或不可写，当前无法接收新的批改任务。",
            },
        )
    saved_uploads: list[tuple] = []
    total_bytes = 0
    current_path = ""
    try:
        for upload in files:
            filename = upload.filename or "unknown"
            if os.path.splitext(filename)[1].lower() == ".pdf":
                raise HTTPException(
                    400,
                    detail="批改流程暂不支持 PDF，请先将每页转换为 JPG、PNG 或 WebP 图片",
                )
            if (
                require_client_ocr_ready
                and not filename.lower().endswith(".ocr-ready-v1.jpg")
            ):
                raise HTTPException(
                    400,
                    detail={
                        "error": "CLIENT_IMAGE_PREPROCESS_REQUIRED",
                        "message": (
                            f"{filename} 未完成浏览器图片处理；服务器不会回退解码，"
                            "请重新选择该图片后再提交"
                        ),
                    },
                )
            remaining_bytes = total_byte_budget - total_bytes
            if remaining_bytes <= 0:
                raise HTTPException(413, detail="上传文件总大小超过限制")

            ext = ocr_ready_storage_suffix(filename) or ".png"
            current_path = os.path.join(destination_dir, f"{uuid.uuid4().hex}{ext}")
            max_bytes = min(MAX_UPLOAD_FILE_SIZE, remaining_bytes)
            if capture_sha256:
                size, content_sha256 = await _stream_to_disk_capped_with_sha256(
                    upload,
                    current_path,
                    max_bytes,
                )
            else:
                size = await stream_to_disk_capped(upload, current_path, max_bytes)
                content_sha256 = ""
            if size < 0:
                try:
                    os.remove(current_path)
                except OSError:
                    pass
                current_path = ""
                if remaining_bytes < MAX_UPLOAD_FILE_SIZE:
                    raise HTTPException(413, detail="上传文件总大小超过限制")
                raise HTTPException(413, detail=f"{filename} 超过 20MB 限制")

            validation = await asyncio.to_thread(
                (
                    _validate_client_ocr_ready_upload
                    if require_client_ocr_ready
                    else validate_upload_path
                ),
                filename,
                current_path,
            )
            if not validation["safe"]:
                try:
                    os.remove(current_path)
                except OSError:
                    pass
                current_path = ""
                raise HTTPException(
                    400,
                    detail=f"文件安全检查失败: {validation['reason']}",
                )

            saved_uploads.append(
                (filename, current_path, content_sha256)
                if capture_sha256
                else (filename, current_path)
            )
            current_path = ""
            total_bytes += size
    except Exception:
        if current_path:
            try:
                os.remove(current_path)
            except OSError:
                pass
        _remove_batch_uploads(saved_uploads)
        raise

    return saved_uploads


def _hash_saved_uploads(original_path: str, working_path: str) -> tuple[str, str]:
    """Hash source and working artifacts only; do not decode either image."""
    source_hash = sha256_file(original_path)
    working_hash = (
        source_hash
        if os.path.realpath(original_path) == os.path.realpath(working_path)
        else sha256_file(working_path)
    )
    return source_hash, working_hash
# ── The browser's .ocr-ready-v1.jpg contract ─────────────────────────────
def _jpeg_dimensions(prefix: bytes) -> tuple[int, int] | None:
    """Read JPEG dimensions from bounded headers without decoding pixels."""
    if not prefix.startswith(b"\xff\xd8\xff"):
        return None
    offset = 2
    size = len(prefix)
    while offset + 9 <= size:
        if prefix[offset] != 0xFF:
            offset += 1
            continue
        while offset < size and prefix[offset] == 0xFF:
            offset += 1
        if offset >= size:
            break
        marker = prefix[offset]
        offset += 1
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > size:
            break
        segment_length = int.from_bytes(prefix[offset:offset + 2], "big")
        if segment_length < 2 or offset + segment_length > size:
            break
        if marker in {
            0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
            0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
        }:
            if segment_length < 7:
                return None
            height = int.from_bytes(prefix[offset + 3:offset + 5], "big")
            width = int.from_bytes(prefix[offset + 5:offset + 7], "big")
            return width, height
        offset += segment_length
    return None


def _validate_client_ocr_ready_upload(filename: str, path: str) -> dict:
    """Validate the browser JPEG contract using headers and bounded reads only."""
    safe_name = os.path.basename(str(filename or ""))
    if not safe_name.lower().endswith(".ocr-ready-v1.jpg"):
        return {"safe": False, "reason": "缺少 .ocr-ready-v1.jpg 客户端规范化合同", "sanitized_filename": safe_name}
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as stream:
            prefix = stream.read(64 * 1024)
            stream.seek(max(0, size - 2))
            suffix = stream.read(2)
    except OSError:
        return {"safe": False, "reason": "文件内容为空", "sanitized_filename": safe_name}
    if size <= 0 or size > MAX_UPLOAD_FILE_SIZE:
        return {"safe": False, "reason": "文件超过大小限制", "sanitized_filename": safe_name}
    if not prefix.startswith(b"\xff\xd8\xff") or suffix != b"\xff\xd9":
        return {"safe": False, "reason": "客户端规范化文件不是完整 JPEG", "sanitized_filename": safe_name}
    dimensions = _jpeg_dimensions(prefix)
    if not dimensions or max(dimensions) > 1920 or dimensions[0] <= 0 or dimensions[1] <= 0:
        return {"safe": False, "reason": "客户端规范化 JPEG 尺寸合同无效", "sanitized_filename": safe_name}
    return {"safe": True, "reason": "", "sanitized_filename": safe_name}


def _client_ocr_ready_orientation(filename: str) -> dict:
    """Return the browser canonicalization checkpoint without touching pixels.

    ``.ocr-ready-v1.jpg`` is emitted only after the browser has applied EXIF
    orientation and bounded the canvas dimensions.  The server deliberately
    trusts only this explicit contract; it never guesses orientation locally.
    """
    if str(filename or "").lower().endswith(".ocr-ready-v1.jpg"):
        return {
            "orientation_status": "auto_passed",
            "orientation_reason": "client_ocr_ready_v1",
            "orientation_confidence": 1.0,
            "detected_tilt_degrees": 0.0,
            "exif_orientation": 1,
            "orientation_coordinate_space": "client_normalized_pixels",
        }
    return {
        "orientation_status": "needs_review",
        "orientation_reason": "client_canonicalization_required",
        "orientation_confidence": 0.0,
        "detected_tilt_degrees": None,
        "exif_orientation": None,
        "orientation_coordinate_space": "client_normalized_pixels",
    }


_ORIENTATION_DIAGNOSTIC_FIELDS = (
    "orientation_status",
    "orientation_reason",
    "orientation_confidence",
    "detected_tilt_degrees",
    "exif_orientation",
    "orientation_coordinate_space",
)


def _orientation_diagnostics(record: dict) -> dict:
    return {
        field: record.get(field)
        for field in _ORIENTATION_DIAGNOSTIC_FIELDS
        if field in record
    }
# ── Paths inside UPLOAD_DIR ──────────────────────────────────────────────
def _resolve_safe_image_path(image_path: str) -> str:
    """校验用户传入的 image_path 必须落在 UPLOAD_DIR 内，防止路径遍历任意文件读取（H6）。

    返回规范化后的绝对路径；越界或文件不存在时抛 HTTPException。
    """
    if not image_path:
        raise HTTPException(400, detail="image_path 不能为空")
    base = os.path.realpath(UPLOAD_DIR)
    # 用户可能传相对路径（相对 uploads/）或绝对路径，统一拼到 base 下再规范化
    candidate = image_path if os.path.isabs(image_path) else os.path.join(base, image_path)
    full = os.path.realpath(candidate)
    # 必须严格在 base 目录内（用 os.sep 防止 /uploads-evil 这类前缀绕过）
    if full != base and not full.startswith(base + os.sep):
        raise HTTPException(403, detail="非法的图片路径")
    if not os.path.isfile(full):
        raise HTTPException(404, detail="图片文件不存在")
    return full
