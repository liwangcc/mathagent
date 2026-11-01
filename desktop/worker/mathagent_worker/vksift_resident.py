"""Crash-isolated resident VulkanSift helper client for Windows."""
from __future__ import annotations

import atexit
import hashlib
import os
import queue
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import BinaryIO

from .gpu_backend import _runtime_root, subprocess_environment

_RESIDENT_SHA256 = "1a1e83c9bb8650a6fb782468fc9fcaee9c0d595b60e42aeee29be86e89523fdc"
_VULKANSIFT_DLL_SHA256 = "777fe099991ad5a0d564ec17b02da8e785a5321806e1219d2c1fd45a30a1c7cc"
_VCRUNTIME140_SHA256 = "d5e4d9a3e835fa679450145d6a7d94e36573a509317111904d9b3712c30d9066"
_REQUEST = struct.Struct("<4sIIII")
_RESPONSE = struct.Struct("<4sIIII")
_METADATA_RESPONSE = struct.Struct("<4sIII")
_FEATURE_SIZE = 144
_PROTOCOL_VERSION = 1
_NVIDIA_VENDOR_ID = 0x10DE
_MAX_EDGE = 1280
_MAX_PIXELS = _MAX_EDGE * _MAX_EDGE
_MAX_FEATURES = 3000


class ResidentGpuError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise ResidentGpuError("gpu_resident_closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class ResidentVulkanSift:
    def __init__(self) -> None:
        self._process: subprocess.Popen[bytes] | None = None
        self._device_name: str | None = None
        self._vendor_id: int | None = None
        self._lock = threading.Lock()

    def _paths(self) -> tuple[Path, Path, Path]:
        root = _runtime_root()
        return (
            root / "vksift_resident.exe",
            root / "vulkansift.dll",
            root / "vcruntime140.dll",
        )

    def _start(self) -> subprocess.Popen[bytes]:
        if self._process is not None and self._process.poll() is None:
            return self._process
        self._stop()
        if os.name != "nt":
            raise ResidentGpuError("gpu_resident_unsupported_os")
        helper, library, runtime = self._paths()
        if not helper.is_file() or not library.is_file() or not runtime.is_file():
            raise ResidentGpuError("gpu_resident_runtime_missing")
        if (
            _sha256(helper) != _RESIDENT_SHA256
            or _sha256(library) != _VULKANSIFT_DLL_SHA256
            or _sha256(runtime) != _VCRUNTIME140_SHA256
        ):
            raise ResidentGpuError("gpu_resident_runtime_mismatch")
        try:
            process = subprocess.Popen(
                [str(helper)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=str(helper.parent),
                env=subprocess_environment(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise ResidentGpuError("gpu_resident_start_failed") from exc
        if process.stdin is None or process.stdout is None:
            process.kill()
            raise ResidentGpuError("gpu_resident_pipe_failed")
        self._process = process
        try:
            self._probe_started_process(process, timeout_seconds=5.0)
        except Exception:
            self._stop()
            raise
        return process

    def _probe_started_process(
        self,
        process: subprocess.Popen[bytes],
        *,
        timeout_seconds: float,
    ) -> None:
        result: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)

        def exchange() -> None:
            try:
                assert process.stdin is not None and process.stdout is not None
                process.stdin.write(
                    _REQUEST.pack(b"VKPQ", _PROTOCOL_VERSION, 0, 0, 0)
                )
                process.stdin.flush()
                header = _read_exact(process.stdout, _METADATA_RESPONSE.size)
                magic, version, vendor_id, name_size = _METADATA_RESPONSE.unpack(
                    header
                )
                if (
                    magic != b"VKPM"
                    or version != _PROTOCOL_VERSION
                    or vendor_id != _NVIDIA_VENDOR_ID
                    or not 1 <= name_size <= 255
                ):
                    raise ResidentGpuError("gpu_resident_metadata_invalid")
                raw_name = _read_exact(process.stdout, name_size)
                try:
                    device_name = raw_name.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ResidentGpuError("gpu_resident_metadata_invalid") from exc
                if "nvidia" not in device_name.casefold():
                    raise ResidentGpuError("gpu_resident_not_nvidia")
                result.put(("ok", (device_name, vendor_id)))
            except Exception as exc:
                result.put(("error", exc))

        thread = threading.Thread(
            target=exchange,
            name="mathagent-vksift-probe",
            daemon=True,
        )
        thread.start()
        try:
            status, value = result.get(timeout=max(0.1, timeout_seconds))
        except queue.Empty as exc:
            raise ResidentGpuError("gpu_resident_probe_timeout") from exc
        if status != "ok":
            if isinstance(value, ResidentGpuError):
                raise value
            raise ResidentGpuError("gpu_resident_probe_failed") from value
        self._device_name, self._vendor_id = value  # type: ignore[misc]

    def _stop(self) -> None:
        process = self._process
        self._process = None
        self._device_name = None
        self._vendor_id = None
        if process is None:
            return
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):
            pass
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def close(self) -> None:
        with self._lock:
            self._stop()

    def probe(self) -> dict[str, object]:
        with self._lock:
            self._start()
            if self._device_name is None or self._vendor_id != _NVIDIA_VENDOR_ID:
                raise ResidentGpuError("gpu_resident_metadata_invalid")
            return {
                "device_name": self._device_name,
                "vendor_id": self._vendor_id,
            }

    def extract(self, gray, *, timeout_seconds: float) -> tuple[bytes, float]:
        if (
            getattr(gray, "dtype", None) is None
            or str(gray.dtype) != "uint8"
            or getattr(gray, "ndim", None) != 2
            or not gray.flags.c_contiguous
        ):
            raise ResidentGpuError("gpu_resident_input_invalid")
        height, width = (int(value) for value in gray.shape)
        payload_size = width * height
        if (
            width <= 0
            or height <= 0
            or width > _MAX_EDGE
            or height > _MAX_EDGE
            or payload_size > _MAX_PIXELS
        ):
            raise ResidentGpuError("gpu_resident_input_invalid")
        with self._lock:
            process = self._start()
            result: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)
            payload = gray.tobytes(order="C")

            def exchange() -> None:
                try:
                    assert process.stdin is not None and process.stdout is not None
                    started = time.perf_counter()
                    process.stdin.write(
                        _REQUEST.pack(
                            b"VKRQ",
                            _PROTOCOL_VERSION,
                            width,
                            height,
                            payload_size,
                        )
                    )
                    process.stdin.write(payload)
                    process.stdin.flush()
                    header = _read_exact(process.stdout, _RESPONSE.size)
                    magic, version, status, count, native_error = _RESPONSE.unpack(
                        header
                    )
                    if (
                        magic != b"VKRS"
                        or version != _PROTOCOL_VERSION
                        or count > _MAX_FEATURES
                    ):
                        raise ResidentGpuError("gpu_resident_protocol_invalid")
                    body = _read_exact(process.stdout, count * _FEATURE_SIZE)
                    if status != 0 or native_error != 0 or count <= 0:
                        raise ResidentGpuError("gpu_resident_native_error")
                    elapsed_ms = (time.perf_counter() - started) * 1000.0
                    result.put(("ok", (struct.pack("<I", count) + body, elapsed_ms)))
                except Exception as exc:
                    result.put(("error", exc))

            thread = threading.Thread(
                target=exchange,
                name="mathagent-vksift-ipc",
                daemon=True,
            )
            thread.start()
            try:
                status, value = result.get(timeout=max(0.1, timeout_seconds))
            except queue.Empty as exc:
                self._stop()
                raise ResidentGpuError("gpu_resident_timeout") from exc
            if status != "ok":
                self._stop()
                if isinstance(value, ResidentGpuError):
                    raise value
                raise ResidentGpuError("gpu_resident_io_failed") from value
            return value  # type: ignore[return-value]


_RESIDENT = ResidentVulkanSift()


def extract_resident(gray, *, timeout_seconds: float) -> tuple[bytes, float]:
    return _RESIDENT.extract(gray, timeout_seconds=timeout_seconds)


def probe_resident() -> dict[str, object]:
    return _RESIDENT.probe()


def close_resident() -> None:
    _RESIDENT.close()


atexit.register(close_resident)
