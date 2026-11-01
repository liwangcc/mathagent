"""生成文件登记与归属下载（P0-1/P0-2）。

Hermes 工具生成的可下载文件落盘到 output/ 后，在 generated_files 表登记归属，
下载时按 id 校验 owner。物理名用 uuid（不可猜），友好名走 Content-Disposition。

P0-2：友好下载名统一为 `{类型}_{安全化标识}_{YYYYMMDD_HHMMSS}.{ext}`，
由 make_display_name 集中生成，所有调用方共用，避免命名风格各异 + 同名覆盖。
"""

import os
import re
import time
import uuid
from datetime import datetime, timedelta
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import FileResponse

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")

_MIME_BY_EXT = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".pdf": "application/pdf",
    ".zip": "application/zip",
}


def _safe_segment(s: str) -> str:
    """文件名片段安全化：去路径分隔/控制字符/空白，空值回退占位。"""
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", (s or "")).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned or "未命名"


def make_display_name(kind: str, label: str, ext: str) -> str:
    """统一友好下载名：`{类型}_{安全化标识}_{时间戳}.{ext}`（P0-2）。

    - kind:  文件类型，如 "班级报告"/"个人报告"/"致家长信"/"错题条"/"成绩表"。
    - label: 区分标识，如班级名/学生名；空则省略该段。
    - ext:   扩展名，带不带点都可，如 "docx" 或 ".docx"。
    时间戳精确到秒，杜绝同类同名文件在下载目录里相互覆盖。
    """
    ext = ext if ext.startswith(".") else f".{ext}"
    ts = time.strftime("%Y%m%d_%H%M%S")
    parts = [_safe_segment(kind)]
    seg = _safe_segment(label)
    if label and seg != "未命名":
        parts.append(seg)
    parts.append(ts)
    return "_".join(parts) + ext


def register_generated_file(db, owner_id: int, data: bytes, display_name: str) -> "object":
    """把字节写入 output/ 下的 uuid 文件并登记归属，返回 GeneratedFile（已 flush，含 id）。

    db: 调用方提供的会话（由调用方负责 commit/close）。
    display_name: 友好下载名，建议用 make_display_name 生成（P0-2）。
    """
    from models import GeneratedFile

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ext = os.path.splitext(display_name)[1].lower()
    if ext not in _MIME_BY_EXT:
        raise ValueError(f"不支持的生成文件类型: {ext or '(无扩展名)'}")
    stored_name = f"{uuid.uuid4().hex}{ext}"
    path = os.path.join(OUTPUT_DIR, stored_name)
    with open(path, "wb") as f:
        f.write(data)

    rec = GeneratedFile(
        owner_id=owner_id,
        stored_name=stored_name,
        display_name=display_name,
        mime=_MIME_BY_EXT.get(ext, "application/octet-stream"),
    )
    db.add(rec)
    db.flush()  # 拿到 rec.id
    return rec


def _as_bytes(data) -> bytes:
    if isinstance(data, bytes):
        return data
    if isinstance(data, bytearray):
        return bytes(data)
    if hasattr(data, "getvalue"):
        return data.getvalue()
    if hasattr(data, "read"):
        try:
            data.seek(0)
        except Exception:
            pass
        return data.read()
    raise TypeError("生成文件内容必须是 bytes 或 file-like 对象")


def _content_disposition(display_name: str) -> str:
    return f"attachment; filename*=UTF-8''{quote(display_name)}"


def register_and_respond(db, owner_id: int, data, kind: str, label: str, ext: str):
    """登记 GeneratedFile 后直接返回文件响应，保持前端一键下载体验。"""
    display_name = make_display_name(kind, label, ext)
    rec = register_generated_file(db, owner_id, _as_bytes(data), display_name)
    db.commit()
    path = _safe_output_path(rec.stored_name)
    if not path or not os.path.exists(path):
        raise HTTPException(404, detail="文件不存在或已被清理")
    return FileResponse(
        path,
        media_type=rec.mime,
        headers={"Content-Disposition": _content_disposition(rec.display_name or rec.stored_name)},
    )


def resolve_owned_file(db, file_id: int, user_id: int):
    """按 GeneratedFile.id 校验归属并返回 (file_path, rec)。非 owner 统一 404。"""
    from models import GeneratedFile

    rec = db.query(GeneratedFile).filter(GeneratedFile.id == file_id).first()
    if not rec or rec.owner_id != user_id:
        raise HTTPException(404, detail="文件不存在或已被清理")
    path = _safe_output_path(rec.stored_name)
    if not path:
        raise HTTPException(403, detail="非法文件路径")
    if not os.path.exists(path):
        raise HTTPException(404, detail="文件不存在或已被清理")
    return path, rec


def owned_file_response(db, file_id: int, user_id: int):
    """返回已校验归属的 GeneratedFile 下载响应。"""
    path, rec = resolve_owned_file(db, file_id, user_id)
    return FileResponse(
        path,
        media_type=rec.mime,
        headers={"Content-Disposition": _content_disposition(rec.display_name or rec.stored_name)},
    )


def _safe_output_path(stored_name: str) -> str | None:
    output_root = os.path.realpath(OUTPUT_DIR)
    path = os.path.realpath(os.path.join(output_root, stored_name or ""))
    if path == output_root or not path.startswith(output_root + os.sep):
        return None
    return path


def cleanup_generated_files(db, older_than_days: int = 30, include_orphans: bool = False) -> dict:
    """清理过期生成文件。

    删除对象以 generated_files 表为准；孤儿文件默认不动，避免误删刚写入但尚未登记的文件。
    调用方负责 commit/rollback。
    """
    from models import GeneratedFile

    cutoff = datetime.now() - timedelta(days=older_than_days)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    deleted_rows = 0
    deleted_files = 0
    failed_files = 0
    missing_files = 0
    skipped_unsafe = 0

    rows = db.query(GeneratedFile).filter(GeneratedFile.created_at < cutoff).all()
    for rec in rows:
        path = _safe_output_path(rec.stored_name)
        if not path:
            skipped_unsafe += 1
        elif os.path.exists(path):
            try:
                os.remove(path)
                deleted_files += 1
            except OSError:
                failed_files += 1
                continue
        else:
            missing_files += 1
        db.delete(rec)
        deleted_rows += 1

    orphan_files = 0
    if include_orphans:
        known_names = {name for (name,) in db.query(GeneratedFile.stored_name).all()}
        cutoff_ts = cutoff.timestamp()
        for name in os.listdir(OUTPUT_DIR):
            path = _safe_output_path(name)
            if not path or name in known_names or not os.path.isfile(path):
                continue
            try:
                if os.path.getmtime(path) >= cutoff_ts:
                    continue
                os.remove(path)
                orphan_files += 1
            except OSError:
                pass

    return {
        "cutoff": cutoff.isoformat(timespec="seconds"),
        "deleted_rows": deleted_rows,
        "deleted_files": deleted_files,
        "failed_files": failed_files,
        "missing_files": missing_files,
        "skipped_unsafe": skipped_unsafe,
        "orphan_files": orphan_files,
    }
