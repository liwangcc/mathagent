from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from mathagent_worker import gpu_backend, vksift_resident
from mathagent_worker.gpu_backend import (
    CPU_BACKEND,
    GPU_BACKEND,
    GPU_BACKEND_VERSION,
    GpuSelfTestResult,
    _canonical_feature_sha256,
    _contract_sample_points,
    conservative_homography,
    select_backend,
    subprocess_environment,
)


def _self_test(*, enabled: bool = True, reason: str | None = None) -> GpuSelfTestResult:
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
        fallback_reason=reason,
    )


@pytest.mark.parametrize("tampered", [False, True], ids=["bundled", "tampered"])
def test_gpu_known_answer_fixture_matches_the_shared_calibration_constant(
    monkeypatch, tmp_path, tampered
) -> None:
    # Exercise the fixture gate without requiring NVIDIA hardware in CI.
    monkeypatch.setattr(gpu_backend, "os", SimpleNamespace(name="nt", environ={}))
    if tampered:
        source = gpu_backend._fixture_path()
        fixture = tmp_path / source.name
        fixture.write_bytes(source.read_bytes() + b"\x00")
        monkeypatch.setattr(gpu_backend, "_fixture_path", lambda: fixture)

    probes = []

    def probe_without_nvidia():
        probes.append(True)
        return {"vendor_id": 0}

    monkeypatch.setattr(vksift_resident, "probe_resident", probe_without_nvidia)
    result = gpu_backend.run_gpu_self_test()

    assert result.enabled is False
    if tampered:
        assert result.fallback_reason == "gpu_known_answer_fixture_mismatch"
        assert probes == []
    else:
        assert result.fallback_reason == "nvidia_device_unavailable"
        assert probes == [True]

def test_conservative_gpu_geometry_uses_qualified_usac_contract() -> None:
    calls = []

    class FakeCv2:
        USAC_ACCURATE = 36

        @staticmethod
        def findHomography(source, target, method, threshold):
            calls.append((source, target, method, threshold))
            return "H", "mask"

    assert conservative_homography("source", "target", FakeCv2) == ("H", "mask")
    assert calls == [("source", "target", FakeCv2.USAC_ACCURATE, 3.0)]


def test_enabled_gpu_route_reports_bounded_backend_metadata() -> None:
    value, metrics = select_backend(_self_test(), lambda: "gpu", lambda: "cpu")

    assert value == "gpu"
    assert metrics == {
        "backend": GPU_BACKEND,
        "device_name": "NVIDIA GeForce RTX 4060 Laptop GPU",
        "driver_version": "591.74",
        "backend_version": GPU_BACKEND_VERSION,
        "fallback_reason": None,
    }


def test_gpu_failure_executes_real_cpu_fallback() -> None:
    calls = []

    def broken_gpu():
        calls.append("gpu")
        raise RuntimeError("boom")

    def cpu():
        calls.append("cpu")
        return "cpu-result"

    value, metrics = select_backend(_self_test(), broken_gpu, cpu)

    assert value == "cpu-result"
    assert calls == ["gpu", "cpu"]
    assert metrics["backend"] == CPU_BACKEND
    assert metrics["fallback_reason"] == "gpu_operation_failed"


def test_failed_geometry_self_test_never_invokes_gpu() -> None:
    calls = []
    self_test = _self_test(enabled=False, reason="gpu_geometry_contract_mismatch")

    value, metrics = select_backend(
        self_test,
        lambda: calls.append("gpu"),
        lambda: calls.append("cpu") or "safe",
    )

    assert value == "safe"
    assert calls == ["cpu"]
    assert metrics["fallback_reason"] == "gpu_geometry_contract_mismatch"


def test_canonical_feature_hash_ignores_record_order(tmp_path) -> None:
    import struct

    record = struct.Struct("<ffff128s")
    left = record.pack(1.0, 2.0, 3.0, 4.0, bytes(range(128)))
    right = record.pack(9.0, 8.0, 7.0, 6.0, bytes(reversed(range(128))))
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(struct.pack("<I", 2) + left + right)
    second.write_bytes(struct.pack("<I", 2) + right + left)

    assert _canonical_feature_sha256(first) == _canonical_feature_sha256(second)


def test_canonical_feature_hash_rejects_truncated_output(tmp_path) -> None:
    broken = tmp_path / "broken.bin"
    broken.write_bytes(b"\x01\x00\x00\x00")

    with pytest.raises(RuntimeError, match="gpu_feature_output_invalid"):
        _canonical_feature_sha256(broken)


def test_contract_points_use_page_boundaries_without_answer_regions() -> None:
    points = _contract_sample_points(
        1000,
        1400,
        ["1"],
    )

    assert points[:5] == [
        (0.0, 0.0),
        (999.0, 0.0),
        (999.0, 1399.0),
        (0.0, 1399.0),
        (500.0, 700.0),
    ]
    assert points[5:] == []


def test_native_helper_environment_excludes_node_credentials(monkeypatch) -> None:
    monkeypatch.setenv("MATHAGENT_ACCELERATOR_TOKEN", "secret")
    monkeypatch.setenv("MATHAGENT_ACCELERATOR_NODE_ID", "node")
    monkeypatch.setenv("PATH", r"C:\Windows\System32")

    value = subprocess_environment()

    assert value["PATH"] == r"C:\Windows\System32"
    assert "MATHAGENT_ACCELERATOR_TOKEN" not in value
    assert "MATHAGENT_ACCELERATOR_NODE_ID" not in value
