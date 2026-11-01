"""安全服务模块 — 文件上传安全验证、文件名净化、恶意内容检测。"""

import asyncio
import logging
import os
import re
from io import BytesIO

logger = logging.getLogger(__name__)

# ── 白名单：允许的文件扩展名 ──
_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".pdf"}

# ── Magic Bytes 签名 ──
_MAGIC_BYTES = {
    "image/jpeg": [b"\xff\xd8\xff"],
    "image/png":  [b"\x89PNG"],
    "image/gif":  [b"GIF87a", b"GIF89a"],
    "application/pdf": [b"%PDF"],
    "image/bmp":  [b"BM"],
    "image/webp": [b"RIFF"],  # RIFF....WEBP, validated further below
}

# ── 扩展名 → 期望的 Magic Bytes 类型 ──
_EXT_TO_MAGIC = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}

# ── 危险内容模式（恶意脚本 / 代码注入） ──
_MALICIOUS_PATTERNS = [
    re.compile(rb"<\s*script", re.IGNORECASE),
    re.compile(rb"<\s*\?php", re.IGNORECASE),
    re.compile(rb"\beval\s*\(", re.IGNORECASE),
    re.compile(rb"\bexec\s*\(", re.IGNORECASE),
    re.compile(rb"\bsystem\s*\(", re.IGNORECASE),
    re.compile(rb"\bshell_exec\s*\(", re.IGNORECASE),
    re.compile(rb"\bpassthru\s*\(", re.IGNORECASE),
    re.compile(rb"\bbase64_decode\s*\(", re.IGNORECASE),
    re.compile(rb"<\s*iframe", re.IGNORECASE),
    re.compile(rb"javascript\s*:", re.IGNORECASE),
    re.compile(rb"\balert\s*\(", re.IGNORECASE),
]

# ── 图片最大分辨率 ──
_MAX_WIDTH = 10000
_MAX_HEIGHT = 10000
_MAX_IMAGE_PIXELS = 16_000_000


def sanitize_filename(filename: str) -> str:
    """净化文件名，防止路径遍历和注入。

    1. 提取纯文件名（去除路径部分）
    2. 只保留：字母、数字、中文、下划线、连字符、点
    3. 限制长度 200 字符
    """
    if not filename:
        return "unnamed"

    # 提取纯文件名，去除路径遍历
    name = os.path.basename(filename)
    # 移除 .. 序列
    name = name.replace("..", "")

    # 分离扩展名
    base, ext = os.path.splitext(name)
    # 只保留安全字符（字母、数字、中文、_、-、空格）
    base = re.sub(r"[^\w\u4e00-\u9fff\- ]", "", base, flags=re.UNICODE)
    base = base.strip()
    if not base:
        base = "unnamed"

    # 限制长度（留出扩展名空间）
    max_base = 200 - len(ext)
    if len(base) > max_base:
        base = base[:max_base]

    return f"{base}{ext}"


def detect_content_type(content: bytes) -> str:
    """通过 Magic Bytes 检测真实文件类型。

    Returns: 'image/jpeg', 'image/png', 'image/gif', 'application/pdf',
             'image/bmp', 'image/webp', 'unknown'
    """
    if not content:
        return "unknown"

    for mime, signatures in _MAGIC_BYTES.items():
        for sig in signatures:
            if content[:len(sig)] == sig:
                # WebP: RIFF....WEBP
                if mime == "image/webp":
                    if len(content) >= 12 and content[8:12] == b"WEBP":
                        return "image/webp"
                    continue
                return mime
    return "unknown"


def _validate_upload_metadata(
    filename: str,
    content_prefix: bytes,
    content_size: int,
    image_source,
) -> dict:
    """Validate one upload from bounded metadata and an image header source."""
    # 1. 文件名净化
    safe_name = sanitize_filename(filename)

    # 2. 扩展名白名单
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in _ALLOWED_EXTENSIONS:
        return {
            "safe": False,
            "reason": f"不允许的文件类型: {ext or '(无扩展名)'}，仅支持: {', '.join(sorted(_ALLOWED_EXTENSIONS))}",
            "sanitized_filename": safe_name,
        }

    # 3. 内容不为空
    if not content_prefix or content_size <= 0:
        return {
            "safe": False,
            "reason": "文件内容为空",
            "sanitized_filename": safe_name,
        }

    # 4. 文件大小（再检查一次，防御性编程）
    if content_size > 20 * 1024 * 1024:
        return {
            "safe": False,
            "reason": "文件超过20MB限制",
            "sanitized_filename": safe_name,
        }

    # 5. Magic Bytes 验证真实类型
    detected = detect_content_type(content_prefix)
    expected = _EXT_TO_MAGIC.get(ext)
    if expected and detected != expected:
        # 特殊情况：.jpg/.jpeg 都映射到 image/jpeg，已处理
        return {
            "safe": False,
            "reason": f"文件内容与扩展名不匹配：扩展名 {ext} 期望 {expected}，实际检测为 {detected}",
            "sanitized_filename": safe_name,
        }
    if detected == "unknown":
        return {
            "safe": False,
            "reason": "无法识别的文件类型",
            "sanitized_filename": safe_name,
        }

    # 6. 恶意内容扫描
    for pattern in _MALICIOUS_PATTERNS:
        if pattern.search(content_prefix[:100_000]):  # 扫描前 100KB
            return {
                "safe": False,
                "reason": "文件包含可疑的脚本或可执行代码",
                "sanitized_filename": safe_name,
            }

    # 7. 图片尺寸验证（仅对图片类型）
    if detected.startswith("image/"):
        try:
            from PIL import Image
            with Image.open(image_source) as img:
                w, h = img.size
            if (
                w <= 0
                or h <= 0
                or w > _MAX_WIDTH
                or h > _MAX_HEIGHT
                or w * h > _MAX_IMAGE_PIXELS
            ):
                return {
                    "safe": False,
                    "reason": (
                        f"图片尺寸过大: {w}x{h}，最大允许 "
                        f"{_MAX_IMAGE_PIXELS:,} 像素"
                    ),
                    "sanitized_filename": safe_name,
                }
        except ImportError:
            logger.error("Pillow not installed; rejecting image upload")
            return {
                "safe": False,
                "reason": "服务器无法校验图片尺寸",
                "sanitized_filename": safe_name,
            }
        except Exception as e:
            logger.warning("Image dimension check failed: %s", e)
            return {
                "safe": False,
                "reason": "图片文件损坏或尺寸信息无效",
                "sanitized_filename": safe_name,
            }

    return {"safe": True, "reason": "", "sanitized_filename": safe_name}


def validate_upload_file(filename: str, content: bytes) -> dict:
    """验证内存中的上传文件，保留既有调用合同。"""
    raw = bytes(content or b"")
    return _validate_upload_metadata(
        filename,
        raw[:100_000],
        len(raw),
        BytesIO(raw),
    )


def validate_upload_path(filename: str, path: str | os.PathLike[str]) -> dict:
    """Validate a server-owned upload without reading the whole file into RAM."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as stream:
            prefix = stream.read(100_000)
    except OSError:
        return {
            "safe": False,
            "reason": "文件内容为空",
            "sanitized_filename": sanitize_filename(filename),
        }
    return _validate_upload_metadata(filename, prefix, size, os.fspath(path))


def escape_like(text: str) -> str:
    """转义 SQL LIKE 通配符，防止用户输入的 %/_ 被当作通配符。

    必须配合 `.like(pattern, escape='\\')` 使用。反斜杠本身先转义，
    再转义 % 与 _。避免：① `%`-only 模式触发全表扫描（DoS）；
    ② 用户用通配符越权匹配到本不该命中的记录。
    """
    if not text:
        return ""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def read_upload_capped(upload, max_bytes: int) -> bytes:
    """流式分块读取 UploadFile，超过 max_bytes 即中止并抛 413，防内存耗尽。

    UploadFile.read() 一次性把整文件读进内存，恶意特大文件可打爆进程。
    这里分块累计，超限立即停止读取并报错。
    """
    from fastapi import HTTPException

    chunks = []
    total = 0
    chunk_size = 1024 * 1024  # 1MB
    while True:
        chunk = await upload.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            mb = max_bytes / (1024 * 1024)
            raise HTTPException(413, detail=f"文件超过 {mb:.0f}MB 限制")
        chunks.append(chunk)
    return b"".join(chunks)


async def stream_to_disk_capped(upload, dest_path: str, max_bytes: int) -> int:
    """分块读取上传并写入磁盘，超过 max_bytes 返回 -1。

    调用方负责删除超限或校验失败后的 dest_path。
    """
    total = 0
    chunk_size = 1024 * 1024
    out = await asyncio.to_thread(open, dest_path, "wb")
    try:
        while True:
            chunk = await upload.read(chunk_size)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                return -1
            await asyncio.to_thread(out.write, chunk)
    finally:
        await asyncio.to_thread(out.close)
    return total
