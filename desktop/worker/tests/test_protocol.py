from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
from copy import deepcopy
from pathlib import Path

from mathagent_worker import __version__
from mathagent_worker.protocol import PROTOCOL_VERSION, health_json, serve


WORKER_ROOT = Path(__file__).resolve().parents[1]

WORKER_STATUS = {
    "capacityState": "server_required",
    "route": "server_required",
    "handoffState": "not_integrated",
    "resourceMode": "balanced",
    "workerCount": 0,
    "minimumWorkerCount": 2,
    "hardwareWorkerCapacity": 6,
    "paused": False,
    "preprocessorAvailable": False,
    "localProcessingAvailable": False,
    "localProcessorState": "unavailable",
    "webFallbackRequired": True,
    "reasonCodes": ["preprocessor_unavailable"],
    "policyVersion": "cpu-capacity-v2",
    "thermalState": "unknown",
    "computeBackend": "none",
    "cpuAcceleration": {
        "available": False,
        "qualified": False,
        "enabled": False,
        "state": "unavailable",
        "backend": "windows_cpu_opencv",
        "backendVersion": "opencv-sift-template-registration-v2",
        "workerCount": 0,
        "hardwareWorkerCapacity": 6,
        "policyVersion": "cpu-capacity-v2",
        "benchmarkVersion": None,
        "fixtureSha256": None,
        "throughputPerSecond": None,
        "p95Ms": None,
        "fallbackReason": "preprocessor_unavailable",
        "reasonCodes": ["preprocessor_unavailable"],
    },
    "gpuAcceleration": {
        "available": False,
        "enabled": False,
        "backend": "windows_cpu_opencv",
        "backendVersion": "vulkansift-04245a5e-usac-accurate-v1",
        "deviceName": None,
        "driverVersion": None,
        "gpuTimeMs": None,
        "cpuTimeMs": None,
        "speedup": None,
        "projectionMaxPx": None,
        "fallbackReason": "gpu_runtime_missing",
    },
}


class FakeSupervisor:
    def __init__(self, *, local_ready: bool = False) -> None:
        self.status = deepcopy(WORKER_STATUS)
        if local_ready:
            self.status.update({
                "capacityState": "local_ready",
                "route": "local_cpu",
                "workerCount": 2,
                "preprocessorAvailable": True,
                "localProcessingAvailable": True,
                "localProcessorState": "ready",
                "webFallbackRequired": False,
                "computeBackend": "windows_cpu_opencv",
            })
            self.status["cpuAcceleration"].update({
                "available": True,
                "qualified": True,
                "enabled": True,
                "state": "ready",
                "workerCount": 2,
            })

    def get_status(self):
        return dict(self.status)

    def refresh_capacity(self):
        return dict(self.status)

    def set_resource_mode(self, mode):
        if mode not in {"quiet", "balanced", "fast"}:
            raise ValueError("invalid_resource_mode")
        self.status["resourceMode"] = mode
        return dict(self.status)

    def pause(self):
        self.status.update({
            "capacityState": "paused",
            "localProcessorState": "paused",
            "paused": True,
        })
        return dict(self.status)

    def resume(self):
        self.status.update({
            "capacityState": "server_required",
            "localProcessorState": "unavailable",
            "paused": False,
        })
        return dict(self.status)


class FakeHandoff:
    def __init__(self, *, state: str = "paused") -> None:
        self.state = state
        self.policy = None
        self.stopped = False

    def configure_local_policy(self, **policy):
        self.policy = policy

    def snapshot(self):
        from mathagent_worker.handoff import HandoffSnapshot

        return HandoffSnapshot(configured=True, state=self.state)

    def pause(self):
        self.state = "paused"

    def resume(self):
        self.state = "idle"

    def stop(self):
        self.stopped = True
        return True


def test_health_payload_is_small_and_non_sensitive() -> None:
    payload = json.loads(health_json())

    assert payload == {
        "status": "ok",
        "service": "mathagent-desktop-sidecar",
        "version": __version__,
        "protocol_version": PROTOCOL_VERSION,
    }
    assert "path" not in payload
    assert "environment" not in payload
    assert PROTOCOL_VERSION == 5


def test_configure_handoff_accepts_secret_over_stdin_without_echo(monkeypatch) -> None:
    from mathagent_worker import handoff as handoff_module

    captured = {}
    runner = FakeHandoff(state="idle")

    class ConfiguredRunner(FakeHandoff):
        def __init__(self, config, **kwargs):
            super().__init__(state="paused")
            captured["config"] = config
            captured["kwargs"] = kwargs

        def start(self):
            captured["started"] = True

    monkeypatch.setattr(handoff_module, "DesktopHandoffRunner", ConfiguredRunner)
    monkeypatch.setattr(handoff_module, "start_from_environment", lambda **_kwargs: None)
    output_stream = io.StringIO()
    secret = "desktop-secret-token-0123456789"
    request = json.dumps({
        "command": "configure_handoff",
        "requestId": "config-1",
        "handoffConfig": {
            "server": "https://api.math.example.com",
            "nodeId": "desktop-node-1",
            "token": secret,
            "pollSeconds": 5,
        },
    })
    assert serve(
        io.StringIO(request + "\n" + '{"command":"shutdown"}\n'),
        output_stream,
        FakeSupervisor(),
    ) == 0
    rendered = output_stream.getvalue()
    assert secret not in rendered
    assert "api.math.example.com" not in rendered
    assert captured["config"].token == secret
    assert captured["config"].node_id == "desktop-node-1"
    assert captured["started"] is True
    message = json.loads(rendered.splitlines()[1])
    assert message["status"] == "ok"
    assert message["requestId"] == "config-1"


def test_configure_handoff_rejects_non_https_origin_without_echo(monkeypatch) -> None:
    from mathagent_worker import handoff as handoff_module

    monkeypatch.setattr(handoff_module, "start_from_environment", lambda **_kwargs: None)
    output_stream = io.StringIO()
    request = json.dumps({
        "command": "configure_handoff",
        "requestId": "bad-config",
        "handoffConfig": {
            "server": "http://api.math.example.com",
            "nodeId": "desktop-node-1",
            "token": "desktop-secret-token-0123456789",
        },
    })
    serve(io.StringIO(request + "\n"), output_stream, FakeSupervisor())
    message = json.loads(output_stream.getvalue().splitlines()[1])
    assert message == {
        "status": "error",
        "errorCode": "server_https_required",
        "requestId": "bad-config",
    }


def test_serve_reports_errors_and_stops_cleanly() -> None:
    input_stream = io.StringIO(
        "not-json\n"
        '{"command":"future-command"}\n'
        '{"command":"shutdown"}\n'
    )
    output_stream = io.StringIO()

    assert serve(input_stream, output_stream) == 0
    messages = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert messages == [
        {
            "status": "ok",
            "service": "mathagent-desktop-sidecar",
            "version": __version__,
            "protocol_version": PROTOCOL_VERSION,
        },
        {"status": "error", "errorCode": "invalid_json"},
        {"status": "error", "errorCode": "unsupported_command"},
        {"status": "stopping"},
    ]


def test_serve_accepts_one_windows_utf8_bom_before_first_request() -> None:
    output_stream = io.StringIO()

    assert serve(
        io.StringIO('\ufeff{"command":"shutdown","requestId":"bom-1"}\n'),
        output_stream,
        FakeSupervisor(),
    ) == 0

    messages = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert messages[-1] == {
        "status": "stopping",
        "requestId": "bom-1",
    }


def test_shutdown_is_not_acknowledged_before_handoff_drains(monkeypatch) -> None:
    from mathagent_worker import handoff as handoff_module

    stop_started = threading.Event()
    release_stop = threading.Event()

    class BlockingHandoff:
        state = "paused"

        def configure_local_policy(self, **_kwargs):
            return None

        def snapshot(self):
            from mathagent_worker.handoff import HandoffSnapshot

            return HandoffSnapshot(configured=True, state=self.state)

        def pause(self):
            self.state = "paused"

        def stop(self):
            stop_started.set()
            assert release_stop.wait(timeout=5)
            return True

    monkeypatch.setattr(
        handoff_module,
        "start_from_environment",
        lambda **_kwargs: BlockingHandoff(),
    )
    output_stream = io.StringIO()
    result = []
    thread = threading.Thread(
        target=lambda: result.append(
            serve(
                io.StringIO('{"command":"shutdown"}\n'),
                output_stream,
                FakeSupervisor(),
            )
        )
    )

    thread.start()
    assert stop_started.wait(timeout=2)
    messages_before_drain = [
        json.loads(line) for line in output_stream.getvalue().splitlines()
    ]
    assert messages_before_drain == [json.loads(health_json())]

    release_stop.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert result == [0]
    messages = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert messages[-1] == {"status": "stopping"}


def test_serve_exposes_bounded_worker_control_commands() -> None:
    input_stream = io.StringIO(
        '{"command":"get_worker_status","requestId":"request-1"}\n'
        '{"command":"set_resource_mode","mode":"quiet","requestId":"request-2"}\n'
        '{"command":"pause_worker","requestId":"request-3"}\n'
        '{"command":"resume_worker","requestId":"request-4"}\n'
        '{"command":"shutdown","requestId":"request-5"}\n'
    )
    output_stream = io.StringIO()

    assert serve(input_stream, output_stream, FakeSupervisor()) == 0
    messages = [json.loads(line) for line in output_stream.getvalue().splitlines()]

    assert messages[1] == {
        "status": "ok",
        "workerStatus": WORKER_STATUS,
        "requestId": "request-1",
    }
    assert messages[2]["workerStatus"]["resourceMode"] == "quiet"
    assert messages[2]["requestId"] == "request-2"
    assert messages[3]["workerStatus"]["paused"] is True
    assert messages[3]["workerStatus"]["webFallbackRequired"] is False
    assert messages[4]["workerStatus"]["paused"] is False
    assert messages[5] == {"status": "stopping", "requestId": "request-5"}


def test_configured_handoff_does_not_force_unqualified_local_cpu(monkeypatch) -> None:
    from mathagent_worker import handoff as handoff_module

    created = {}
    runner = FakeHandoff()

    def start(**kwargs):
        created.update(kwargs)
        return runner

    monkeypatch.setattr(handoff_module, "start_from_environment", start)
    supervisor = FakeSupervisor()
    output_stream = io.StringIO()

    assert serve(
        io.StringIO(
            '{"command":"get_worker_status"}\n'
            '{"command":"shutdown"}\n'
        ),
        output_stream,
        supervisor,
    ) == 0
    messages = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    status = messages[1]["workerStatus"]

    assert created == {
        "initially_paused": True,
        "gpu_enabled": False,
        "cpu_fallback_allowed": False,
        "cpu_worker_count": 0,
    }
    assert status["route"] == "server_required"
    assert status["workerCount"] == 0
    assert status["localProcessingAvailable"] is False
    assert status["handoffState"] == "paused"
    assert status["webFallbackRequired"] is True
    assert runner.policy["cpu_fallback_allowed"] is False
    assert runner.stopped is True


def test_configured_handoff_selects_ready_gpu_without_cpu_qualification(monkeypatch) -> None:
    from mathagent_worker import handoff as handoff_module

    runner = FakeHandoff()
    monkeypatch.setattr(
        handoff_module,
        "start_from_environment",
        lambda **_kwargs: runner,
    )
    supervisor = FakeSupervisor()
    supervisor.status = deepcopy(WORKER_STATUS)
    supervisor.status.update({
        "capacityState": "local_ready",
        "preprocessorAvailable": True,
        "localProcessingAvailable": True,
        "localProcessorState": "ready",
        "computeBackend": "windows_nvidia_gpu",
    })
    supervisor.status["gpuAcceleration"].update({
        "available": True,
        "enabled": True,
        "backend": "windows_nvidia_gpu",
        "deviceName": "NVIDIA Test GPU",
        "fallbackReason": None,
    })
    output_stream = io.StringIO()

    assert serve(
        io.StringIO(
            '{"command":"get_worker_status"}\n'
            '{"command":"shutdown"}\n'
        ),
        output_stream,
        supervisor,
    ) == 0
    messages = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    status = messages[1]["workerStatus"]

    assert status["route"] == "local_gpu"
    assert status["workerCount"] == 1
    assert status["webFallbackRequired"] is False
    assert status["handoffState"] == "idle"
    assert runner.policy == {
        "gpu_enabled": True,
        "cpu_fallback_allowed": False,
        "cpu_worker_count": 0,
    }


def test_configured_handoff_selects_only_qualified_cpu_tier(monkeypatch) -> None:
    from mathagent_worker import handoff as handoff_module

    runner = FakeHandoff()
    monkeypatch.setattr(
        handoff_module,
        "start_from_environment",
        lambda **_kwargs: runner,
    )
    supervisor = FakeSupervisor()
    supervisor.status = deepcopy(WORKER_STATUS)
    supervisor.status.update({
        "capacityState": "local_ready",
        "preprocessorAvailable": True,
        "localProcessingAvailable": True,
        "localProcessorState": "ready",
        "computeBackend": "windows_cpu_opencv",
    })
    supervisor.status["cpuAcceleration"].update({
        "available": True,
        "qualified": True,
        "enabled": True,
        "state": "ready",
        "workerCount": 2,
        "fallbackReason": None,
        "reasonCodes": [],
    })
    output_stream = io.StringIO()

    assert serve(
        io.StringIO(
            '{"command":"get_worker_status"}\n'
            '{"command":"shutdown"}\n'
        ),
        output_stream,
        supervisor,
    ) == 0
    messages = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    status = messages[1]["workerStatus"]

    assert status["route"] == "local_cpu"
    assert status["workerCount"] == 2
    assert status["webFallbackRequired"] is False
    assert runner.policy == {
        "gpu_enabled": False,
        "cpu_fallback_allowed": True,
        "cpu_worker_count": 2,
    }


def test_serve_rejects_invalid_mode_and_request_id() -> None:
    input_stream = io.StringIO(
        '{"command":"set_resource_mode","mode":"turbo","requestId":"request-1"}\n'
        '{"command":"get_worker_status","requestId":"bad request"}\n'
        '{"command":"shutdown"}\n'
    )
    output_stream = io.StringIO()

    assert serve(input_stream, output_stream, FakeSupervisor()) == 0
    messages = [json.loads(line) for line in output_stream.getvalue().splitlines()]

    assert messages[1] == {
        "status": "error",
        "errorCode": "invalid_resource_mode",
        "requestId": "request-1",
    }
    assert messages[2] == {"status": "error", "errorCode": "invalid_request_id"}


def test_non_finite_worker_metric_fails_request_without_stopping_sidecar() -> None:
    supervisor = FakeSupervisor()
    supervisor.status["cpuAcceleration"]["p95Ms"] = float("nan")
    output_stream = io.StringIO()

    assert serve(
        io.StringIO(
            '{"command":"get_worker_status","requestId":"bad-metric"}\n'
            '{"command":"shutdown"}\n'
        ),
        output_stream,
        supervisor,
    ) == 0

    messages = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert messages[1] == {
        "status": "error",
        "errorCode": "capacity_review_failed",
        "requestId": "bad-metric",
    }
    assert messages[2] == {"status": "stopping"}


def test_canonicalize_template_command_has_an_exact_bounded_contract(
    tmp_path,
    monkeypatch,
) -> None:
    from mathagent_worker import canonicalization as canonicalization_module

    canonical_path = (tmp_path / "canonical" / "template.png").resolve()

    class Result:
        def protocol_payload(self):
            return {
                "canonicalPath": str(canonical_path),
                "canonicalization": {
                    "version": "paper-canonical-v1",
                    "algorithm": "opencv_page_quad_v1",
                    "source_sha256": "a" * 64,
                    "canonical_sha256": "b" * 64,
                    "source_to_canonical": [
                        [1.0, 0.0, 0.0],
                        [0.0, 1.0, 0.0],
                        [0.0, 0.0, 1.0],
                    ],
                },
            }

    calls = []

    def canonicalize(workspace_root, source_path, output_path):
        calls.append((workspace_root, source_path, output_path))
        return Result()

    monkeypatch.setattr(canonicalization_module, "canonicalize_template", canonicalize)
    request = {
        "command": "canonicalize_template",
        "requestId": "canonical-request-1",
        "workspaceRoot": str(tmp_path.resolve()),
        "sourcePath": str((tmp_path / "incoming" / "source.png").resolve()),
        "canonicalPath": str(canonical_path),
    }
    output_stream = io.StringIO()

    assert serve(
        io.StringIO(json.dumps(request) + "\n" + '{"command":"shutdown"}\n'),
        output_stream,
        FakeSupervisor(local_ready=True),
    ) == 0

    messages = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert calls == [(
        request["workspaceRoot"],
        request["sourcePath"],
        request["canonicalPath"],
    )]
    assert messages[1] == {
        "status": "ok",
        **Result().protocol_payload(),
        "requestId": "canonical-request-1",
    }


def test_canonicalize_template_rejects_missing_and_unknown_fields_before_work(
    tmp_path,
    monkeypatch,
) -> None:
    from mathagent_worker import canonicalization as canonicalization_module

    monkeypatch.setattr(
        canonicalization_module,
        "canonicalize_template",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    base = {
        "command": "canonicalize_template",
        "requestId": "canonical-fields",
        "workspaceRoot": str(tmp_path.resolve()),
        "sourcePath": str((tmp_path / "source.png").resolve()),
        "canonicalPath": str((tmp_path / "canonical.png").resolve()),
    }
    with_unknown = {**base, "algorithm": "caller-controlled"}
    without_request_id = dict(base)
    without_request_id.pop("requestId")
    output_stream = io.StringIO()

    assert serve(
        io.StringIO(
            json.dumps(with_unknown)
            + "\n"
            + json.dumps(without_request_id)
            + "\n"
            + '{"command":"shutdown"}\n'
        ),
        output_stream,
        FakeSupervisor(local_ready=True),
    ) == 0

    messages = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert messages[1] == {
        "status": "error",
        "errorCode": "invalid_canonicalization_request",
        "requestId": "canonical-fields",
    }
    assert messages[2] == {"status": "error", "errorCode": "invalid_request_id"}


def test_protocol_rejects_non_finite_json_and_non_finite_canonical_response(
    tmp_path,
    monkeypatch,
) -> None:
    from mathagent_worker import canonicalization as canonicalization_module

    class NonFiniteResult:
        def protocol_payload(self):
            return {
                "canonicalPath": str((tmp_path / "canonical.png").resolve()),
                "canonicalization": {"confidence": float("nan")},
            }

    monkeypatch.setattr(
        canonicalization_module,
        "canonicalize_template",
        lambda *_args: NonFiniteResult(),
    )
    request = {
        "command": "canonicalize_template",
        "requestId": "non-finite-output",
        "workspaceRoot": str(tmp_path.resolve()),
        "sourcePath": str((tmp_path / "source.png").resolve()),
        "canonicalPath": str((tmp_path / "canonical.png").resolve()),
    }
    output_stream = io.StringIO()

    assert serve(
        io.StringIO(
            '{"command":"set_resource_mode","mode":NaN}\n'
            + json.dumps(request)
            + "\n"
            + '{"command":"shutdown"}\n'
        ),
        output_stream,
        FakeSupervisor(local_ready=True),
    ) == 0

    messages = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert messages[1] == {"status": "error", "errorCode": "invalid_json"}
    assert messages[2] == {
        "status": "error",
        "errorCode": "template_canonicalization_failed",
        "requestId": "non-finite-output",
    }
    assert messages[3] == {"status": "stopping"}


def test_canonicalize_template_masks_unexpected_exception_details(
    tmp_path,
    monkeypatch,
) -> None:
    from mathagent_worker import canonicalization as canonicalization_module

    secret_detail = str((tmp_path / "private-teacher-name.png").resolve())
    monkeypatch.setattr(
        canonicalization_module,
        "canonicalize_template",
        lambda *_args: (_ for _ in ()).throw(RuntimeError(secret_detail)),
    )
    request = {
        "command": "canonicalize_template",
        "requestId": "masked-error",
        "workspaceRoot": str(tmp_path.resolve()),
        "sourcePath": secret_detail,
        "canonicalPath": str((tmp_path / "canonical.png").resolve()),
    }
    output_stream = io.StringIO()

    assert serve(
        io.StringIO(json.dumps(request) + "\n" + '{"command":"shutdown"}\n'),
        output_stream,
        FakeSupervisor(local_ready=True),
    ) == 0

    serialized = output_stream.getvalue()
    messages = [json.loads(line) for line in serialized.splitlines()]
    assert messages[1] == {
        "status": "error",
        "errorCode": "template_canonicalization_failed",
        "requestId": "masked-error",
    }
    assert secret_detail not in serialized


def test_canonicalize_template_rejects_work_after_local_processing_is_paused(
    tmp_path,
    monkeypatch,
) -> None:
    from mathagent_worker import canonicalization as canonicalization_module

    monkeypatch.setattr(
        canonicalization_module,
        "canonicalize_template",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    request = {
        "command": "canonicalize_template",
        "requestId": "canonical-paused",
        "workspaceRoot": str(tmp_path.resolve()),
        "sourcePath": str((tmp_path / "source.png").resolve()),
        "canonicalPath": str((tmp_path / "canonical.png").resolve()),
    }
    output_stream = io.StringIO()
    supervisor = FakeSupervisor(local_ready=True)

    assert serve(
        io.StringIO(
            '{"command":"pause_worker","requestId":"pause-before-template"}\n'
            + json.dumps(request)
            + "\n"
            + '{"command":"shutdown"}\n'
        ),
        output_stream,
        supervisor,
    ) == 0

    messages = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert messages[1]["workerStatus"]["paused"] is True
    assert messages[2] == {
        "status": "error",
        "errorCode": "local_template_processing_paused",
        "requestId": "canonical-paused",
    }


def test_module_health_command() -> None:
    process = subprocess.run(
        [sys.executable, "-m", "mathagent_worker", "--health"],
        check=True,
        capture_output=True,
        text=True,
        cwd=WORKER_ROOT,
    )

    assert json.loads(process.stdout) == json.loads(health_json())


def test_module_serve_accepts_windows_utf8_bom() -> None:
    process = subprocess.run(
        [sys.executable, "-m", "mathagent_worker", "--serve"],
        input=b'\xef\xbb\xbf{"command":"shutdown","requestId":"bom-cli"}\n',
        capture_output=True,
        cwd=WORKER_ROOT,
    )

    assert process.returncode == 0, process.stderr.decode("utf-8", errors="replace")
    messages = [json.loads(line) for line in process.stdout.decode("utf-8").splitlines()]
    assert messages[-1] == {
        "status": "stopping",
        "requestId": "bom-cli",
    }
    assert process.stderr == b""


def test_module_version_command() -> None:
    process = subprocess.run(
        [sys.executable, "-m", "mathagent_worker", "--version"],
        check=True,
        capture_output=True,
        text=True,
        cwd=WORKER_ROOT,
    )

    assert process.stdout.strip() == __version__
    assert process.stderr == ""
