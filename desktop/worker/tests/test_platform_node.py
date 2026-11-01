from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path
import threading

import pytest

from mathagent_worker.handoff import HandoffSnapshot
from mathagent_worker.platform_node import (
    ACCESS_SCOPE,
    PlatformCredential,
    PlatformNodeError,
    configure_platform_credential,
    configure_provider_profile,
    read_platform_status,
    run_platform_node,
)
from mathagent_worker.provider_executor import ProviderProfile


def _credential() -> PlatformCredential:
    return PlatformCredential.from_payload(
        {
            "server": "https://api.math.example.com",
            "nodeId": "platform-node-1",
            "token": "x" * 32,
            "accessScope": "platform",
        }
    )


def _status(*, gpu: bool, cpu_workers: int = 0) -> dict:
    return {
        "hardwareWorkerCapacity": max(2, cpu_workers),
        "localProcessingAvailable": gpu or cpu_workers in (2, 3),
        "gpuAcceleration": {"enabled": gpu},
        "cpuAcceleration": {
            "available": cpu_workers in (2, 3),
            "qualified": cpu_workers in (2, 3),
            "workerCount": cpu_workers,
        },
    }


class FakeSupervisor:
    def __init__(self, status: dict) -> None:
        self.status = status

    def set_resource_mode(self, _value: str) -> dict:
        return self.status

    def get_status(self, *, refresh: bool = True) -> dict:
        assert refresh is True
        return self.status


class SequenceSupervisorFactory:
    def __init__(self, statuses: list[dict]) -> None:
        self._statuses = iter(statuses)

    def __call__(self) -> FakeSupervisor:
        return FakeSupervisor(next(self._statuses))


class FakeRunner:
    def __init__(self, _config, *, initially_paused: bool, **policy) -> None:
        self.initial_policy = dict(policy)
        self.policy = policy
        self.started = False
        self.stopped = False
        self.snapshot_value = HandoffSnapshot(
            configured=True,
            running=not initially_paused,
            connected=not initially_paused,
            state="paused" if initially_paused else "idle",
        )

    def start(self) -> None:
        self.started = True

    def stop(self, timeout=None) -> bool:
        assert timeout == 30.0
        self.stopped = True
        return True

    def snapshot(self) -> HandoffSnapshot:
        return self.snapshot_value

    def configure_local_policy(self, **policy) -> None:
        self.policy = policy

    def pause(self) -> None:
        self.snapshot_value = HandoffSnapshot(configured=True, state="paused")

    def resume(self) -> None:
        self.snapshot_value = HandoffSnapshot(
            configured=True,
            running=True,
            connected=True,
            state="idle",
        )


def test_platform_credential_requires_exact_platform_scope_and_https() -> None:
    with pytest.raises(PlatformNodeError, match="platform_scope_required"):
        PlatformCredential.from_payload(
            {
                "server": "https://api.math.example.com",
                "nodeId": "node",
                "token": "x" * 32,
                "accessScope": "tenant",
            }
        )
    with pytest.raises(PlatformNodeError, match="platform_server_origin_invalid"):
        PlatformCredential.from_payload(
            {
                "server": "http://api.math.example.com",
                "nodeId": "node",
                "token": "x" * 32,
                "accessScope": "platform",
            }
        )


def test_configuration_reads_secret_from_stdin_without_returning_it(monkeypatch) -> None:
    captured = []
    monkeypatch.setattr(
        "mathagent_worker.platform_node.write_platform_credential",
        captured.append,
    )
    token = "secret-platform-token-value-12345"
    payload = json.dumps(
        {
            "server": "https://api.math.example.com",
            "nodeId": "platform-node-1",
            "token": token,
            "accessScope": "platform",
        }
    )

    response = configure_platform_credential(io.StringIO(payload))

    assert response == {
        "status": "ok",
        "configured": True,
        "accessScope": "platform",
    }
    assert captured[0].token == token
    assert token not in json.dumps(response)


def test_configuration_accepts_one_windows_utf8_bom(monkeypatch) -> None:
    captured = []
    monkeypatch.setattr(
        "mathagent_worker.platform_node.write_platform_credential",
        captured.append,
    )
    payload = json.dumps(
        {
            "server": "https://api.math.example.com",
            "nodeId": "platform-node-1",
            "token": "x" * 32,
            "accessScope": "platform",
        }
    )

    response = configure_platform_credential(io.StringIO("\ufeff" + payload))

    assert response["status"] == "ok"
    assert len(captured) == 1


def test_provider_profile_configuration_uses_stdin_and_never_echoes_secret(
    monkeypatch,
) -> None:
    captured = []
    monkeypatch.setattr(
        "mathagent_worker.platform_node.write_provider_profile",
        lambda name, profile: captured.append((name, profile)),
    )
    api_key = "local-provider-secret-key"
    response = configure_provider_profile(
        "primary",
        io.StringIO(json.dumps({
            "baseUrl": "https://provider.example/v1",
            "model": "gpt-5.6-sol",
            "apiKey": api_key,
            "requestTimeoutSeconds": 300,
        })),
    )

    assert response == {
        "status": "ok",
        "configured": True,
        "providerProfile": "primary",
        "model": "gpt-5.6-sol",
    }
    assert captured[0][0] == "primary"
    assert captured[0][1].api_key == api_key
    assert api_key not in json.dumps(response)


def test_provider_profile_cannot_enable_api_work_without_image_hardware(
    tmp_path: Path,
) -> None:
    writes = []
    runners = []

    def runner_factory(*args, **kwargs):
        runner = FakeRunner(*args, **kwargs)
        runners.append(runner)
        return runner

    result = run_platform_node(
        stop_event=threading.Event(),
        status_path=tmp_path / "status.json",
        credential_loader=_credential,
        provider_loader=lambda: pytest.fail("provider profiles must not be loaded"),
        supervisor_factory=lambda: FakeSupervisor(_status(gpu=False)),
        runner_factory=runner_factory,
        scope_validator=lambda _credential: None,
        status_writer=lambda _path, payload: writes.append(payload),
        max_cycles=1,
    )

    assert result == 0
    assert runners[0].started is True
    assert runners[0].snapshot_value.state == "paused"
    assert "provider_profiles" not in runners[0].initial_policy
    assert any(payload.get("apiReady") is False for payload in writes)


def test_platform_runner_uses_gpu_sidecar_policy_and_safe_status(tmp_path: Path) -> None:
    writes = []
    runners = []

    def runner_factory(*args, **kwargs):
        runner = FakeRunner(*args, **kwargs)
        runners.append(runner)
        return runner

    result = run_platform_node(
        "fast",
        stop_event=threading.Event(),
        status_path=tmp_path / "status.json",
        credential_loader=_credential,
        provider_loader=lambda: {},
        supervisor_factory=lambda: FakeSupervisor(_status(gpu=True)),
        runner_factory=runner_factory,
        scope_validator=lambda _credential: None,
        status_writer=lambda _path, payload: writes.append(payload),
        max_cycles=1,
    )

    assert result == 0
    assert runners[0].started is True
    assert runners[0].stopped is True
    assert runners[0].policy == {
        "gpu_enabled": True,
        "cpu_fallback_allowed": False,
        "cpu_worker_count": 0,
    }
    assert any(payload.get("state") == "ready" for payload in writes)
    encoded = json.dumps(writes)
    assert _credential().token not in encoded
    assert _credential().server not in encoded


def test_platform_node_reprobes_gpu_after_cpu_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    writes = []
    runners = []

    def runner_factory(*args, **kwargs):
        runner = FakeRunner(*args, **kwargs)
        runners.append(runner)
        return runner

    monkeypatch.setattr(
        "mathagent_worker.platform_node._HARDWARE_REPROBE_SECONDS",
        0.0,
    )
    result = run_platform_node(
        "fast",
        stop_event=threading.Event(),
        status_path=tmp_path / "status.json",
        credential_loader=_credential,
        provider_loader=lambda: {},
        supervisor_factory=SequenceSupervisorFactory([
            _status(gpu=False, cpu_workers=3),
            _status(gpu=True),
        ]),
        runner_factory=runner_factory,
        scope_validator=lambda _credential: None,
        status_writer=lambda _path, payload: writes.append(payload),
        refresh_seconds=0,
        max_cycles=1,
    )

    assert result == 0
    assert runners[0].initial_policy == {
        "gpu_enabled": False,
        "cpu_fallback_allowed": True,
        "cpu_worker_count": 3,
    }
    assert runners[0].policy == {
        "gpu_enabled": True,
        "cpu_fallback_allowed": False,
        "cpu_worker_count": 0,
    }
    assert any(payload.get("route") == "local_gpu" for payload in writes)


def test_unavailable_hardware_stays_registered_but_advertises_no_work(
    tmp_path: Path,
) -> None:
    writes = []
    runners = []

    def runner_factory(*args, **kwargs):
        runner = FakeRunner(*args, **kwargs)
        runners.append(runner)
        return runner

    result = run_platform_node(
        stop_event=threading.Event(),
        status_path=tmp_path / "status.json",
        credential_loader=_credential,
        provider_loader=lambda: {},
        supervisor_factory=lambda: FakeSupervisor(_status(gpu=False)),
        runner_factory=runner_factory,
        scope_validator=lambda _credential: None,
        status_writer=lambda _path, payload: writes.append(payload),
        max_cycles=1,
    )

    assert result == 0
    assert runners[0].snapshot_value.state == "paused"
    assert runners[0].policy == {
        "gpu_enabled": False,
        "cpu_fallback_allowed": False,
        "cpu_worker_count": 0,
    }
    assert any(
        payload.get("state") == "hardware_unavailable"
        and payload.get("hardwareReady") is False
        for payload in writes
    )


def test_gpu_node_pauses_when_host_memory_or_cpu_capacity_gate_fails(
    tmp_path: Path,
) -> None:
    writes = []
    runners = []
    pressured = _status(gpu=True)
    pressured["hardwareWorkerCapacity"] = 0

    def runner_factory(*args, **kwargs):
        runner = FakeRunner(*args, **kwargs)
        runners.append(runner)
        return runner

    result = run_platform_node(
        stop_event=threading.Event(),
        status_path=tmp_path / "status.json",
        credential_loader=_credential,
        provider_loader=lambda: {},
        supervisor_factory=lambda: FakeSupervisor(pressured),
        runner_factory=runner_factory,
        scope_validator=lambda _credential: None,
        status_writer=lambda _path, payload: writes.append(payload),
        max_cycles=1,
    )

    assert result == 0
    assert runners[0].policy == {
        "gpu_enabled": False,
        "cpu_fallback_allowed": False,
        "cpu_worker_count": 0,
    }
    assert any(payload.get("state") == "hardware_unavailable" for payload in writes)


def test_missing_credential_fails_closed_without_starting_runner(tmp_path: Path) -> None:
    writes = []

    result = run_platform_node(
        stop_event=threading.Event(),
        status_path=tmp_path / "status.json",
        credential_loader=lambda: None,
        provider_loader=lambda: {},
        runner_factory=lambda *_args, **_kwargs: pytest.fail("runner must not start"),
        scope_validator=lambda _credential: pytest.fail("scope must not be checked"),
        status_writer=lambda _path, payload: writes.append(payload),
    )

    assert result == 2
    assert writes[-1]["state"] == "credential_missing"
    assert writes[-1]["accessScope"] == ACCESS_SCOPE


def test_status_reader_returns_bounded_non_secret_state(tmp_path: Path) -> None:
    status = tmp_path / "status.json"
    updated_at = datetime.now(timezone.utc).isoformat()
    status.write_text(
        json.dumps(
            {
                "state": "ready",
                "accessScope": "platform",
                "connected": True,
                "updatedAt": updated_at,
            }
        ),
        encoding="utf-8",
    )

    assert read_platform_status(status) == {
        "status": "ok",
        "platformNode": {
            "state": "ready",
            "accessScope": "platform",
            "connected": True,
            "updatedAt": updated_at,
        },
    }


def test_status_reader_never_echoes_unknown_secret_fields(tmp_path: Path) -> None:
    status = tmp_path / "status.json"
    status.write_text(
        json.dumps(
            {
                "state": "ready",
                "accessScope": "platform",
                "token": "must-not-be-returned",
                "server": "https://private.example.com",
            }
        ),
        encoding="utf-8",
    )

    encoded = json.dumps(read_platform_status(status))
    assert "must-not-be-returned" not in encoded
    assert "private.example.com" not in encoded


def test_status_reader_marks_an_old_live_state_stale(tmp_path: Path) -> None:
    status = tmp_path / "status.json"
    status.write_text(
        json.dumps(
            {
                "state": "ready",
                "accessScope": "platform",
                "connected": True,
                "updatedAt": "2020-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    value = read_platform_status(status)["platformNode"]
    assert value["state"] == "stale"
    assert value["connected"] is False
    assert value["lastErrorCode"] == "platform_status_stale"
