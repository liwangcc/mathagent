"""Local hardware readiness checks for upload-heavy grading workflows."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import shutil
import tempfile
import threading
from datetime import timedelta
from pathlib import Path


UPLOAD_STORAGE_RESERVE_BYTES = 512 * 1024 * 1024
READINESS_IO_TIMEOUT_SECONDS = 3.0

_storage_probe_lock = threading.Lock()
_storage_probe_future: concurrent.futures.Future | None = None
_storage_probe_key: tuple[str, int, int] | None = None

_hardware_probe_lock = threading.Lock()
_hardware_probe_futures: dict[
    tuple[int, str, str], concurrent.futures.Future
] = {}

_DEFAULT_ACCELERATOR_STAGE = "template_registration"
_DEFAULT_ACCELERATOR_ARTIFACT_CONTRACT = "client-evidence-v1"

_PUBLIC_HARDWARE_MESSAGES = {
    "ready": "教师客户端或机会式加速节点已就绪。",
    "waiting_accelerator": "等待教师客户端加速节点，已保存进度不会丢失。",
    "unavailable": "本地加速节点不可用，已停止新的图像处理任务；已保存进度仍可查看。",
    "fault": "本地加速节点故障，已停止新的图像处理任务；已保存进度仍可查看。",
}


def _public_hardware_state(status: str) -> dict[str, str]:
    normalized = status if status in _PUBLIC_HARDWARE_MESSAGES else "fault"
    return {
        "status": normalized,
        "message": _PUBLIC_HARDWARE_MESSAGES[normalized],
    }


def _read_accelerator_hardware_readiness(
    teacher_id: int,
    *,
    stage: str = _DEFAULT_ACCELERATOR_STAGE,
    artifact_contract: str = _DEFAULT_ACCELERATOR_ARTIFACT_CONTRACT,
) -> dict[str, str]:
    """Read compatible node state without exposing pool or device details."""
    from sqlalchemy import and_, or_

    from database import SessionLocal
    from models_accelerator import AcceleratorNode
    from services.accelerator_jobs import (
        ALGORITHM_VERSION,
        NODE_STALE_SECONDS,
        node_supports_artifact_contract,
        node_supports_stage,
        utcnow,
    )

    db = SessionLocal()
    try:
        now = utcnow()
        rows = db.query(AcceleratorNode).filter(
            AcceleratorNode.enabled == 1,
            AcceleratorNode.revoked_at.is_(None),
            or_(
                AcceleratorNode.token_expires_at.is_(None),
                AcceleratorNode.token_expires_at > now,
            ),
            AcceleratorNode.algorithm_version == ALGORITHM_VERSION,
            or_(
                AcceleratorNode.access_scope == "platform",
                and_(
                    AcceleratorNode.access_scope == "tenant",
                    AcceleratorNode.owner_user_id == teacher_id,
                ),
            ),
        ).all()
    finally:
        db.close()

    compatible = [
        node for node in rows
        if node_supports_stage(node, stage)
        and node_supports_artifact_contract(
            node,
            artifact_contract,
        )
    ]
    if not compatible:
        return _public_hardware_state("unavailable")

    fresh_after = now - timedelta(seconds=NODE_STALE_SECONDS)
    fresh = [
        node
        for node in compatible
        if node.last_seen_at is not None and node.last_seen_at >= fresh_after
    ]
    if not fresh:
        return _public_hardware_state("unavailable")
    if any(int(node.free_slots or 0) > 0 for node in fresh):
        return _public_hardware_state("ready")
    return _public_hardware_state("waiting_accelerator")


def _start_hardware_probe(
    key: tuple[int, str, str],
) -> concurrent.futures.Future:
    """Start one keyed hardware probe while the process lock is held."""

    teacher_id, stage, artifact_contract = key

    future: concurrent.futures.Future = concurrent.futures.Future()
    _hardware_probe_futures[key] = future

    def run() -> None:
        try:
            result = _read_accelerator_hardware_readiness(
                teacher_id,
                stage=stage,
                artifact_contract=artifact_contract,
            )
        except BaseException as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)
        finally:
            with _hardware_probe_lock:
                if _hardware_probe_futures.get(key) is future:
                    _hardware_probe_futures.pop(key, None)

    threading.Thread(
        target=run,
        name="mathagent-hardware-readiness",
        daemon=True,
    ).start()
    return future


async def get_public_hardware_readiness(
    teacher_id: int | str,
    *,
    stage: str = _DEFAULT_ACCELERATOR_STAGE,
    artifact_contract: str = _DEFAULT_ACCELERATOR_ARTIFACT_CONTRACT,
    timeout_seconds: float = READINESS_IO_TIMEOUT_SECONDS,
) -> dict[str, str]:
    """Return anonymous accelerator readiness within one bounded I/O budget."""
    try:
        owner_id = int(teacher_id)
    except (TypeError, ValueError):
        return _public_hardware_state("unavailable")
    if owner_id < 1:
        return _public_hardware_state("unavailable")
    if not isinstance(stage, str) or not stage.strip():
        return _public_hardware_state("fault")
    if not isinstance(artifact_contract, str) or not artifact_contract.strip():
        return _public_hardware_state("fault")
    probe_key = (owner_id, stage.strip(), artifact_contract.strip())

    timeout = float(timeout_seconds)
    if timeout <= 0:
        raise ValueError("hardware readiness timeout must be positive")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    with _hardware_probe_lock:
        future = _hardware_probe_futures.get(probe_key)
        if future is None or future.done():
            future = _start_hardware_probe(probe_key)

    while not future.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            return _public_hardware_state("fault")
        await asyncio.sleep(min(0.05, remaining))
    try:
        result = future.result()
    except Exception:
        return _public_hardware_state("fault")
    status = str(result.get("status") or "fault")
    return _public_hardware_state(status)


def configure_multipart_temp_dir(
    upload_dir: str | os.PathLike[str],
) -> str:
    """Point Starlette spools at monitored storage without touching the mount.

    This function runs while ``app`` is imported.  Directory creation belongs
    in the time-bounded storage probe so a stalled upload mount cannot prevent
    the teacher-facing application from starting.
    """
    root = Path(os.path.abspath(os.fspath(upload_dir)))
    target = root / ".multipart-tmp"
    tempfile.tempdir = os.fspath(target)
    return os.fspath(target)


def _existing_storage_root(path: str | os.PathLike[str]) -> Path:
    candidate = Path(path).resolve(strict=False)
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    if candidate.exists() and not candidate.is_dir():
        candidate = candidate.parent
    return candidate


def get_upload_storage_readiness(
    upload_dir: str | os.PathLike[str],
    max_batch_bytes: int,
    *,
    required_copies: int = 2,
) -> dict[str, object]:
    """Check that a complete upload can be staged without exhausting disk.

    Before request parsing, one copy may be held by the multipart spool and a
    second is written into the teacher-owned batch directory. Once parsing has
    completed, callers can pass ``required_copies=1`` for the remaining copy.
    """
    if max_batch_bytes < 1 or required_copies < 1:
        raise ValueError("storage readiness limits must be positive")

    try:
        requested = Path(upload_dir).resolve(strict=False)
        if requested.exists() and not requested.is_dir():
            return {
                "ready": False,
                "status": "fault",
                "code": "upload_storage_path_invalid",
            }
        root = _existing_storage_root(requested)
        if not root.is_dir() or not os.access(root, os.W_OK):
            return {
                "ready": False,
                "status": "fault",
                "code": "upload_storage_not_writable",
            }

        # Prepare the durable target while this function is running in the
        # bounded daemon probe. Before multipart parsing, also prepare the one
        # process-wide spool under the upload root. Post-parse checks pass
        # required_copies=1 and must not create a nested spool in every batch.
        requested.mkdir(parents=True, exist_ok=True)
        if not requested.is_dir() or not os.access(requested, os.W_OK):
            return {
                "ready": False,
                "status": "fault",
                "code": "upload_storage_not_writable",
            }
        if required_copies > 1:
            spool = requested / ".multipart-tmp"
            if spool.is_symlink():
                return {
                    "ready": False,
                    "status": "fault",
                    "code": "upload_storage_temp_invalid",
                }
            spool.mkdir(parents=False, exist_ok=True)
            if not spool.is_dir() or not os.access(spool, os.W_OK):
                return {
                    "ready": False,
                    "status": "fault",
                    "code": "upload_storage_temp_unavailable",
                }
            try:
                spool.chmod(0o700)
            except OSError:
                pass

        usage = shutil.disk_usage(requested)
    except (OSError, RuntimeError, ValueError):
        return {
            "ready": False,
            "status": "fault",
            "code": "upload_storage_check_failed",
        }

    required = (
        int(max_batch_bytes) * int(required_copies)
        + UPLOAD_STORAGE_RESERVE_BYTES
    )
    if int(usage.free) < required:
        return {
            "ready": False,
            "status": "unavailable",
            "code": "upload_storage_insufficient",
        }
    return {"ready": True, "status": "available", "code": "ready"}


async def get_upload_storage_readiness_async(
    upload_dir: str | os.PathLike[str],
    max_batch_bytes: int,
    *,
    required_copies: int = 2,
    timeout_seconds: float = READINESS_IO_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Run storage probes off-loop without accumulating stuck worker threads.

    A filesystem call already executing in this process is reused when its
    arguments match. Other probes wait for it to settle before starting. This
    keeps a stalled mount to one daemon thread instead of consuming the shared
    ``asyncio.to_thread`` pool on every availability or upload request.
    """
    timeout = float(timeout_seconds)
    if timeout <= 0:
        raise ValueError("storage readiness timeout must be positive")
    key = (
        os.path.abspath(os.fspath(upload_dir)),
        int(max_batch_bytes),
        int(required_copies),
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    while True:
        with _storage_probe_lock:
            future = _storage_probe_future
            current_key = _storage_probe_key
            if future is None or future.done():
                future = _start_storage_probe(
                    key,
                    upload_dir,
                    max_batch_bytes,
                    required_copies,
                )
                current_key = key

        while not future.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            # Do not wrap the concurrent future for every request: a probe on a
            # hung mount may never complete, and cancelled wrappers would then
            # accumulate callbacks forever. Short polling keeps the wait layer
            # bounded while the single daemon probe remains shared.
            await asyncio.sleep(min(0.05, remaining))
        if not future.done():
            break
        try:
            result = future.result()
        except Exception:
            return {
                "ready": False,
                "status": "fault",
                "code": "upload_storage_check_failed",
            }
        if current_key == key:
            return result

    return {
        "ready": False,
        "status": "fault",
        "code": "upload_storage_check_timed_out",
    }


def _start_storage_probe(
    key: tuple[str, int, int],
    upload_dir: str | os.PathLike[str],
    max_batch_bytes: int,
    required_copies: int,
) -> concurrent.futures.Future:
    """Start the process-wide storage probe while ``_storage_probe_lock`` is held."""
    global _storage_probe_future, _storage_probe_key

    future: concurrent.futures.Future = concurrent.futures.Future()
    _storage_probe_future = future
    _storage_probe_key = key

    def run() -> None:
        global _storage_probe_future, _storage_probe_key
        try:
            result = get_upload_storage_readiness(
                upload_dir,
                max_batch_bytes,
                required_copies=required_copies,
            )
        except BaseException as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)
        finally:
            with _storage_probe_lock:
                if _storage_probe_future is future:
                    _storage_probe_future = None
                    _storage_probe_key = None

    threading.Thread(
        target=run,
        name="mathagent-storage-readiness",
        daemon=True,
    ).start()
    return future


async def get_grading_infrastructure_readiness(
    upload_dir: str | os.PathLike[str],
    request_bytes: int,
) -> dict[str, object]:
    """Check global grading dependencies before multipart parsing begins."""
    storage = await get_upload_storage_readiness_async(
        upload_dir,
        max(1, int(request_bytes)),
        required_copies=2,
    )
    if not storage.get("ready"):
        return storage

    from services.analyzer import get_grading_readiness, get_grading_readiness_async
    from services.model_concurrency import RedisModelLimiter, get_model_limiter
    from services.ocr_service import get_ocr_readiness

    try:
        async with asyncio.timeout(READINESS_IO_TIMEOUT_SECONDS):
            limiter = get_model_limiter()
            if isinstance(limiter, RedisModelLimiter):
                await limiter.ping()
            ocr = await get_ocr_readiness()
            grading = get_grading_readiness()
            if isinstance(limiter, RedisModelLimiter):
                grading = await get_grading_readiness_async(limiter)
    except TimeoutError:
        return {
            "ready": False,
            "status": "fault",
            "code": "grading_readiness_timed_out",
        }
    except Exception:
        return {
            "ready": False,
            "status": "fault",
            "code": "grading_coordination_unavailable",
        }

    if not ocr.get("configured") or not ocr.get("ready"):
        return {
            "ready": False,
            "status": "unavailable",
            "code": "ocr_unavailable",
        }
    if (
        not grading
        or not grading.get("configured")
        or not grading.get("ready")
    ):
        return {
            "ready": False,
            "status": "unavailable",
            "code": "flash_unavailable",
        }
    return {"ready": True, "status": "available", "code": "ready"}
