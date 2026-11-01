from __future__ import annotations

from mathagent_worker.capacity import (
    CPU_BENCHMARK_FIXTURE_SHA256,
    GIB,
    POLICY_VERSION,
    BenchmarkReport,
    BenchmarkTier,
    ResourceMode,
    SystemSnapshot,
    run_cpu_benchmark,
    schedule_capacity,
)
from mathagent_worker.gpu_backend import (
    CPU_BACKEND,
    GPU_BACKEND,
    GPU_BACKEND_VERSION,
    GpuSelfTestResult,
)
from mathagent_worker.supervisor import CpuSupervisor


def _snapshot(
    *,
    physical_cores: int = 10,
    available_memory_bytes: int = 8 * GIB,
    cpu_percent: float = 20.0,
    on_battery: bool = False,
) -> SystemSnapshot:
    return SystemSnapshot(
        physical_cores=physical_cores,
        logical_cores=physical_cores * 2,
        available_memory_bytes=available_memory_bytes,
        cpu_percent=cpu_percent,
        on_battery=on_battery,
    )


def _tier(
    workers: int,
    throughput: float,
    *,
    p95: float = 2.0,
    completed: int = 20,
    attempted: int = 20,
    digest_consistent: bool = True,
) -> BenchmarkTier:
    return BenchmarkTier(
        workers=workers,
        throughput_per_second=throughput,
        p95_seconds=p95,
        peak_rss_bytes=workers * 256 * 1024**2,
        completed=completed,
        attempted=attempted,
        digest_consistent=digest_consistent,
    )


def _report(*tiers: BenchmarkTier) -> BenchmarkReport:
    return BenchmarkReport(
        policy_version=POLICY_VERSION,
        algorithm_version="template-registration-v2",
        fixture_sha256="a" * 64,
        preprocessor_available=True,
        tiers=tiers,
    )


def _gpu_result(*, enabled: bool = True) -> GpuSelfTestResult:
    return GpuSelfTestResult(
        available=True,
        enabled=enabled,
        backend=GPU_BACKEND if enabled else CPU_BACKEND,
        backend_version=GPU_BACKEND_VERSION,
        device_name="NVIDIA GeForce RTX 4060 Laptop GPU",
        driver_version="591.74",
        gpu_time_ms=8.1,
        cpu_time_ms=579.1,
        speedup=71.49,
        projection_max_px=5.282679 if enabled else 9.110979,
        fallback_reason=None if enabled else "gpu_geometry_contract_mismatch",
    )


def test_capacity_never_schedules_a_single_worker() -> None:
    decision = schedule_capacity(
        _snapshot(physical_cores=3),
        _report(_tier(2, 1.0)),
        ResourceMode.FAST,
    )

    assert decision.route == "server_required"
    assert decision.worker_count == 0
    assert decision.hardware_worker_capacity == 0
    assert "insufficient_physical_cores" in decision.reason_codes


def test_two_worker_tier_is_the_minimum_local_capacity() -> None:
    decision = schedule_capacity(
        _snapshot(physical_cores=4),
        _report(_tier(2, 0.9)),
        ResourceMode.BALANCED,
    )

    assert decision.route == "local_cpu"
    assert decision.worker_count == 2
    assert decision.hardware_worker_capacity == 2


def test_minimum_tier_must_meet_speed_latency_and_consistency_gates() -> None:
    slow = schedule_capacity(
        _snapshot(),
        _report(_tier(2, 0.79)),
        ResourceMode.BALANCED,
    )
    high_latency = schedule_capacity(
        _snapshot(),
        _report(_tier(2, 1.0, p95=3.01)),
        ResourceMode.BALANCED,
    )
    inconsistent = schedule_capacity(
        _snapshot(),
        _report(_tier(2, 1.0, digest_consistent=False)),
        ResourceMode.BALANCED,
    )

    assert slow.worker_count == 0
    assert "benchmark_throughput_too_low" in slow.reason_codes
    assert high_latency.worker_count == 0
    assert "benchmark_p95_too_high" in high_latency.reason_codes
    assert inconsistent.worker_count == 0
    assert "benchmark_digest_mismatch" in inconsistent.reason_codes


def test_benchmark_identity_and_tiers_are_strict() -> None:
    invalid_identity = BenchmarkReport(
        policy_version=POLICY_VERSION,
        algorithm_version="template-registration-v2",
        fixture_sha256="Z" * 64,
        preprocessor_available=True,
        tiers=(_tier(2, 1.0),),
    )
    duplicate_tiers = _report(_tier(2, 1.0), _tier(2, 1.1))

    invalid = schedule_capacity(
        _snapshot(), invalid_identity, ResourceMode.BALANCED
    )
    duplicate = schedule_capacity(
        _snapshot(), duplicate_tiers, ResourceMode.BALANCED
    )

    assert invalid.worker_count == 0
    assert "benchmark_identity_invalid" in invalid.reason_codes
    assert duplicate.worker_count == 0
    assert "benchmark_tiers_duplicate" in duplicate.reason_codes


def test_balanced_and_fast_modes_cap_cpu_compute_at_three() -> None:
    report = _report(
        _tier(2, 1.0),
        _tier(3, 1.4),
    )

    balanced = schedule_capacity(_snapshot(), report, ResourceMode.BALANCED)
    fast = schedule_capacity(_snapshot(), report, ResourceMode.FAST)

    assert balanced.worker_count == 3
    assert fast.worker_count == 3


def test_marginal_tier_is_not_selected() -> None:
    decision = schedule_capacity(
        _snapshot(),
        _report(
            _tier(2, 1.0),
            _tier(3, 1.05),
        ),
        ResourceMode.BALANCED,
    )

    assert decision.worker_count == 2


def test_runtime_pressure_routes_new_work_to_the_server() -> None:
    decision = schedule_capacity(
        _snapshot(cpu_percent=80.0),
        _report(_tier(2, 1.0)),
        ResourceMode.BALANCED,
    )

    assert decision.route == "server_required"
    assert decision.worker_count == 0
    assert "cpu_busy" in decision.reason_codes


def test_battery_power_reduces_the_selected_hardware_tier() -> None:
    decision = schedule_capacity(
        _snapshot(on_battery=True),
        _report(
            _tier(2, 1.0),
            _tier(3, 1.4),
        ),
        ResourceMode.BALANCED,
    )

    assert decision.hardware_worker_capacity == 2
    assert decision.worker_count == 2
    assert "on_battery_capacity_reduced" in decision.reason_codes


def test_supervisor_caches_cpu_qualification_and_reports_local_gpu_capability() -> None:
    calls = []
    supervisor = CpuSupervisor(
        probe=_snapshot,
        gpu_probe=lambda: _gpu_result(),
        benchmark_probe=lambda: calls.append("benchmark") or _report(
            _tier(2, 1.0),
            _tier(3, 1.4),
        ),
    )

    status = supervisor.get_status()
    supervisor.refresh_capacity()

    assert status["route"] == "server_required"
    assert status["handoffState"] == "not_integrated"
    assert status["workerCount"] == 0
    assert status["hardwareWorkerCapacity"] == 3
    assert status["minimumWorkerCount"] == 2
    assert status["preprocessorAvailable"] is True
    assert status["localProcessingAvailable"] is True
    assert status["localProcessorState"] == "ready"
    assert status["webFallbackRequired"] is False
    assert status["computeBackend"] == GPU_BACKEND
    assert status["gpuState"] == "ready"
    assert status["deviceName"] == "NVIDIA GeForce RTX 4060 Laptop GPU"
    assert status["driverVersion"] == "591.74"
    assert status["backendVersion"] == GPU_BACKEND_VERSION
    assert status["batchSize"] == 1
    assert status["processingTimeMs"] == 8.1
    assert status["fallbackReason"] is None
    assert status["cpuAcceleration"]["available"] is True
    assert status["cpuAcceleration"]["qualified"] is True
    assert status["cpuAcceleration"]["workerCount"] == 3
    assert status["gpuAcceleration"]["enabled"] is True
    assert status["gpuAcceleration"]["deviceName"] == "NVIDIA GeForce RTX 4060 Laptop GPU"
    assert calls == ["benchmark"]


def test_gpu_geometry_failure_is_visible_and_keeps_cpu_backend() -> None:
    supervisor = CpuSupervisor(
        probe=_snapshot,
        benchmark_report=_report(_tier(2, 1.0), _tier(3, 1.4)),
        gpu_probe=lambda: _gpu_result(enabled=False),
    )

    status = supervisor.get_status()

    assert status["computeBackend"] == CPU_BACKEND
    assert status["localProcessingAvailable"] is True
    assert status["cpuAcceleration"]["available"] is True
    assert status["gpuAcceleration"]["enabled"] is False
    assert status["gpuAcceleration"]["fallbackReason"] == "gpu_geometry_contract_mismatch"


def test_pause_and_resume_do_not_create_a_one_worker_state() -> None:
    supervisor = CpuSupervisor(
        probe=lambda: _snapshot(physical_cores=4),
        benchmark_report=_report(_tier(2, 1.0)),
        gpu_probe=lambda: _gpu_result(),
    )

    assert supervisor.get_status()["workerCount"] == 0
    assert supervisor.get_status()["cpuAcceleration"]["workerCount"] == 2
    paused = supervisor.pause()
    assert paused["capacityState"] == "paused"
    assert paused["workerCount"] == 0
    assert paused["route"] == "server_required"
    assert paused["localProcessingAvailable"] is True
    assert paused["localProcessorState"] == "paused"
    assert paused["webFallbackRequired"] is False
    resumed = supervisor.resume()
    assert resumed["workerCount"] == 0
    assert resumed["cpuAcceleration"]["workerCount"] == 2


def test_both_unqualified_backends_fail_closed() -> None:
    supervisor = CpuSupervisor(
        probe=_snapshot,
        benchmark_probe=lambda: None,
        gpu_probe=lambda: _gpu_result(enabled=False),
    )

    status = supervisor.get_status()

    assert status["route"] == "server_required"
    assert status["computeBackend"] == "none"
    assert status["localProcessingAvailable"] is False
    assert status["localProcessorState"] == "unavailable"
    assert status["webFallbackRequired"] is True
    assert status["gpuState"] == "self_test_failed"
    assert status["batchSize"] == 0
    assert status["cpuAcceleration"]["available"] is False
    assert status["cpuAcceleration"]["qualified"] is False


def test_real_known_answer_cpu_benchmark_qualifies_only_two_or_three_workers() -> None:
    run_cpu_benchmark.cache_clear()
    try:
        report = run_cpu_benchmark()
        supervisor = CpuSupervisor(
            probe=_snapshot,
            gpu_probe=lambda: _gpu_result(enabled=False),
        )
        status = supervisor.get_status()
    finally:
        run_cpu_benchmark.cache_clear()

    assert report.preprocessor_available is True
    assert report.fixture_sha256 == CPU_BENCHMARK_FIXTURE_SHA256
    assert [tier.workers for tier in report.tiers] == [2, 3]
    assert all(tier.completed == tier.attempted for tier in report.tiers)
    assert all(tier.digest_consistent for tier in report.tiers)
    assert status["cpuAcceleration"]["available"] is True
    assert status["cpuAcceleration"]["workerCount"] in (2, 3)


def test_failed_cpu_benchmark_is_cached_and_fails_closed(monkeypatch) -> None:
    import mathagent_worker.capacity as capacity_module

    calls = []

    def broken():
        calls.append("run")
        raise RuntimeError("fixture failure")

    monkeypatch.setattr(capacity_module, "_run_cpu_benchmark_uncached", broken)
    run_cpu_benchmark.cache_clear()
    try:
        first = run_cpu_benchmark()
        second = run_cpu_benchmark()
    finally:
        run_cpu_benchmark.cache_clear()

    assert first is second
    assert first.preprocessor_available is False
    assert first.tiers == ()
    assert calls == ["run"]
