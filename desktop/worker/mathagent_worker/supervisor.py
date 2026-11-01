from __future__ import annotations

import math
import threading
from collections.abc import Callable

from .capacity import (
    MIN_LOCAL_WORKERS,
    POLICY_VERSION,
    BenchmarkReport,
    CapacityDecision,
    ResourceMode,
    SystemSnapshot,
    parse_resource_mode,
    run_cpu_benchmark,
    schedule_capacity,
)
from .gpu_backend import (
    CPU_BACKEND,
    GPU_BACKEND_VERSION,
    GpuSelfTestResult,
    run_gpu_self_test,
)
from .system_probe import inspect_system


Probe = Callable[[], SystemSnapshot]
GpuProbe = Callable[[], GpuSelfTestResult]
BenchmarkProbe = Callable[[], BenchmarkReport | None]


class CpuSupervisor:
    def __init__(
        self,
        *,
        probe: Probe = inspect_system,
        benchmark_report: BenchmarkReport | None = None,
        resource_mode: ResourceMode = ResourceMode.BALANCED,
        gpu_probe: GpuProbe = run_gpu_self_test,
        benchmark_probe: BenchmarkProbe | None = None,
    ) -> None:
        self._probe = probe
        self._benchmark_report = benchmark_report
        self._benchmark_probe = benchmark_probe or run_cpu_benchmark
        self._benchmark_attempted = benchmark_report is not None
        self._resource_mode = resource_mode
        self._gpu_probe = gpu_probe
        self._gpu_self_test: GpuSelfTestResult | None = None
        self._paused = False
        self._lock = threading.RLock()
        self._decision = CapacityDecision(
            "server_required", 0, 0, ("capacity_not_reviewed",)
        )

    def get_status(self, *, refresh: bool = True) -> dict:
        with self._lock:
            if refresh:
                self._refresh_locked()
            return self._status_locked()

    def refresh_capacity(self) -> dict:
        return self.get_status(refresh=True)

    def set_resource_mode(self, value: str) -> dict:
        mode = parse_resource_mode(value)
        with self._lock:
            self._resource_mode = mode
            self._refresh_locked()
            return self._status_locked()

    def pause(self) -> dict:
        with self._lock:
            self._paused = True
            return self._status_locked()

    def resume(self) -> dict:
        with self._lock:
            self._paused = False
            self._refresh_locked()
            return self._status_locked()

    def _refresh_locked(self) -> None:
        if self._gpu_self_test is None:
            try:
                self._gpu_self_test = self._gpu_probe()
            except Exception:
                self._gpu_self_test = GpuSelfTestResult(
                    available=False,
                    enabled=False,
                    backend=CPU_BACKEND,
                    backend_version=GPU_BACKEND_VERSION,
                    device_name=None,
                    driver_version=None,
                    gpu_time_ms=None,
                    cpu_time_ms=None,
                    speedup=None,
                    projection_max_px=None,
                    fallback_reason="gpu_self_test_failed",
                )
        if not self._benchmark_attempted:
            self._benchmark_attempted = True
            try:
                self._benchmark_report = self._benchmark_probe()
            except Exception:
                self._benchmark_report = None
        try:
            snapshot = self._probe()
            self._decision = schedule_capacity(
                snapshot,
                self._benchmark_report,
                self._resource_mode,
            )
        except Exception:
            self._decision = CapacityDecision(
                "server_required", 0, 0, ("capacity_probe_failed",)
            )

    def _status_locked(self) -> dict:
        decision = self._decision
        gpu = self._gpu_self_test
        gpu_ready = bool(gpu is not None and gpu.enabled)
        cpu_ready = bool(
            decision.route == "local_cpu"
            and decision.worker_count >= MIN_LOCAL_WORKERS
        )
        local_available = gpu_ready or cpu_ready
        cpu_reasons = list(dict.fromkeys(decision.reason_codes))
        if gpu_ready:
            reasons: tuple[str, ...] = ()
        else:
            reasons = decision.reason_codes
            if not cpu_ready and gpu is not None and gpu.fallback_reason:
                reasons = (*reasons, gpu.fallback_reason)
        if self._paused:
            route = "server_required"
            worker_count = 0
            capacity_state = "paused"
            local_processor_state = "paused"
            reasons = (*reasons, "user_paused")
        else:
            route = "server_required"
            worker_count = 0
            capacity_state = "local_ready" if local_available else "server_required"
            local_processor_state = "ready" if local_available else "unavailable"

        compute_backend = (
            gpu.backend
            if gpu_ready and gpu is not None
            else CPU_BACKEND if cpu_ready else "none"
        )
        gpu_fallback_reason = gpu.fallback_reason if gpu is not None else "gpu_self_test_not_run"
        if self._paused:
            gpu_state = "disabled"
        elif gpu_ready:
            gpu_state = "ready"
        elif gpu is not None and gpu_fallback_reason in {
            "gpu_known_answer_failed",
            "gpu_known_answer_timeout",
            "gpu_known_answer_mismatch",
            "gpu_known_answer_fixture_mismatch",
            "gpu_geometry_contract_mismatch",
            "gpu_timing_invalid",
            "gpu_self_test_failed",
        }:
            gpu_state = "self_test_failed"
        elif gpu is not None and gpu.available:
            gpu_state = "fallback"
        else:
            gpu_state = "unavailable"
        benchmark = self._benchmark_report
        benchmark_available = bool(
            benchmark is not None and benchmark.preprocessor_available
        )
        benchmark_tier = None
        if benchmark_available and benchmark is not None:
            tiers = {tier.workers: tier for tier in benchmark.tiers}
            benchmark_tier = tiers.get(
                decision.worker_count if cpu_ready else MIN_LOCAL_WORKERS
            )
        throughput = (
            benchmark_tier.throughput_per_second
            if benchmark_tier is not None
            and math.isfinite(benchmark_tier.throughput_per_second)
            and benchmark_tier.throughput_per_second >= 0
            else None
        )
        p95_ms = (
            benchmark_tier.p95_seconds * 1000.0
            if benchmark_tier is not None
            and math.isfinite(benchmark_tier.p95_seconds)
            and benchmark_tier.p95_seconds >= 0
            else None
        )
        cpu_fallback_reason = None
        if self._paused:
            cpu_state = "disabled"
            cpu_fallback_reason = "user_paused"
        elif cpu_ready:
            cpu_state = "ready"
        elif benchmark_available:
            cpu_state = "unqualified"
            cpu_fallback_reason = cpu_reasons[0] if cpu_reasons else "cpu_unqualified"
        else:
            cpu_state = "unavailable"
            cpu_fallback_reason = (
                cpu_reasons[0] if cpu_reasons else "preprocessor_unavailable"
            )

        return {
            "capacityState": capacity_state,
            "route": route,
            "handoffState": "not_integrated",
            "resourceMode": self._resource_mode.value,
            "workerCount": worker_count,
            "minimumWorkerCount": MIN_LOCAL_WORKERS,
            "hardwareWorkerCapacity": decision.hardware_worker_capacity,
            "paused": self._paused,
            "preprocessorAvailable": local_available,
            "localProcessingAvailable": local_available,
            "localProcessorState": local_processor_state,
            "webFallbackRequired": not self._paused and not local_available,
            "reasonCodes": list(dict.fromkeys(reasons)),
            "policyVersion": POLICY_VERSION,
            "thermalState": "unknown",
            "computeBackend": compute_backend,
            "gpuState": gpu_state,
            "deviceName": gpu.device_name if gpu is not None else None,
            "driverVersion": gpu.driver_version if gpu is not None else None,
            "backendVersion": gpu.backend_version if gpu is not None else GPU_BACKEND_VERSION,
            "batchSize": 1 if gpu_ready and not self._paused else 0,
            "processingTimeMs": gpu.gpu_time_ms if gpu is not None else None,
            "fallbackReason": (
                "user_paused"
                if self._paused
                else None if gpu_ready else gpu_fallback_reason
            ),
            "cpuAcceleration": {
                "available": benchmark_available,
                "qualified": cpu_ready,
                "enabled": cpu_ready and not self._paused,
                "state": cpu_state,
                "backend": CPU_BACKEND,
                "backendVersion": "opencv-sift-template-registration-v2",
                "workerCount": decision.worker_count if cpu_ready else 0,
                "hardwareWorkerCapacity": decision.hardware_worker_capacity,
                "policyVersion": POLICY_VERSION,
                "benchmarkVersion": (
                    benchmark.policy_version if benchmark_available and benchmark else None
                ),
                "fixtureSha256": (
                    benchmark.fixture_sha256 if benchmark is not None else None
                ),
                "throughputPerSecond": throughput,
                "p95Ms": p95_ms,
                "fallbackReason": cpu_fallback_reason,
                "reasonCodes": [] if cpu_ready else cpu_reasons,
            },
            "gpuAcceleration": (
                gpu.as_status() if gpu is not None else {
                    "available": False,
                    "enabled": False,
                    "backend": "windows_cpu_opencv",
                    "backendVersion": "unknown",
                    "deviceName": None,
                    "driverVersion": None,
                    "gpuTimeMs": None,
                    "cpuTimeMs": None,
                    "speedup": None,
                    "projectionMaxPx": None,
                    "fallbackReason": "gpu_self_test_not_run",
                }
            ),
        }
