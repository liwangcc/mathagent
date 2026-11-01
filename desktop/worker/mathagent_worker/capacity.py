from __future__ import annotations

import hashlib
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path


POLICY_VERSION = "cpu-capacity-v2"
ALLOWED_WORKER_COUNTS = (0, 2, 3)
MIN_LOCAL_WORKERS = 2
MIN_PHYSICAL_CORES = 4
CPU_BENCHMARK_WORKER_COUNTS = (2, 3)
CPU_BENCHMARK_ATTEMPTS_PER_WORKER = 2
MIN_GOOD_FEATURES = 25
CPU_BENCHMARK_FIXTURE_SHA256 = (
    "999765b2f9ea826c3175643f24f1652363fc8b1fa0f7d41fdaa611a4bf650a22"
)

GIB = 1024**3
MIB = 1024**2
MIN_AVAILABLE_MEMORY_BYTES = int(2.5 * GIB)
HOST_MEMORY_RESERVE_BYTES = int(1.5 * GIB)
ESTIMATED_WORKER_MEMORY_BYTES = 384 * MIB
BENCHMARK_MEMORY_HEADROOM = 1.25

MAX_START_CPU_PERCENT = 80.0
MIN_THROUGHPUT_PER_SECOND = 0.8
MAX_P95_SECONDS = 3.0
MIN_TIER_THROUGHPUT_GAIN = 0.10
_LOWER_HEX = frozenset("0123456789abcdef")


class ResourceMode(str, Enum):
    QUIET = "quiet"
    BALANCED = "balanced"
    FAST = "fast"


@dataclass(frozen=True)
class SystemSnapshot:
    physical_cores: int | None
    logical_cores: int | None
    available_memory_bytes: int | None
    cpu_percent: float | None
    on_battery: bool | None


@dataclass(frozen=True)
class BenchmarkTier:
    workers: int
    throughput_per_second: float
    p95_seconds: float
    peak_rss_bytes: int
    completed: int
    attempted: int
    digest_consistent: bool


@dataclass(frozen=True)
class BenchmarkReport:
    policy_version: str
    algorithm_version: str
    fixture_sha256: str
    preprocessor_available: bool
    tiers: tuple[BenchmarkTier, ...]


@dataclass(frozen=True)
class CapacityDecision:
    route: str
    worker_count: int
    hardware_worker_capacity: int
    reason_codes: tuple[str, ...]


def _cpu_benchmark_fixture() -> Path:
    return (
        Path(__file__).resolve().parent
        / "native"
        / "windows_nvidia_gpu"
        / "known_answer_1536x2048.pgm"
    )


def _cpu_sift_sample(fixture: Path) -> tuple[float, str, int]:
    import cv2
    import numpy as np
    import psutil

    from .preprocessing import (
        FEATURE_MAX_EDGE,
        SIFT_CONTRAST_THRESHOLD,
        SIFT_MAX_FEATURES,
    )

    started = time.perf_counter()
    image = cv2.imread(
        str(fixture),
        cv2.IMREAD_GRAYSCALE | getattr(cv2, "IMREAD_IGNORE_ORIENTATION", 0),
    )
    if image is None or image.ndim != 2:
        raise RuntimeError("cpu_benchmark_fixture_decode_failed")
    height, width = image.shape
    scale = min(1.0, FEATURE_MAX_EDGE / float(max(height, width)))
    if scale != 1.0:
        image = cv2.resize(
            image,
            (max(1, round(width * scale)), max(1, round(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
    sift = cv2.SIFT_create(
        nfeatures=SIFT_MAX_FEATURES,
        contrastThreshold=SIFT_CONTRAST_THRESHOLD,
    )
    points, descriptors = sift.detectAndCompute(image, None)
    elapsed = time.perf_counter() - started
    if descriptors is None or len(points) < MIN_GOOD_FEATURES:
        raise RuntimeError("cpu_benchmark_features_missing")
    point_values = np.asarray(
        [
            (
                point.pt[0],
                point.pt[1],
                point.size,
                point.angle,
                point.response,
                point.octave,
            )
            for point in points
        ],
        dtype=np.float64,
    )
    digest = hashlib.sha256(
        point_values.tobytes(order="C")
        + np.ascontiguousarray(descriptors, dtype=np.float32).tobytes(order="C")
    ).hexdigest()
    peak_rss = int(psutil.Process().memory_info().rss)
    return elapsed, digest, peak_rss


def _benchmark_cpu_tier(
    fixture: Path,
    workers: int,
    expected_digest: str,
) -> BenchmarkTier:
    attempted = workers * CPU_BENCHMARK_ATTEMPTS_PER_WORKER
    started = time.perf_counter()
    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix=f"mathagent-cpu-benchmark-{workers}",
    ) as executor:
        samples = list(executor.map(_cpu_sift_sample, (fixture,) * attempted))
    wall_seconds = time.perf_counter() - started
    elapsed = sorted(sample[0] for sample in samples)
    p95_index = max(0, math.ceil(0.95 * len(elapsed)) - 1)
    digests = [sample[1] for sample in samples]
    return BenchmarkTier(
        workers=workers,
        throughput_per_second=(
            len(samples) / wall_seconds if wall_seconds > 0 else 0.0
        ),
        p95_seconds=elapsed[p95_index],
        peak_rss_bytes=max(sample[2] for sample in samples),
        completed=len(samples),
        attempted=attempted,
        digest_consistent=all(value == expected_digest for value in digests),
    )


def _failed_benchmark_report() -> BenchmarkReport:
    return BenchmarkReport(
        policy_version=POLICY_VERSION,
        algorithm_version="template-registration-v2",
        fixture_sha256=CPU_BENCHMARK_FIXTURE_SHA256,
        preprocessor_available=False,
        tiers=(),
    )


def _run_cpu_benchmark_uncached() -> BenchmarkReport:
    import cv2

    from .preprocessing import ALGORITHM_VERSION

    fixture = _cpu_benchmark_fixture()
    if not fixture.is_file():
        raise RuntimeError("cpu_benchmark_fixture_missing")
    if hashlib.sha256(fixture.read_bytes()).hexdigest() != CPU_BENCHMARK_FIXTURE_SHA256:
        raise RuntimeError("cpu_benchmark_fixture_mismatch")

    cv2.setNumThreads(1)
    _warmup_seconds, expected_digest, _warmup_rss = _cpu_sift_sample(fixture)
    tiers = tuple(
        _benchmark_cpu_tier(fixture, workers, expected_digest)
        for workers in CPU_BENCHMARK_WORKER_COUNTS
    )
    return BenchmarkReport(
        policy_version=POLICY_VERSION,
        algorithm_version=ALGORITHM_VERSION,
        fixture_sha256=CPU_BENCHMARK_FIXTURE_SHA256,
        preprocessor_available=True,
        tiers=tiers,
    )


@lru_cache(maxsize=1)
def run_cpu_benchmark() -> BenchmarkReport:
    """Run the packaged known-answer CPU qualification once per sidecar process."""

    try:
        return _run_cpu_benchmark_uncached()
    except Exception:
        return _failed_benchmark_report()


def parse_resource_mode(value: str) -> ResourceMode:
    try:
        return ResourceMode(value)
    except ValueError as exc:
        raise ValueError("invalid_resource_mode") from exc


def _normalize_worker_count(value: int) -> int:
    return max(count for count in ALLOWED_WORKER_COUNTS if count <= max(0, value))


def _next_lower_worker_count(value: int) -> int:
    return _normalize_worker_count(value - 1)


def _core_limit(physical_cores: int, mode: ResourceMode) -> int:
    if mode is ResourceMode.QUIET:
        return min(2, max(0, physical_cores - 2))
    if mode is ResourceMode.BALANCED:
        return min(3, max(0, physical_cores - 2))
    return min(3, max(0, physical_cores - 1))


def review_hardware(snapshot: SystemSnapshot, mode: ResourceMode) -> CapacityDecision:
    reasons: list[str] = []
    physical_cores = snapshot.physical_cores
    available_memory = snapshot.available_memory_bytes

    if physical_cores is None:
        reasons.append("physical_core_count_unavailable")
    elif physical_cores < MIN_PHYSICAL_CORES:
        reasons.append("insufficient_physical_cores")

    if available_memory is None:
        reasons.append("available_memory_unavailable")
    elif available_memory < MIN_AVAILABLE_MEMORY_BYTES:
        reasons.append("insufficient_available_memory")

    if snapshot.cpu_percent is None:
        reasons.append("cpu_load_unavailable")
    elif not math.isfinite(snapshot.cpu_percent) or snapshot.cpu_percent < 0:
        reasons.append("cpu_load_invalid")
    elif snapshot.cpu_percent >= MAX_START_CPU_PERCENT:
        reasons.append("cpu_busy")

    if reasons:
        return CapacityDecision("server_required", 0, 0, tuple(reasons))

    assert physical_cores is not None
    assert available_memory is not None
    memory_workers = max(
        0,
        (available_memory - HOST_MEMORY_RESERVE_BYTES)
        // ESTIMATED_WORKER_MEMORY_BYTES,
    )
    capacity = _normalize_worker_count(
        min(_core_limit(physical_cores, mode), int(memory_workers))
    )
    if snapshot.on_battery:
        capacity = _next_lower_worker_count(capacity)
        reasons.append("on_battery_capacity_reduced")

    if capacity < MIN_LOCAL_WORKERS:
        reasons.append("minimum_worker_capacity_unmet")
        return CapacityDecision("server_required", 0, 0, tuple(reasons))

    return CapacityDecision("qualification_required", 0, capacity, tuple(reasons))


def _tier_failure_codes(
    tier: BenchmarkTier,
    available_memory_bytes: int,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if tier.workers not in ALLOWED_WORKER_COUNTS or tier.workers < MIN_LOCAL_WORKERS:
        reasons.append("benchmark_worker_count_invalid")
    if tier.attempted <= 0 or tier.completed != tier.attempted:
        reasons.append("benchmark_tasks_failed")
    if not tier.digest_consistent:
        reasons.append("benchmark_digest_mismatch")
    if (
        not math.isfinite(tier.throughput_per_second)
        or tier.throughput_per_second < MIN_THROUGHPUT_PER_SECOND
    ):
        reasons.append("benchmark_throughput_too_low")
    if (
        not math.isfinite(tier.p95_seconds)
        or tier.p95_seconds <= 0
        or tier.p95_seconds > MAX_P95_SECONDS
    ):
        reasons.append("benchmark_p95_too_high")
    required_memory = HOST_MEMORY_RESERVE_BYTES + math.ceil(
        max(0, tier.peak_rss_bytes) * BENCHMARK_MEMORY_HEADROOM
    )
    if tier.peak_rss_bytes <= 0 or available_memory_bytes < required_memory:
        reasons.append("benchmark_memory_budget_exceeded")
    return tuple(reasons)


def schedule_capacity(
    snapshot: SystemSnapshot,
    report: BenchmarkReport | None,
    mode: ResourceMode,
) -> CapacityDecision:
    hardware = review_hardware(snapshot, mode)
    if hardware.hardware_worker_capacity < MIN_LOCAL_WORKERS:
        return hardware

    if report is None or not report.preprocessor_available:
        return CapacityDecision(
            "server_required",
            0,
            hardware.hardware_worker_capacity,
            (*hardware.reason_codes, "preprocessor_unavailable"),
        )
    if report.policy_version != POLICY_VERSION:
        return CapacityDecision(
            "server_required",
            0,
            hardware.hardware_worker_capacity,
            (*hardware.reason_codes, "benchmark_policy_mismatch"),
        )
    if (
        not 1 <= len(report.algorithm_version) <= 64
        or len(report.fixture_sha256) != 64
        or any(value not in _LOWER_HEX for value in report.fixture_sha256)
    ):
        return CapacityDecision(
            "server_required",
            0,
            hardware.hardware_worker_capacity,
            (*hardware.reason_codes, "benchmark_identity_invalid"),
        )

    assert snapshot.available_memory_bytes is not None
    tiers = {tier.workers: tier for tier in report.tiers}
    if len(tiers) != len(report.tiers):
        return CapacityDecision(
            "server_required",
            0,
            hardware.hardware_worker_capacity,
            (*hardware.reason_codes, "benchmark_tiers_duplicate"),
        )
    base = tiers.get(MIN_LOCAL_WORKERS)
    if base is None:
        return CapacityDecision(
            "server_required",
            0,
            hardware.hardware_worker_capacity,
            (*hardware.reason_codes, "minimum_benchmark_tier_missing"),
        )
    base_failures = _tier_failure_codes(base, snapshot.available_memory_bytes)
    if base_failures:
        return CapacityDecision(
            "server_required",
            0,
            hardware.hardware_worker_capacity,
            (*hardware.reason_codes, *base_failures),
        )

    selected = MIN_LOCAL_WORKERS
    previous_throughput = base.throughput_per_second
    for worker_count in ALLOWED_WORKER_COUNTS:
        if worker_count <= MIN_LOCAL_WORKERS:
            continue
        if worker_count > hardware.hardware_worker_capacity:
            break
        tier = tiers.get(worker_count)
        if tier is None or _tier_failure_codes(tier, snapshot.available_memory_bytes):
            break
        gain = (tier.throughput_per_second - previous_throughput) / previous_throughput
        if gain < MIN_TIER_THROUGHPUT_GAIN:
            break
        selected = worker_count
        previous_throughput = tier.throughput_per_second

    return CapacityDecision(
        "local_cpu",
        selected,
        hardware.hardware_worker_capacity,
        hardware.reason_codes,
    )
