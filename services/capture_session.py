"""Phone capture sessions for the desktop grading page.

A teacher opens a capture session from the grading page; a phone scans the QR
code and uploads paper photos into it, and the desktop page imports them into
the normal grading flow.  Sessions are file-backed under
``UPLOAD_DIR/capture_sessions`` so they need no schema change and stay
consistent across Uvicorn workers.

The phone never logs in.  It holds a random token scoped to one session; only
the token's SHA-256 is stored.  The token can add and delete images but cannot
read any image back -- viewing and importing require the owning teacher's login.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, BinaryIO, Iterator

import config

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = 3 * 60 * 60
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_IMAGE_PIXELS = 30_000_000
MIN_IMAGE_EDGE = 200
LOCK_TIMEOUT_SECONDS = 5.0

_SESSION_ID_RE = re.compile(r"^[a-f0-9]{24}$")
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
# Accepted photo formats (Pillow names) and how stored files are served.
IMAGE_EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
IMAGE_MEDIA_TYPES = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
_MANIFEST = "manifest.json"

_MSG_SESSION_GONE = "拍摄会话不存在或已结束"
_MSG_SESSION_EXPIRED = "拍摄会话已过期，请在电脑上重新打开二维码"
_MSG_BAD_LINK = "拍摄链接无效或已被替换，请在电脑上重新扫码"
_MSG_IMAGE_GONE = "该照片不存在或已删除"
_MSG_BUSY = "拍摄服务繁忙，请稍后重试"


class CaptureError(Exception):
    """Base class; ``message`` is safe to show to the teacher."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class CaptureNotFound(CaptureError):
    pass


class CaptureForbidden(CaptureError):
    pass


class CaptureFull(CaptureError):
    pass


class CaptureInvalidImage(CaptureError):
    pass


class CaptureBusy(CaptureError):
    pass


def _max_images() -> int:
    return int(config.MAX_UPLOAD_FILES)


def _root() -> str:
    return os.path.abspath(os.path.join(config.UPLOAD_DIR, "capture_sessions"))


def _session_ids() -> list[str]:
    root = _root()
    if not os.path.isdir(root):
        return []
    return sorted(name for name in os.listdir(root) if _SESSION_ID_RE.fullmatch(name))


def _session_dir(session_id: str) -> str:
    if not _SESSION_ID_RE.fullmatch(str(session_id or "")):
        raise CaptureNotFound(_MSG_SESSION_GONE)
    return os.path.join(_root(), session_id)


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()


# ── Store-wide lock ──

if os.name == "nt":
    import msvcrt

    def _try_lock_file(handle: BinaryIO) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock_file(handle: BinaryIO) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock_file(handle: BinaryIO) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_file(handle: BinaryIO) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


_local_lock = threading.Lock()


def _lock_file(handle: BinaryIO) -> None:
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            _try_lock_file(handle)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise CaptureBusy(_MSG_BUSY) from None
            time.sleep(0.05)


@contextmanager
def _capture_lock() -> Iterator[None]:
    """Serialize manifest changes across threads and Uvicorn workers.

    Writes are tiny and rare (one photo every few seconds per teacher), so one
    store-wide lock keeps find-or-create and sequence allocation atomic.
    """
    if not _local_lock.acquire(timeout=LOCK_TIMEOUT_SECONDS):
        raise CaptureBusy(_MSG_BUSY)
    try:
        os.makedirs(_root(), exist_ok=True)
        with open(os.path.join(_root(), ".lock"), "a+b") as handle:
            if handle.seek(0, os.SEEK_END) == 0:
                # msvcrt locks a byte of the file, so there must be one.
                handle.write(b"0")
                handle.flush()
            _lock_file(handle)
            try:
                yield
            finally:
                try:
                    _unlock_file(handle)
                except OSError:
                    logger.warning("failed to unlock capture session store")
    finally:
        _local_lock.release()


# ── Manifests ──


def _load(session_id: str) -> dict[str, Any] | None:
    path = os.path.join(_session_dir(session_id), _MANIFEST)
    try:
        with open(path, encoding="utf-8") as stream:
            manifest = json.load(stream)
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict) or manifest.get("session_id") != session_id:
        return None
    return manifest


def _save(manifest: dict[str, Any]) -> None:
    target = os.path.join(_session_dir(manifest["session_id"]), _MANIFEST)
    temporary = f"{target}.{secrets.token_hex(4)}.tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False)
    os.replace(temporary, target)


def _remove_session_dir(session_id: str) -> None:
    shutil.rmtree(_session_dir(session_id), ignore_errors=True)


def _expired(manifest: dict[str, Any], now: float) -> bool:
    return float(manifest.get("expires_at") or 0) <= now


def _require_unexpired(manifest: dict[str, Any], now: float) -> None:
    """Expired sessions are deleted on sight."""
    if _expired(manifest, now):
        _remove_session_dir(manifest["session_id"])
        raise CaptureNotFound(_MSG_SESSION_EXPIRED)


def _is_owner(manifest: dict[str, Any], teacher_id: int) -> bool:
    return int(manifest.get("teacher_id") or 0) == int(teacher_id)


def _owned_manifest(session_id: str, teacher_id: int, now: float) -> dict[str, Any]:
    manifest = _load(session_id)
    if manifest is None or not _is_owner(manifest, teacher_id):
        # Same answer as a missing session: never confirm another teacher's id.
        raise CaptureNotFound(_MSG_SESSION_GONE)
    _require_unexpired(manifest, now)
    return manifest


def _token_manifest(session_id: str, token: str, now: float) -> dict[str, Any]:
    valid_id = _SESSION_ID_RE.fullmatch(str(session_id or ""))
    manifest = _load(session_id) if token and valid_id else None
    if manifest is None or not hmac.compare_digest(_token_hash(token), str(manifest.get("token_sha256") or "")):
        raise CaptureForbidden(_MSG_BAD_LINK)
    _require_unexpired(manifest, now)
    return manifest


def _image_view(image: dict[str, Any]) -> dict[str, Any]:
    return {
        "seq": int(image["seq"]),
        "client_id": image.get("client_id") or None,
        "bytes": int(image.get("bytes") or 0),
        "width": int(image.get("width") or 0),
        "height": int(image.get("height") or 0),
        "created_at": _iso(image.get("created_at")),
    }


def _session_view(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": manifest["session_id"],
        "status": "active",
        "created_at": _iso(manifest.get("created_at")),
        "expires_at": _iso(manifest.get("expires_at")),
        "phone_seen_at": _iso(manifest.get("phone_seen_at")),
        "max_images": _max_images(),
        "images": [_image_view(image) for image in manifest.get("images", [])],
    }


def _phone_view(manifest: dict[str, Any]) -> dict[str, Any]:
    # The phone sees counts and its own client ids only -- no teacher, class or
    # student information travels to the token holder.
    return {
        "session_id": manifest["session_id"],
        "status": "active",
        "expires_at": _iso(manifest.get("expires_at")),
        "max_images": _max_images(),
        "images": [
            {"seq": int(image["seq"]), "client_id": image.get("client_id") or None}
            for image in manifest.get("images", [])
        ],
    }


def _find_image(manifest: dict[str, Any], seq: int) -> dict[str, Any] | None:
    return next((image for image in manifest.get("images", []) if int(image["seq"]) == int(seq)), None)


def _delete_image(manifest: dict[str, Any], seq: int) -> None:
    image = _find_image(manifest, seq)
    if image is None:
        raise CaptureNotFound(_MSG_IMAGE_GONE)
    manifest["images"].remove(image)
    _save(manifest)
    try:
        os.remove(os.path.join(_session_dir(manifest["session_id"]), os.path.basename(image["file"])))
    except OSError:
        pass


# ── Teacher ──


def sweep_expired_sessions(now: float | None = None) -> int:
    """Delete expired or unreadable sessions; returns how many were removed."""
    now = time.time() if now is None else now
    removed = 0
    for session_id in _session_ids():
        manifest = _load(session_id)
        if manifest is not None and not _expired(manifest, now):
            continue
        if manifest is None:
            # A crash between mkdir and the first manifest write leaves an
            # orphan; only reap it once it is older than any live session.
            try:
                age = now - os.path.getmtime(_session_dir(session_id))
            except OSError:
                continue
            if age < SESSION_TTL_SECONDS:
                continue
        _remove_session_dir(session_id)
        removed += 1
    return removed


def _find_teacher_session(teacher_id: int) -> dict[str, Any] | None:
    """The teacher's session, if any (open_session keeps it to one)."""
    for session_id in _session_ids():
        manifest = _load(session_id)
        if manifest and _is_owner(manifest, teacher_id):
            return manifest
    return None


def open_session(teacher_id: int) -> tuple[dict[str, Any], str]:
    """Resume the teacher's active session with a fresh token, or create one.

    Each open rotates the token, so any phone still holding an older link stops
    working; photos already uploaded are kept.
    """
    now = time.time()
    token = secrets.token_urlsafe(32)
    with _capture_lock():
        sweep_expired_sessions(now)
        manifest = _find_teacher_session(teacher_id)
        if manifest is None:
            session_id = secrets.token_hex(12)
            os.makedirs(_session_dir(session_id))
            manifest = {
                "version": 1,
                "session_id": session_id,
                "teacher_id": int(teacher_id),
                "created_at": now,
                "phone_seen_at": None,
                "next_seq": 1,
                "images": [],
            }
        manifest["token_sha256"] = _token_hash(token)
        manifest["expires_at"] = now + SESSION_TTL_SECONDS
        _save(manifest)
    return _session_view(manifest), token


def get_teacher_session(session_id: str, teacher_id: int) -> dict[str, Any]:
    return _session_view(_owned_manifest(session_id, teacher_id, time.time()))


def teacher_image_path(session_id: str, teacher_id: int, seq: int) -> tuple[str, str]:
    """Return ``(path, media_type)`` for one image of the teacher's session."""
    image = _find_image(_owned_manifest(session_id, teacher_id, time.time()), seq)
    if image is not None:
        path = os.path.join(_session_dir(session_id), os.path.basename(image["file"]))
        if os.path.isfile(path):
            return path, IMAGE_MEDIA_TYPES.get(os.path.splitext(path)[1], "image/jpeg")
    raise CaptureNotFound(_MSG_IMAGE_GONE)


def teacher_delete_image(session_id: str, teacher_id: int, seq: int) -> dict[str, Any]:
    with _capture_lock():
        manifest = _owned_manifest(session_id, teacher_id, time.time())
        _delete_image(manifest, seq)
        return _session_view(manifest)


def close_session(session_id: str, teacher_id: int) -> None:
    with _capture_lock():
        _owned_manifest(session_id, teacher_id, time.time())
        _remove_session_dir(session_id)


# ── Phone ──


def phone_state(session_id: str, token: str) -> dict[str, Any]:
    now = time.time()
    with _capture_lock():
        manifest = _token_manifest(session_id, token, now)
        manifest["phone_seen_at"] = now
        _save(manifest)
    return _phone_view(manifest)


def inspect_image(data: bytes) -> tuple[str, int, int]:
    """Return ``(pillow_format, width, height)`` of an acceptable paper photo.

    Also used by student self-upload (services/student_submission.py).
    """
    from PIL import Image, UnidentifiedImageError

    if not data:
        raise CaptureInvalidImage("照片内容为空，请重拍")
    if len(data) > MAX_IMAGE_BYTES:
        raise CaptureInvalidImage("照片过大，请重拍")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image_format = str(image.format or "").upper()
            width, height = image.size
            if image_format not in IMAGE_EXTENSIONS:
                raise CaptureInvalidImage("仅支持 JPEG、PNG 或 WebP 照片")
            if width * height > MAX_IMAGE_PIXELS or min(width, height) < MIN_IMAGE_EDGE:
                raise CaptureInvalidImage("照片尺寸异常，请重拍")
            image.verify()
    except CaptureInvalidImage:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise CaptureInvalidImage("照片已损坏，请重拍") from exc
    return image_format, int(width), int(height)


def add_image(session_id: str, token: str, data: bytes, client_id: str = "") -> dict[str, Any]:
    """Store one phone photo; a repeated ``client_id`` returns the first copy."""
    client_id = str(client_id or "").strip()
    if client_id and not _CLIENT_ID_RE.fullmatch(client_id):
        raise CaptureInvalidImage("照片编号无效，请重拍")
    image_format, width, height = inspect_image(data)
    now = time.time()
    with _capture_lock():
        manifest = _token_manifest(session_id, token, now)
        manifest["phone_seen_at"] = now
        if client_id:
            for image in manifest.get("images", []):
                if image.get("client_id") == client_id:
                    _save(manifest)
                    return {"image": _image_view(image), "duplicate": True}
        if len(manifest.get("images", [])) >= _max_images():
            raise CaptureFull(f"本次拍摄已达 {_max_images()} 张上限，请先在电脑上导入")
        seq = int(manifest.get("next_seq") or 1)
        name = f"{seq:04d}-{secrets.token_hex(8)}{IMAGE_EXTENSIONS[image_format]}"
        target = os.path.join(_session_dir(session_id), name)
        temporary = f"{target}.tmp"
        with open(temporary, "wb") as stream:
            stream.write(data)
        os.replace(temporary, target)
        image = {
            "seq": seq,
            "file": name,
            "client_id": client_id or None,
            "bytes": len(data),
            "width": width,
            "height": height,
            "created_at": now,
        }
        manifest.setdefault("images", []).append(image)
        manifest["next_seq"] = seq + 1
        try:
            _save(manifest)
        except Exception:
            try:
                os.remove(target)
            except OSError:
                pass
            raise
    return {"image": _image_view(image), "duplicate": False}


def phone_delete_image(session_id: str, token: str, seq: int) -> dict[str, Any]:
    with _capture_lock():
        manifest = _token_manifest(session_id, token, time.time())
        _delete_image(manifest, seq)
        return _phone_view(manifest)
