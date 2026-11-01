from __future__ import annotations

import hashlib
import math
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

GPU_BACKEND = "windows_nvidia_gpu"
CPU_BACKEND = "windows_cpu_opencv"
GPU_BACKEND_VERSION = "vulkansift-04245a5e-usac-accurate-v1"
GPU_CONTRACT_MAX_PROJECTION_PX = 6.0
_KNOWN_ANSWER_FIXTURE_SHA256 = "12d12c9607e5b33292ddda7b9a593c1fb7537c0e96e6903c5740b45ccef15fbd"
_MIN_KNOWN_ANSWER_FEATURES = 100


@dataclass(frozen=True)
class GpuSelfTestResult:
    available: bool
    enabled: bool
    backend: str
    backend_version: str
    device_name: str | None
    driver_version: str | None
    gpu_time_ms: float | None
    cpu_time_ms: float | None
    speedup: float | None
    projection_max_px: float | None
    fallback_reason: str | None

    def as_status(self) -> dict:
        return {
            "available": self.available,
            "enabled": self.enabled,
            "backend": self.backend,
            "backendVersion": self.backend_version,
            "deviceName": self.device_name,
            "driverVersion": self.driver_version,
            "gpuTimeMs": self.gpu_time_ms,
            "cpuTimeMs": self.cpu_time_ms,
            "speedup": self.speedup,
            "projectionMaxPx": self.projection_max_px,
            "fallbackReason": self.fallback_reason,
        }


def _runtime_root() -> Path:
    configured = os.environ.get("MATHAGENT_NVIDIA_RUNTIME_DIR", "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent / "native" / "windows_nvidia_gpu"


def _fixture_path() -> Path:
    return _runtime_root() / "known_answer_1536x2048.pgm"


def subprocess_environment() -> dict[str, str]:
    """Pass only the OS fields needed by native helpers, never node credentials."""

    allowed = (
        "SystemRoot",
        "WINDIR",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "PROGRAMDATA",
        "ProgramFiles",
        "ProgramW6432",
        "PATH",
    )
    return {key: os.environ[key] for key in allowed if os.environ.get(key)}


def _canonical_feature_bytes(raw: bytes) -> tuple[int, str]:
    import struct

    if len(raw) < 4:
        raise RuntimeError("gpu_feature_output_invalid")
    count = struct.unpack_from("<I", raw, 0)[0]
    record = struct.Struct("<ffff128s")
    expected = 4 + count * record.size
    if len(raw) != expected:
        raise RuntimeError("gpu_feature_output_invalid")
    records = [record.unpack_from(raw, 4 + index * record.size) for index in range(count)]
    records.sort(key=lambda value: (value[0], value[1], value[2], value[3], value[4]))
    canonical = b"".join(record.pack(*value) for value in records)
    return count, hashlib.sha256(canonical).hexdigest()


def _canonical_feature_sha256(path: Path) -> str:
    return _canonical_feature_bytes(path.read_bytes())[1]


def _contract_sample_points(
    width: int,
    height: int,
    question_numbers: list[str] | None = None,
) -> list[tuple[float, float]]:
    """Return stable registration probes without answer-box geometry."""

    return [
        (0.0, 0.0),
        (float(width - 1), 0.0),
        (float(width - 1), float(height - 1)),
        (0.0, float(height - 1)),
        (float(width) / 2.0, float(height) / 2.0),
    ]


def _system_directory() -> Path:
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if length <= 0 or length >= len(buffer):
        raise OSError("system_directory_unavailable")
    return Path(buffer.value)


def _nvidia_driver_version(device_name: str) -> str | None:
    try:
        executable = _system_directory() / "nvidia-smi.exe"
        if not executable.is_file():
            return None
        completed = subprocess.run(
            [
                str(executable),
                "--query-gpu=name,driver_version",
                "--format=csv,noheader",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
            env=subprocess_environment(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in completed.stdout.splitlines():
        name, separator, driver = line.partition(",")
        name = name.strip()
        driver = driver.strip()
        if (
            separator
            and name.casefold() == device_name.casefold()
            and 1 <= len(name) <= 128
        ):
            return driver[:64] or None
    return None


def run_gpu_self_test(
    *,
    timeout_seconds: float = 8.0,
) -> GpuSelfTestResult:
    fixture = _fixture_path()
    if os.name != "nt":
        return _unavailable("unsupported_operating_system")
    if not fixture.is_file():
        return _unavailable("gpu_runtime_missing")
    if hashlib.sha256(fixture.read_bytes()).hexdigest() != _KNOWN_ANSWER_FIXTURE_SHA256:
        return _unavailable("gpu_known_answer_fixture_mismatch")
    device_name: str | None = None
    driver_version: str | None = None
    try:
        import cv2
        import numpy as np

        from . import preprocessing
        from .vksift_resident import extract_resident, probe_resident

        native_metadata = probe_resident()
        device_name = str(native_metadata.get("device_name") or "")
        if (
            native_metadata.get("vendor_id") != 0x10DE
            or "nvidia" not in device_name.casefold()
            or not 1 <= len(device_name) <= 128
        ):
            return _unavailable("nvidia_device_unavailable")
        driver_version = _nvidia_driver_version(device_name)

        with tempfile.TemporaryDirectory(prefix="mathagent-gpu-selftest-") as directory:
            root = Path(directory)
            template_image = cv2.imread(
                str(fixture),
                cv2.IMREAD_COLOR | getattr(cv2, "IMREAD_IGNORE_ORIENTATION", 0),
            )
            if template_image is None:
                return _unavailable(
                    "gpu_known_answer_failed",
                    device_name=device_name,
                )
            gray, _scale = preprocessing._resize_gray(template_image)
            raw_features, _warmup_ms = extract_resident(
                gray,
                timeout_seconds=timeout_seconds,
            )
            feature_count, _feature_sha256 = _canonical_feature_bytes(raw_features)
            if feature_count < _MIN_KNOWN_ANSWER_FEATURES:
                return _unavailable(
                    "gpu_known_answer_mismatch",
                    device_name=device_name,
                )

            height, width = template_image.shape[:2]
            source_to_template = np.asarray(
                [
                    [0.992, 0.018, 18.0],
                    [-0.012, 0.985, 24.0],
                    [0.000012, -0.000009, 1.0],
                ],
                dtype=np.float64,
            )
            source_image = cv2.warpPerspective(
                template_image,
                np.linalg.inv(source_to_template),
                (width, height),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=(255, 255, 255),
            )
            source_path = root / "source.png"
            template_path = root / "template.png"
            if not cv2.imwrite(str(source_path), source_image) or not cv2.imwrite(
                str(template_path), template_image
            ):
                raise RuntimeError("gpu_known_answer_encode_failed")
            question_numbers = ["1"]
            job = {
                "stage": "template_registration",
                "algorithm_version": preprocessing.ALGORITHM_VERSION,
                "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
                "template_sha256": hashlib.sha256(template_path.read_bytes()).hexdigest(),
                "layout_snapshot_sha256": "a" * 64,
                "layout_snapshot": {
                    "page_type_id": "known-answer",
                    "question_schema": [
                        {"question_number": "1", "question_type": "solve"}
                    ],
                    "question_numbers": question_numbers,
                },
            }
            preprocessing._GPU_TEMPLATE_CACHE.clear()
            preprocessing._CPU_TEMPLATE_CACHE.clear()
            preprocessing._gpu_candidate(source_path, template_path, job)
            preprocessing._cpu_candidate(source_path, template_path, job)

            gpu_started = time.perf_counter()
            gpu_candidate = preprocessing._gpu_candidate(source_path, template_path, job)
            gpu_time_ms = (time.perf_counter() - gpu_started) * 1000.0
            cpu_started = time.perf_counter()
            cpu_candidate = preprocessing._cpu_candidate(source_path, template_path, job)
            cpu_time_ms = (time.perf_counter() - cpu_started) * 1000.0

            sample_points = np.float32(
                _contract_sample_points(width, height, question_numbers)
            ).reshape(-1, 1, 2)
            gpu_projection = cv2.perspectiveTransform(
                sample_points,
                np.asarray(gpu_candidate["homography"], dtype=np.float64),
            )
            cpu_projection = cv2.perspectiveTransform(
                sample_points,
                np.asarray(cpu_candidate["homography"], dtype=np.float64),
            )
            expected_projection = cv2.perspectiveTransform(
                sample_points,
                source_to_template,
            )
            projection = max(
                float(np.linalg.norm(gpu_projection - cpu_projection, axis=2).max()),
                float(np.linalg.norm(gpu_projection - expected_projection, axis=2).max()),
                float(np.linalg.norm(cpu_projection - expected_projection, axis=2).max()),
            )
    except Exception as exc:
        reason = (
            "gpu_known_answer_timeout"
            if "timeout" in str(exc).lower()
            else "gpu_known_answer_failed"
        )
        return _unavailable(reason, device_name=device_name)
    finally:
        try:
            preprocessing._GPU_TEMPLATE_CACHE.clear()
            preprocessing._CPU_TEMPLATE_CACHE.clear()
        except (NameError, AttributeError):
            pass

    enabled = True
    reason = None
    if not math.isfinite(projection) or projection > GPU_CONTRACT_MAX_PROJECTION_PX:
        enabled = False
        reason = "gpu_geometry_contract_mismatch"
    elif not math.isfinite(gpu_time_ms) or not math.isfinite(cpu_time_ms):
        enabled = False
        reason = "gpu_timing_invalid"
    elif gpu_time_ms >= cpu_time_ms:
        enabled = False
        reason = "gpu_not_faster_than_cpu"
    speedup = cpu_time_ms / gpu_time_ms if gpu_time_ms > 0 else None
    return GpuSelfTestResult(
        available=True,
        enabled=enabled,
        backend=GPU_BACKEND if enabled else CPU_BACKEND,
        backend_version=GPU_BACKEND_VERSION,
        device_name=device_name,
        driver_version=driver_version,
        gpu_time_ms=round(gpu_time_ms, 3),
        cpu_time_ms=round(cpu_time_ms, 3),
        speedup=round(speedup, 3) if speedup is not None else None,
        projection_max_px=round(projection, 6),
        fallback_reason=reason,
    )


def _unavailable(reason: str, *, device_name: str | None = None) -> GpuSelfTestResult:
    return GpuSelfTestResult(
        available=False,
        enabled=False,
        backend=CPU_BACKEND,
        backend_version=GPU_BACKEND_VERSION,
        device_name=device_name,
        driver_version=None,
        gpu_time_ms=None,
        cpu_time_ms=None,
        speedup=None,
        projection_max_px=None,
        fallback_reason=reason,
    )


def conservative_homography(
    source_points,
    target_points,
    cv2_module,
):
    """Fit the Windows GPU candidate with the qualified deterministic contract."""
    homography, mask = cv2_module.findHomography(
        source_points,
        target_points,
        cv2_module.USAC_ACCURATE,
        3.0,
    )
    return homography, mask


def select_backend(
    self_test: GpuSelfTestResult,
    gpu_operation: Callable[[], object],
    cpu_operation: Callable[[], object],
) -> tuple[object, dict]:
    """Run one bounded GPU queue operation and fail closed to real CPU work."""
    if self_test.enabled:
        try:
            value = gpu_operation()
            return value, {
                "backend": GPU_BACKEND,
                "device_name": self_test.device_name,
                "driver_version": self_test.driver_version,
                "backend_version": self_test.backend_version,
                "fallback_reason": None,
            }
        except Exception:
            fallback_reason = "gpu_operation_failed"
    else:
        fallback_reason = self_test.fallback_reason or "gpu_not_enabled"
    value = cpu_operation()
    return value, {
        "backend": CPU_BACKEND,
        "device_name": self_test.device_name,
        "driver_version": self_test.driver_version,
        "backend_version": self_test.backend_version,
        "fallback_reason": fallback_reason,
    }
