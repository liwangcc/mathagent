from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import TextIO

from . import __version__

PROTOCOL_VERSION = 5
_REQUEST_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
)


@dataclass(frozen=True)
class HealthResponse:
    status: str = "ok"
    service: str = "mathagent-desktop-sidecar"
    version: str = __version__
    protocol_version: int = PROTOCOL_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=True, separators=(",", ":"))


def health_json() -> str:
    return HealthResponse().to_json()


def capacity_json(resource_mode: str = "balanced") -> str:
    from .supervisor import CpuSupervisor

    supervisor = CpuSupervisor()
    status = supervisor.set_resource_mode(resource_mode)
    return _json({"status": "ok", "workerStatus": status})


def _local_policy(status: dict) -> tuple[bool, bool, int, bool]:
    gpu = status.get("gpuAcceleration")
    cpu = status.get("cpuAcceleration")
    gpu_enabled = bool(isinstance(gpu, dict) and gpu.get("enabled") is True)
    cpu_worker_count = 0
    cpu_available = False
    if (
        isinstance(cpu, dict)
        and cpu.get("available") is True
        and cpu.get("qualified") is True
    ):
        candidate = cpu.get("workerCount")
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate in (2, 3):
            cpu_worker_count = candidate
            cpu_available = True
    local_available = bool(
        status.get("localProcessingAvailable") is True
        and (gpu_enabled or cpu_available)
    )
    return gpu_enabled, cpu_available, cpu_worker_count, local_available


def _integrate_handoff_status(status: dict, handoff_runner) -> dict:
    status = dict(status)
    gpu_enabled, cpu_available, cpu_workers, local_available = _local_policy(status)
    paused = status.get("paused") is True
    status["localProcessingAvailable"] = local_available
    status["preprocessorAvailable"] = local_available

    if handoff_runner is None:
        status.update({
            "route": "server_required",
            "handoffState": "not_integrated",
            "workerCount": 0,
            "webFallbackRequired": not paused and not local_available,
        })
        return status

    handoff_runner.configure_local_policy(
        gpu_enabled=gpu_enabled,
        cpu_fallback_allowed=cpu_available,
        cpu_worker_count=cpu_workers,
    )
    should_pause = paused or not local_available
    snapshot = handoff_runner.snapshot()
    if should_pause and snapshot.state != "paused":
        handoff_runner.pause()
    elif not should_pause and snapshot.state == "paused":
        handoff_runner.resume()

    handoff = handoff_runner.snapshot().as_status()
    status["handoff"] = handoff
    status["handoffState"] = handoff["state"]
    if paused:
        status.update({
            "route": "server_required",
            "capacityState": "paused",
            "localProcessorState": "paused",
            "workerCount": 0,
            "webFallbackRequired": False,
        })
    elif local_available:
        status.update({
            "route": "local_gpu" if gpu_enabled else "local_cpu",
            "capacityState": "local_ready",
            "localProcessorState": "ready",
            "workerCount": 1 if gpu_enabled else cpu_workers,
            "webFallbackRequired": False,
        })
    else:
        status.update({
            "route": "server_required",
            "capacityState": "server_required",
            "localProcessorState": "unavailable",
            "workerCount": 0,
            "webFallbackRequired": True,
        })
    return status


def serve(input_stream: TextIO, output_stream: TextIO, supervisor=None) -> int:
    output_stream.write(health_json() + "\n")
    output_stream.flush()

    active_supervisor = supervisor
    handoff_runner = None
    try:
        from .handoff import start_from_environment

        handoff_runner = start_from_environment(
            initially_paused=True,
            gpu_enabled=False,
            cpu_fallback_allowed=False,
            cpu_worker_count=0,
        )
    except Exception:
        handoff_runner = None
    if handoff_runner is not None:
        if active_supervisor is None:
            from .supervisor import CpuSupervisor

            active_supervisor = CpuSupervisor()
        try:
            initial_status = active_supervisor.get_status()
            _integrate_handoff_status(initial_status, handoff_runner)
        except Exception:
            handoff_runner.pause()
    try:
        first_line = True
        for raw_line in input_stream:
            if first_line and raw_line.startswith("\ufeff"):
                raw_line = raw_line[1:]
            first_line = False
            try:
                message = json.loads(raw_line, parse_constant=_reject_json_constant)
            except (json.JSONDecodeError, TypeError, ValueError):
                _write_error(output_stream, "invalid_json")
                continue

            if not isinstance(message, dict) or not isinstance(message.get("command"), str):
                _write_error(output_stream, "invalid_request")
                continue
            request_id = message.get("requestId")
            if request_id is not None and not _valid_request_id(request_id):
                _write_error(output_stream, "invalid_request_id")
                continue

            command = message["command"]
            if command == "shutdown":
                if handoff_runner is not None:
                    handoff_runner.stop()
                    handoff_runner = None
                _write(output_stream, {"status": "stopping"}, request_id=request_id)
                return 0

            if command == "canonicalize_template":
                expected_fields = {
                    "command",
                    "requestId",
                    "workspaceRoot",
                    "sourcePath",
                    "canonicalPath",
                }
                if request_id is None:
                    _write_error(output_stream, "invalid_request_id")
                    continue
                if set(message) != expected_fields:
                    _write_error(
                        output_stream,
                        "invalid_canonicalization_request",
                        request_id=request_id,
                    )
                    continue
                if active_supervisor is None:
                    from .supervisor import CpuSupervisor

                    active_supervisor = CpuSupervisor()
                try:
                    local_status = active_supervisor.get_status()
                    _gpu, _cpu, _workers, local_available = _local_policy(local_status)
                except Exception:
                    _write_error(
                        output_stream,
                        "capacity_review_failed",
                        request_id=request_id,
                    )
                    continue
                if local_status.get("paused") is True:
                    _write_error(
                        output_stream,
                        "local_template_processing_paused",
                        request_id=request_id,
                    )
                    continue
                if not local_available:
                    _write_error(
                        output_stream,
                        "local_template_processing_unavailable",
                        request_id=request_id,
                    )
                    continue
                try:
                    from .canonicalization import (
                        TemplateCanonicalizationError,
                        canonicalize_template,
                    )
                except Exception:
                    _write_error(
                        output_stream,
                        "template_canonicalization_failed",
                        request_id=request_id,
                    )
                    continue
                try:
                    result = canonicalize_template(
                        message.get("workspaceRoot"),
                        message.get("sourcePath"),
                        message.get("canonicalPath"),
                    )
                    _write(
                        output_stream,
                        {"status": "ok", **result.protocol_payload()},
                        request_id=request_id,
                    )
                except TemplateCanonicalizationError as exc:
                    _write_error(output_stream, exc.code, request_id=request_id)
                except Exception:
                    _write_error(
                        output_stream,
                        "template_canonicalization_failed",
                        request_id=request_id,
                    )
                continue

            if command == "configure_handoff":
                try:
                    from .handoff import HandoffConfig, DesktopHandoffRunner

                    config = HandoffConfig.from_payload(message.get("handoffConfig"))
                    if handoff_runner is not None:
                        handoff_runner.stop()
                    if active_supervisor is None:
                        from .supervisor import CpuSupervisor

                        active_supervisor = CpuSupervisor()
                    local_status = active_supervisor.get_status()
                    gpu_enabled, cpu_available, cpu_workers, local_available = _local_policy(
                        local_status
                    )
                    handoff_runner = DesktopHandoffRunner(
                        config,
                        initially_paused=not local_available,
                        gpu_enabled=gpu_enabled,
                        cpu_fallback_allowed=cpu_available,
                        cpu_worker_count=cpu_workers,
                    )
                    handoff_runner.start()
                    status = _integrate_handoff_status(local_status, handoff_runner)
                    _write_worker_status(output_stream, status, request_id=request_id)
                except ValueError as exc:
                    _write_error(output_stream, str(exc), request_id=request_id)
                except Exception:
                    _write_error(output_stream, "handoff_configuration_failed", request_id=request_id)
                continue

            if command == "clear_handoff":
                if handoff_runner is not None:
                    handoff_runner.stop()
                    handoff_runner = None
                if active_supervisor is None:
                    from .supervisor import CpuSupervisor

                    active_supervisor = CpuSupervisor()
                status = _integrate_handoff_status(active_supervisor.get_status(), None)
                _write_worker_status(output_stream, status, request_id=request_id)
                continue

            if command not in {
                "get_worker_status",
                "refresh_capacity",
                "set_resource_mode",
                "pause_worker",
                "resume_worker",
            }:
                _write_error(output_stream, "unsupported_command", request_id=request_id)
                continue

            if active_supervisor is None:
                from .supervisor import CpuSupervisor

                active_supervisor = CpuSupervisor()

            try:
                if command == "get_worker_status":
                    status = active_supervisor.get_status()
                elif command == "refresh_capacity":
                    status = active_supervisor.refresh_capacity()
                elif command == "set_resource_mode":
                    mode = message.get("mode")
                    if not isinstance(mode, str):
                        raise ValueError("invalid_resource_mode")
                    status = active_supervisor.set_resource_mode(mode)
                elif command == "pause_worker":
                    status = active_supervisor.pause()
                    if handoff_runner is not None:
                        handoff_runner.pause()
                else:
                    status = active_supervisor.resume()
                    if handoff_runner is not None:
                        handoff_runner.resume()
                status = _integrate_handoff_status(status, handoff_runner)
            except ValueError as exc:
                code = str(exc)
                _write_error(
                    output_stream,
                    code if code == "invalid_resource_mode" else "invalid_request",
                    request_id=request_id,
                )
                continue
            except Exception:
                _write_error(output_stream, "capacity_review_failed", request_id=request_id)
                continue

            _write_worker_status(output_stream, status, request_id=request_id)
        return 0
    finally:
        if handoff_runner is not None:
            handoff_runner.stop()


def _valid_request_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 64
        and all(character in _REQUEST_ID_CHARS for character in value)
    )


def _reject_json_constant(_value: str):
    raise ValueError("non-finite JSON number")


def _json(payload: dict) -> str:
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _write(output_stream: TextIO, payload: dict, *, request_id: str | None = None) -> None:
    if request_id is not None:
        payload = {**payload, "requestId": request_id}
    output_stream.write(_json(payload) + "\n")
    output_stream.flush()


def _write_error(
    output_stream: TextIO,
    code: str,
    *,
    request_id: str | None = None,
) -> None:
    _write(
        output_stream,
        {"status": "error", "errorCode": code},
        request_id=request_id,
    )


def _write_worker_status(
    output_stream: TextIO,
    status: dict,
    *,
    request_id: str | None = None,
) -> None:
    try:
        _write(
            output_stream,
            {"status": "ok", "workerStatus": status},
            request_id=request_id,
        )
    except (TypeError, ValueError):
        _write_error(
            output_stream,
            "capacity_review_failed",
            request_id=request_id,
        )
