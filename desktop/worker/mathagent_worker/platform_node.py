from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import tempfile
import threading
import time
from typing import Any, Callable, TextIO
from urllib.parse import urlparse

from . import __version__
from .handoff import DesktopHandoffError, DesktopHandoffRunner, HandoffConfig, HttpTransport
from .preprocessing import ALGORITHM_VERSION
from .provider_executor import ProviderExecutorError, ProviderProfile
from .protocol import PROTOCOL_VERSION, _local_policy
from .supervisor import CpuSupervisor


CREDENTIAL_TARGET = "MathAgent/PlatformAccelerator/v1"
PROVIDER_CREDENTIAL_TARGET_PREFIX = "MathAgent/PlatformProvider/v1/"
ACCESS_SCOPE = "platform"
PROVIDER_PROFILE_NAMES = ("primary", "failover", "accelerator", "retry")
_CREDENTIAL_USERNAME = "platform_accelerator"
_MAX_CREDENTIAL_BYTES = 2048
_MAX_PROVIDER_CREDENTIAL_BYTES = 2048
_STATUS_FILE_NAME = "status.json"
_DEFAULT_REFRESH_SECONDS = 5.0
_HARDWARE_REPROBE_SECONDS = 300.0
_STATUS_STALE_SECONDS = 30.0
_MIN_PLATFORM_HARDWARE_WORKERS = 2
_PUBLIC_STATUS_FIELDS = frozenset(
    {
        "state",
        "accessScope",
        "connected",
        "hardwareReady",
        "route",
        "backend",
        "gpuState",
        "gpuFallbackReason",
        "cpuWorkerCount",
        "handoffState",
        "processing",
        "apiProcessing",
        "apiCompleted",
        "apiFailed",
        "apiRemaining",
        "apiReady",
        "apiLeaseSlots",
        "providerProfiles",
        "completed",
        "failed",
        "lastErrorCode",
        "version",
        "protocolVersion",
        "pid",
        "updatedAt",
    }
)


class PlatformNodeError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PlatformCredential:
    server: str
    node_id: str
    token: str
    access_scope: str = ACCESS_SCOPE

    @classmethod
    def from_payload(cls, payload: object) -> PlatformCredential:
        if not isinstance(payload, dict) or set(payload) != {
            "server",
            "nodeId",
            "token",
            "accessScope",
        }:
            raise PlatformNodeError("platform_credential_invalid")
        if payload.get("accessScope") != ACCESS_SCOPE:
            raise PlatformNodeError("platform_scope_required")
        server = payload.get("server")
        parsed = urlparse(server.strip()) if isinstance(server, str) else None
        if (
            parsed is None
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise PlatformNodeError("platform_server_origin_invalid")
        try:
            config = HandoffConfig.from_payload(
                {
                    "server": server,
                    "nodeId": payload.get("nodeId"),
                    "token": payload.get("token"),
                    "pollSeconds": 5.0,
                }
            )
        except ValueError as exc:
            raise PlatformNodeError("platform_credential_invalid") from exc
        return cls(
            server=config.server,
            node_id=config.node_id,
            token=config.token,
        )

    def handoff_config(self) -> HandoffConfig:
        return HandoffConfig(
            server=self.server,
            node_id=self.node_id,
            token=self.token,
            poll_seconds=5.0,
        )

    def encoded(self) -> bytearray:
        value = bytearray(
            json.dumps(
                {
                    "server": self.server,
                    "nodeId": self.node_id,
                    "token": self.token,
                    "accessScope": self.access_scope,
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if not value or len(value) > _MAX_CREDENTIAL_BYTES:
            value[:] = b"\0" * len(value)
            raise PlatformNodeError("platform_credential_invalid")
        return value


@dataclass(frozen=True)
class LocalProviderProfile:
    """A provider profile loaded only from the local credential store."""

    name: str
    profile: ProviderProfile


def _provider_target(name: str) -> str:
    if name not in PROVIDER_PROFILE_NAMES:
        raise PlatformNodeError("provider_profile_name_invalid")
    return PROVIDER_CREDENTIAL_TARGET_PREFIX + name


def _provider_payload(profile_name: str, profile: ProviderProfile) -> bytearray:
    if profile_name not in PROVIDER_PROFILE_NAMES or not isinstance(profile, ProviderProfile):
        raise PlatformNodeError("provider_profile_invalid")
    value = bytearray(
        json.dumps(
            {
                "baseUrl": profile.base_url,
                "model": profile.model,
                "apiKey": profile.api_key,
                "requestTimeoutSeconds": profile.request_timeout_s,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if not value or len(value) > _MAX_PROVIDER_CREDENTIAL_BYTES:
        value[:] = b"\0" * len(value)
        raise PlatformNodeError("provider_profile_invalid")
    return value


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _credential_api():
    if os.name != "nt":
        raise PlatformNodeError("windows_credential_manager_required")
    api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    api.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
    ]
    api.CredReadW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    return api


def write_provider_profile(profile_name: str, profile: ProviderProfile) -> None:
    """Store one provider key in its own Windows Credential Manager target."""

    api = _credential_api()
    encoded = _provider_payload(profile_name, profile)
    buffer = (ctypes.c_ubyte * len(encoded)).from_buffer(encoded)
    credential = _CREDENTIALW(
        Flags=0,
        Type=1,
        TargetName=_provider_target(profile_name),
        Comment=None,
        LastWritten=_FILETIME(),
        CredentialBlobSize=len(encoded),
        CredentialBlob=ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        Persist=2,
        AttributeCount=0,
        Attributes=None,
        TargetAlias=None,
        UserName=f"provider_{profile_name}",
    )
    try:
        if not api.CredWriteW(ctypes.byref(credential), 0):
            raise PlatformNodeError("provider_profile_store_failed")
    finally:
        ctypes.memset(buffer, 0, len(encoded))
        encoded[:] = b"\0" * len(encoded)


def read_provider_profile(profile_name: str) -> ProviderProfile | None:
    api = _credential_api()
    raw = ctypes.POINTER(_CREDENTIALW)()
    try:
        target = _provider_target(profile_name)
    except PlatformNodeError:
        raise
    if not api.CredReadW(target, 1, 0, ctypes.byref(raw)):
        if ctypes.get_last_error() == 1168:
            return None
        raise PlatformNodeError("provider_profile_read_failed")
    try:
        if not raw:
            raise PlatformNodeError("provider_profile_invalid")
        size = int(raw.contents.CredentialBlobSize)
        blob = raw.contents.CredentialBlob
        if size <= 0 or size > _MAX_PROVIDER_CREDENTIAL_BYTES or not blob:
            raise PlatformNodeError("provider_profile_invalid")
        try:
            payload = json.loads(ctypes.string_at(blob, size).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PlatformNodeError("provider_profile_invalid") from exc
        if not isinstance(payload, dict) or set(payload) != {
            "baseUrl", "model", "apiKey", "requestTimeoutSeconds"
        }:
            raise PlatformNodeError("provider_profile_invalid")
        try:
            return ProviderProfile(
                base_url=payload["baseUrl"],
                model=payload["model"],
                api_key=payload["apiKey"],
                request_timeout_s=payload["requestTimeoutSeconds"],
            )
        except ProviderExecutorError as exc:
            raise PlatformNodeError("provider_profile_invalid") from exc
    finally:
        if raw:
            api.CredFree(raw)


def read_provider_profiles() -> dict[str, ProviderProfile]:
    profiles: dict[str, ProviderProfile] = {}
    for name in PROVIDER_PROFILE_NAMES:
        profile = read_provider_profile(name)
        if profile is not None:
            profiles[name] = profile
    return profiles


def delete_provider_profile(profile_name: str) -> None:
    api = _credential_api()
    target = _provider_target(profile_name)
    if not api.CredDeleteW(target, 1, 0):
        if ctypes.get_last_error() != 1168:
            raise PlatformNodeError("provider_profile_delete_failed")


def configure_provider_profile(
    profile_name: str,
    input_stream: TextIO,
) -> dict[str, Any]:
    """Read provider credentials from stdin; never accept them as CLI args."""

    if profile_name not in PROVIDER_PROFILE_NAMES:
        raise PlatformNodeError("provider_profile_name_invalid")
    raw = input_stream.read(_MAX_PROVIDER_CREDENTIAL_BYTES + 1)
    if raw.startswith("\ufeff"):
        raw = raw[1:]
    if len(raw.encode("utf-8", errors="ignore")) > _MAX_PROVIDER_CREDENTIAL_BYTES:
        raise PlatformNodeError("provider_profile_invalid")
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PlatformNodeError("provider_profile_invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "baseUrl", "model", "apiKey", "requestTimeoutSeconds"
    }:
        raise PlatformNodeError("provider_profile_invalid")
    try:
        profile = ProviderProfile(
            base_url=payload["baseUrl"],
            model=payload["model"],
            api_key=payload["apiKey"],
            request_timeout_s=payload["requestTimeoutSeconds"],
        )
    except ProviderExecutorError as exc:
        raise PlatformNodeError("provider_profile_invalid") from exc
    write_provider_profile(profile_name, profile)
    return {
        "status": "ok",
        "configured": True,
        "providerProfile": profile_name,
        "model": profile.model,
    }


def provider_profile_status() -> dict[str, Any]:
    """Return only configured names/models, never URL or credential material."""

    try:
        profiles = read_provider_profiles()
    except PlatformNodeError as exc:
        return {"status": "error", "errorCode": exc.code}
    return {
        "status": "ok",
        "profiles": [
            {"providerProfile": name, "model": profiles[name].model}
            for name in PROVIDER_PROFILE_NAMES
            if name in profiles
        ],
    }


def write_platform_credential(value: PlatformCredential) -> None:
    api = _credential_api()
    encoded = value.encoded()
    buffer = (ctypes.c_ubyte * len(encoded)).from_buffer(encoded)
    credential = _CREDENTIALW(
        Flags=0,
        Type=1,
        TargetName=CREDENTIAL_TARGET,
        Comment=None,
        LastWritten=_FILETIME(),
        CredentialBlobSize=len(encoded),
        CredentialBlob=ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        Persist=2,
        AttributeCount=0,
        Attributes=None,
        TargetAlias=None,
        UserName=_CREDENTIAL_USERNAME,
    )
    try:
        if not api.CredWriteW(ctypes.byref(credential), 0):
            raise PlatformNodeError("platform_credential_store_failed")
    finally:
        ctypes.memset(buffer, 0, len(encoded))
        encoded[:] = b"\0" * len(encoded)


def read_platform_credential() -> PlatformCredential | None:
    api = _credential_api()
    raw = ctypes.POINTER(_CREDENTIALW)()
    if not api.CredReadW(CREDENTIAL_TARGET, 1, 0, ctypes.byref(raw)):
        if ctypes.get_last_error() == 1168:
            return None
        raise PlatformNodeError("platform_credential_read_failed")
    try:
        if not raw:
            raise PlatformNodeError("platform_credential_invalid")
        size = int(raw.contents.CredentialBlobSize)
        blob = raw.contents.CredentialBlob
        if size <= 0 or size > _MAX_CREDENTIAL_BYTES or not blob:
            raise PlatformNodeError("platform_credential_invalid")
        try:
            payload = json.loads(ctypes.string_at(blob, size).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PlatformNodeError("platform_credential_invalid") from exc
        return PlatformCredential.from_payload(payload)
    finally:
        if raw:
            api.CredFree(raw)


def delete_platform_credential() -> None:
    api = _credential_api()
    if not api.CredDeleteW(CREDENTIAL_TARGET, 1, 0):
        if ctypes.get_last_error() != 1168:
            raise PlatformNodeError("platform_credential_delete_failed")


def configure_platform_credential(input_stream: TextIO) -> dict[str, Any]:
    raw = input_stream.read(_MAX_CREDENTIAL_BYTES + 1)
    if raw.startswith("\ufeff"):
        raw = raw[1:]
    if len(raw.encode("utf-8", errors="ignore")) > _MAX_CREDENTIAL_BYTES:
        raise PlatformNodeError("platform_credential_invalid")
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PlatformNodeError("platform_credential_invalid") from exc
    credential = PlatformCredential.from_payload(payload)
    write_platform_credential(credential)
    return {
        "status": "ok",
        "configured": True,
        "accessScope": ACCESS_SCOPE,
    }


def platform_status_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        raise PlatformNodeError("local_app_data_unavailable")
    return Path(local_app_data) / "MathAgent" / "PlatformNode" / _STATUS_FILE_NAME


def read_platform_status(path: Path | None = None) -> dict[str, Any]:
    status_path = path or platform_status_path()
    try:
        raw = status_path.read_text(encoding="utf-8")
        if len(raw) > 16 * 1024:
            raise ValueError
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or value.get("accessScope") != ACCESS_SCOPE
            or not isinstance(value.get("state"), str)
        ):
            raise ValueError
        public = {
            key: value[key]
            for key in _PUBLIC_STATUS_FIELDS
            if key in value
        }
        if public.get("state") not in {
            "not_running",
            "stopped",
            "fault",
            "credential_missing",
            "credential_error",
            "credential_rejected",
            "registration_unverified",
        }:
            try:
                updated_at = datetime.fromisoformat(str(public["updatedAt"]))
                if updated_at.tzinfo is None:
                    raise ValueError
                age = (datetime.now(timezone.utc) - updated_at).total_seconds()
                if age < -5.0 or age > _STATUS_STALE_SECONDS:
                    public.update(
                        {
                            "state": "stale",
                            "connected": False,
                            "lastErrorCode": "platform_status_stale",
                        }
                    )
            except (KeyError, TypeError, ValueError):
                public.update(
                    {
                        "state": "stale",
                        "connected": False,
                        "lastErrorCode": "platform_status_stale",
                    }
                )
        return {"status": "ok", "platformNode": public}
    except FileNotFoundError:
        return {
            "status": "ok",
            "platformNode": {
                "state": "not_running",
                "accessScope": ACCESS_SCOPE,
            },
        }
    except (OSError, ValueError, json.JSONDecodeError):
        return {
            "status": "error",
            "errorCode": "platform_status_invalid",
        }


def _write_status(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".platform-status-",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _safe_status_code(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    safe = "".join(character for character in text if character.isascii() and (
        character.isalnum() or character == "_"
    ))
    return safe[:64] or "worker_error"


def _runtime_status(local_status: dict[str, Any], snapshot) -> dict[str, Any]:
    gpu_enabled, cpu_available, cpu_workers, local_available = _platform_local_policy(
        local_status
    )
    last_error = _safe_status_code(snapshot.last_error_code)
    provider_profiles: tuple[str, ...] = ()
    api_ready = False
    gpu = local_status.get("gpuAcceleration")
    gpu_enabled = bool(isinstance(gpu, dict) and gpu.get("enabled") is True)
    gpu_state = (
        "ready"
        if gpu_enabled
        else str(gpu.get("state") or "unavailable")
        if isinstance(gpu, dict)
        else "unavailable"
    )
    gpu_fallback_reason = _safe_status_code(
        gpu.get("fallbackReason") if isinstance(gpu, dict) else None
    )
    if not local_available and not api_ready:
        state = "hardware_unavailable"
    elif last_error == "node_auth_failed":
        state = "credential_rejected"
    elif snapshot.connected:
        state = "ready"
    elif last_error:
        state = "degraded"
    else:
        state = "connecting"
    return {
        "state": state,
        "accessScope": ACCESS_SCOPE,
        "connected": bool(snapshot.connected),
        "hardwareReady": local_available,
        "apiReady": api_ready,
        "route": (
            "local_gpu"
            if gpu_enabled
            else "local_cpu" if cpu_available else "unavailable"
        ),
        "backend": (
            "windows_nvidia_gpu"
            if gpu_enabled
            else "windows_cpu_opencv" if cpu_available else "none"
        ),
        "gpuState": gpu_state,
        "gpuFallbackReason": gpu_fallback_reason,
        "cpuWorkerCount": cpu_workers if cpu_available else 0,
        "handoffState": str(snapshot.state),
        "processing": int(snapshot.processing),
        "apiProcessing": int(getattr(snapshot, "api_processing", 0)),
        "apiCompleted": int(getattr(snapshot, "api_completed", 0)),
        "apiFailed": int(getattr(snapshot, "api_failed", 0)),
        "apiRemaining": int(getattr(snapshot, "api_remaining", 0)),
        "apiLeaseSlots": int(getattr(snapshot, "api_lease_slots", 0) or 0),
        "providerProfiles": list(provider_profiles),
        "completed": int(snapshot.completed),
        "failed": int(snapshot.failed),
        "lastErrorCode": last_error,
        "version": __version__,
        "protocolVersion": PROTOCOL_VERSION,
        "pid": os.getpid(),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }


def _platform_local_policy(status: dict[str, Any]) -> tuple[bool, bool, int, bool]:
    gpu_enabled, cpu_available, cpu_workers, local_available = _local_policy(status)
    capacity = status.get("hardwareWorkerCapacity")
    if (
        isinstance(capacity, bool)
        or not isinstance(capacity, int)
        or capacity < _MIN_PLATFORM_HARDWARE_WORKERS
    ):
        return False, False, 0, False
    return gpu_enabled, cpu_available, cpu_workers, local_available


def _write_status_best_effort(
    path: Path,
    payload: dict[str, Any],
    writer: Callable[[Path, dict[str, Any]], None],
) -> None:
    try:
        writer(path, payload)
    except OSError:
        pass


def _install_signal_handlers(stop_event: threading.Event) -> None:
    if threading.current_thread() is not threading.main_thread():
        return

    def stop(_signum, _frame) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)


def verify_platform_scope(credential: PlatformCredential) -> None:
    try:
        response = HttpTransport(credential.handoff_config()).json_request(
            "/api/accelerator/nodes/heartbeat",
            {
                "capabilities": {
                    "backend": "none",
                    "stages": [],
                    "artifact_contracts": [],
                    "platform": "windows",
                    "batch_size": 1,
                    "lease_slots": 0,
                    "cpu_compute_slots": 0,
                    "gpu_contexts": 0,
                },
                "algorithm_version": ALGORITHM_VERSION,
                "free_slots": 0,
                "queue_depth": 0,
                "estimated_finish_ms": None,
                "last_error_code": None,
            },
        )
    except DesktopHandoffError as exc:
        raise PlatformNodeError(exc.code) from exc
    if response.get("access_scope") != ACCESS_SCOPE:
        raise PlatformNodeError("platform_scope_rejected")


def run_platform_node(
    resource_mode: str = "fast",
    *,
    stop_event: threading.Event | None = None,
    status_path: Path | None = None,
    credential_loader: Callable[[], PlatformCredential | None] = read_platform_credential,
    provider_loader: Callable[[], dict[str, ProviderProfile]] = read_provider_profiles,
    supervisor_factory: Callable[[], Any] = CpuSupervisor,
    runner_factory: Callable[..., Any] = DesktopHandoffRunner,
    scope_validator: Callable[[PlatformCredential], None] = verify_platform_scope,
    status_writer: Callable[[Path, dict[str, Any]], None] = _write_status,
    refresh_seconds: float = _DEFAULT_REFRESH_SECONDS,
    max_cycles: int | None = None,
) -> int:
    active_stop = stop_event or threading.Event()
    if stop_event is None:
        _install_signal_handlers(active_stop)
    active_status_path = status_path or platform_status_path()
    _write_status_best_effort(
        active_status_path,
        {
            "state": "starting",
            "accessScope": ACCESS_SCOPE,
            "version": __version__,
            "protocolVersion": PROTOCOL_VERSION,
            "pid": os.getpid(),
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        },
        status_writer,
    )
    try:
        credential = credential_loader()
    except (PlatformNodeError, OSError) as exc:
        code = exc.code if isinstance(exc, PlatformNodeError) else "platform_credential_read_failed"
        _write_status_best_effort(
            active_status_path,
            {
                "state": "credential_error",
                "accessScope": ACCESS_SCOPE,
                "lastErrorCode": code,
                "version": __version__,
                "protocolVersion": PROTOCOL_VERSION,
                "pid": os.getpid(),
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            },
            status_writer,
        )
        return 2
    if credential is None:
        _write_status_best_effort(
            active_status_path,
            {
                "state": "credential_missing",
                "accessScope": ACCESS_SCOPE,
                "lastErrorCode": "platform_credential_missing",
                "version": __version__,
                "protocolVersion": PROTOCOL_VERSION,
                "pid": os.getpid(),
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            },
            status_writer,
        )
        return 2

    try:
        scope_validator(credential)
    except Exception as exc:
        code = exc.code if isinstance(exc, PlatformNodeError) else "platform_scope_check_failed"
        state = (
            "credential_rejected"
            if code in {"node_auth_failed", "platform_scope_rejected"}
            else "registration_unverified"
        )
        _write_status_best_effort(
            active_status_path,
            {
                "state": state,
                "accessScope": ACCESS_SCOPE,
                "lastErrorCode": code,
                "version": __version__,
                "protocolVersion": PROTOCOL_VERSION,
                "pid": os.getpid(),
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            },
            status_writer,
        )
        return 2

    # Provider profiles are retained only for legacy credential cleanup.
    # Platform nodes never load model credentials or execute model calls.
    del provider_loader
    provider_profiles: dict[str, ProviderProfile] = {}

    runner = None
    runtime_fault = False
    try:
        supervisor = supervisor_factory()
        local_status = supervisor.set_resource_mode(resource_mode)
        gpu_enabled, cpu_available, cpu_workers, local_available = _platform_local_policy(
            local_status
        )
        runner_kwargs = {
            "initially_paused": not local_available,
            "gpu_enabled": gpu_enabled,
            "cpu_fallback_allowed": cpu_available,
            "cpu_worker_count": cpu_workers,
        }
        runner = runner_factory(credential.handoff_config(), **runner_kwargs)
        runner.start()
        last_hardware_reprobe = time.monotonic()
        cycles = 0
        while not active_stop.is_set():
            local_status = supervisor.get_status(refresh=True)
            gpu_enabled, cpu_available, cpu_workers, local_available = _platform_local_policy(
                local_status
            )
            # A transient Vulkan/driver failure must not leave an otherwise
            # healthy node on the CPU route until the user restarts it.
            if (
                not gpu_enabled
                and time.monotonic() - last_hardware_reprobe >= _HARDWARE_REPROBE_SECONDS
            ):
                supervisor = supervisor_factory()
                local_status = supervisor.set_resource_mode(resource_mode)
                gpu_enabled, cpu_available, cpu_workers, local_available = _platform_local_policy(
                    local_status
                )
                last_hardware_reprobe = time.monotonic()
            runner.configure_local_policy(
                gpu_enabled=gpu_enabled,
                cpu_fallback_allowed=cpu_available,
                cpu_worker_count=cpu_workers,
            )
            snapshot = runner.snapshot()
            if not local_available and snapshot.state != "paused":
                runner.pause()
            elif local_available and snapshot.state == "paused":
                runner.resume()
            snapshot = runner.snapshot()
            _write_status_best_effort(
                active_status_path,
                _runtime_status(local_status, snapshot),
                status_writer,
            )
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            active_stop.wait(refresh_seconds)
        return 0
    except Exception:
        runtime_fault = True
        _write_status_best_effort(
            active_status_path,
            {
                "state": "fault",
                "accessScope": ACCESS_SCOPE,
                "lastErrorCode": "platform_runtime_failed",
                "version": __version__,
                "protocolVersion": PROTOCOL_VERSION,
                "pid": os.getpid(),
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            },
            status_writer,
        )
        return 1
    finally:
        if runner is not None:
            try:
                runner.stop(timeout=30.0)
            except Exception:
                pass
        if credential is not None and not runtime_fault:
            _write_status_best_effort(
                active_status_path,
                {
                    "state": "stopped",
                    "accessScope": ACCESS_SCOPE,
                    "version": __version__,
                    "protocolVersion": PROTOCOL_VERSION,
                    "pid": os.getpid(),
                    "updatedAt": datetime.now(timezone.utc).isoformat(),
                },
                status_writer,
            )
