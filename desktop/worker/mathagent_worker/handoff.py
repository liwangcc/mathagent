from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import ssl
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import ProxyHandler, Request, build_opener

from .canonicalization import (
    TemplateCanonicalizationError,
    canonicalize_template,
)
from .preprocessing import ALGORITHM_VERSION, process_candidate
from services.accelerator_artifacts import CLIENT_EVIDENCE_SLOT_GEOMETRY_CONTRACT_VERSION
from .template_ocr_evidence import (
    TEMPLATE_OCR_EVIDENCE_CONTRACT_VERSION,
    TEMPLATE_OCR_EVIDENCE_STAGE,
    render_template_ocr_evidence,
)
from .provider_executor import (
    ProviderExecutor,
    ProviderExecutorError,
    ProviderProfile,
)

_MAX_ASSET_BYTES = 25 * 1024 * 1024
_MAX_ARTIFACT_BYTES = 30 * 1024 * 1024
_MAX_RESPONSE_BYTES = 256 * 1024
_POLL_MIN_SECONDS = 0.25
_POLL_MAX_SECONDS = 30.0
_HANDOFF_WORKER_SLOTS = 8
_PROVIDER_WORKER_SLOTS = 8
_CPU_COMPUTE_SLOTS = 3
# The qualified RTX 4060 path was measured at eight concurrent image jobs.
_GPU_COMPUTE_SLOTS = 8
_CPU_WORKER_COUNTS = (0, 2, 3)
_HEARTBEAT_SECONDS = 5.0
# These errors describe a transport interruption rather than a failed job.
# Clear them after a later heartbeat succeeds so a recovered node is not
# advertised as faulty forever. Job/provider errors remain visible.
_RECOVERABLE_CONNECTION_ERRORS = frozenset({
    "server_unreachable",
    "server_connection_error",
    "server_dns_error",
    "server_http_error",
    "server_response_invalid",
    "server_response_too_large",
    "server_timeout",
    "server_tls_certificate_error",
    "server_tls_error",
})
_LEASE_RENEW_SECONDS = 15.0
_LEASE_RENEW_RETRY_SECONDS = 2.0
_LEASE_RENEW_FAILURE_LIMIT = 3
# Receipts are small JSON records; image buffers are already uploaded and freed.
# Keep both queued records and HTTP submissions bounded independently of compute.
_RESULT_PENDING_LIMIT = 32
_RESULT_SUBMIT_SLOTS = 4
_RESULT_RETRY_SECONDS = 0.5
_RESULT_RETRY_MAX_SECONDS = 10.0
_RESULT_SHUTDOWN_DRAIN_SECONDS = 30.0
_RESULT_PAYLOAD_MAX_BYTES = 128 * 1024
_TEMPLATE_ASSET_CACHE_ENTRIES = 32
_CANONICALIZATION_STAGE = "template_canonicalization"
_VISION_MODEL_STAGE = "vision_model_request"
_VISION_MODEL_REQUEST_CONTRACT_VERSION = "vision-model-request-v1"
_VISION_MODEL_RESPONSE_CONTRACT_VERSION = "vision-model-response-v1"
_SUPPORTED_STAGES = (
    "template_registration",
    "student_multi_evidence",
    _CANONICALIZATION_STAGE,
    TEMPLATE_OCR_EVIDENCE_STAGE,
)
_IMAGE_STAGES = _SUPPORTED_STAGES
_ALL_SUPPORTED_STAGES = (*_IMAGE_STAGES, _VISION_MODEL_STAGE)
_MODEL_RESPONSE_MAX_BYTES = 512 * 1024
_PROVIDER_PROFILE_NAMES = frozenset({"primary", "failover", "accelerator", "retry"})
_CLIENT_EVIDENCE_CONTRACT_VERSION = "client-evidence-v1"
_CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION = "client-evidence-multi-v3"
_CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION = "client-evidence-columns-v1"
_CANONICAL_TEMPLATE_CONTRACT_VERSION = "client-canonical-template-v1"
_CANONICAL_COMPUTE_SLOT = threading.BoundedSemaphore(1)
_SAFE_PATH_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")

# The platform node talks directly to the MathAgent server.  A system proxy
# may be unavailable or may change routes while the desktop node is running.
_DIRECT_OPENER = build_opener(ProxyHandler({}))


def urlopen(request: Request, *, timeout: float):
    """Open a platform-node request without consulting system proxy settings."""

    return _DIRECT_OPENER.open(request, timeout=timeout)


class DesktopHandoffError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code[:64]


def _selected_image_compute_slots(
    *,
    gpu_enabled: bool,
    cpu_fallback_allowed: bool,
    cpu_worker_count: int,
) -> int:
    if gpu_enabled:
        return _GPU_COMPUTE_SLOTS
    if cpu_fallback_allowed and cpu_worker_count in _CPU_WORKER_COUNTS[1:]:
        return cpu_worker_count
    return 0


def _classify_connection_error(error: BaseException) -> str:
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "server_tls_certificate_error"
    if isinstance(reason, ssl.SSLError):
        return "server_tls_error"
    if isinstance(reason, socket.gaierror):
        return "server_dns_error"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "server_timeout"
    if isinstance(
        reason,
        (ConnectionAbortedError, ConnectionRefusedError, ConnectionResetError),
    ):
        return "server_connection_error"
    return "server_unreachable"


def _validate_relative_server_path(value: object, *, code: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise DesktopHandoffError(code)
    parsed = urlparse(value)
    if (
        not value.startswith("/api/accelerator/")
        or parsed.scheme
        or parsed.netloc
        or parsed.params
        or parsed.query
        or parsed.fragment
        or "\\" in value
        or "%" in value
        or any(segment in {".", ".."} for segment in parsed.path.split("/"))
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise DesktopHandoffError(code)
    return value


def _validate_path_segment(value: object, *, code: str) -> str:
    normalized = str(value or "")
    if not _SAFE_PATH_SEGMENT.fullmatch(normalized):
        raise DesktopHandoffError(code)
    return normalized


def _validate_job_contract(job: dict[str, Any]) -> None:
    if job.get("stage") not in _ALL_SUPPORTED_STAGES:
        raise DesktopHandoffError("unsupported_accelerator_stage")
    if job.get("algorithm_version") != ALGORITHM_VERSION:
        raise DesktopHandoffError("algorithm_version_mismatch")
    for field in ("source_url", "template_url"):
        if field in job and job.get(field) is not None:
            _validate_relative_server_path(
                job.get(field),
                code="lease_payload_invalid",
            )
    if job.get("stage") != _VISION_MODEL_STAGE:
        return
    request = job.get("model_request")
    required = {
        "contract_version", "request_digest", "provider_profile", "model",
        "prompt", "max_output_tokens", "reasoning_effort", "stream",
        "first_event_timeout_s", "attempt_ttl_s", "sse_idle_timeout_s", "assets",
    }
    optional = {"prompt_first"}
    if (
        not isinstance(request, dict)
        or not required.issubset(request)
        or set(request) - required - optional
        or not isinstance(request.get("prompt_first", False), bool)
    ):
        raise DesktopHandoffError("vision_model_request_invalid")
    if (
        request.get("contract_version") != _VISION_MODEL_REQUEST_CONTRACT_VERSION
        or request.get("provider_profile") not in _PROVIDER_PROFILE_NAMES
        or not isinstance(request.get("model"), str)
        or not request.get("model")
        or request.get("reasoning_effort") != "high"
        or request.get("stream") is not True
    ):
        raise DesktopHandoffError("vision_model_request_invalid")
    assets = request.get("assets")
    if not isinstance(assets, list) or not 1 <= len(assets) <= 3:
        raise DesktopHandoffError("vision_model_request_invalid")
    for position, asset in enumerate(assets, 1):
        if (
            not isinstance(asset, dict)
            or set(asset) != {"kind", "sha256", "byte_size", "media_type", "url"}
            or asset.get("kind") != f"input_{position:02d}"
            or not isinstance(asset.get("url"), str)
            or not asset.get("url")
        ):
            raise DesktopHandoffError("vision_model_request_invalid")
        _validate_relative_server_path(
            asset.get("url"),
            code="vision_model_request_invalid",
        )


def _canonical_candidate(result) -> dict[str, Any]:
    metadata = dict(result.canonicalization)
    try:
        size = result.canonical_path.stat().st_size
        sha256 = str(metadata["canonical_sha256"])
        width = int(metadata["canonical_width"])
        height = int(metadata["canonical_height"])
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise DesktopHandoffError("canonical_artifact_invalid") from exc
    if size <= 0 or size > _MAX_ARTIFACT_BYTES:
        raise DesktopHandoffError("canonical_artifact_invalid")
    return {
        "status": "succeeded",
        "artifact_manifest": {
            "contract_version": _CANONICAL_TEMPLATE_CONTRACT_VERSION,
            "canonical_template": {
                "sha256": sha256,
                "byte_size": size,
                "width": width,
                "height": height,
                "channels": 3,
                "format": "png",
            },
        },
        "canonicalization": metadata,
    }


@dataclass(frozen=True)
class HandoffSnapshot:
    configured: bool = False
    running: bool = False
    connected: bool = False
    state: str = "not_configured"
    backend: str = "none"
    completed: int = 0
    failed: int = 0
    remaining: int = 0
    processing: int = 0
    pending_results: int = 0
    last_error_code: str | None = None
    last_job_status: str | None = None
    cpu_compute_slots: int = _CPU_COMPUTE_SLOTS
    image_compute_slots: int = 0
    api_processing: int = 0
    api_completed: int = 0
    api_failed: int = 0
    api_remaining: int = 0
    api_last_error_code: str | None = None
    api_last_job_status: str | None = None
    api_lease_slots: int = 0
    provider_profiles: tuple[str, ...] = ()

    def as_status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "running": self.running,
            "connected": self.connected,
            "state": self.state,
            "backend": self.backend,
            "completed": self.completed,
            "failed": self.failed,
            "remaining": self.remaining,
            "processing": self.processing,
            "pendingResults": self.pending_results,
            "leaseSlots": _HANDOFF_WORKER_SLOTS,
            "cpuComputeSlots": self.cpu_compute_slots,
            "imageComputeSlots": self.image_compute_slots,
            "gpuContexts": 1,
            "lastErrorCode": self.last_error_code,
            "lastJobStatus": self.last_job_status,
            "apiProcessing": self.api_processing,
            "apiCompleted": self.api_completed,
            "apiFailed": self.api_failed,
            "apiRemaining": self.api_remaining,
            "apiLastErrorCode": self.api_last_error_code,
            "apiLastJobStatus": self.api_last_job_status,
            "apiLeaseSlots": self.api_lease_slots,
            "providerProfiles": list(self.provider_profiles),
        }


@dataclass(frozen=True)
class HandoffConfig:
    server: str
    node_id: str
    token: str
    poll_seconds: float = 5.0

    @classmethod
    def from_payload(cls, payload: object) -> HandoffConfig:
        if not isinstance(payload, dict):
            raise ValueError("handoff_config_invalid")
        if set(payload) - {"server", "nodeId", "token", "pollSeconds"}:
            raise ValueError("handoff_config_invalid")
        server = payload.get("server")
        node_id = payload.get("nodeId")
        token = payload.get("token")
        poll_seconds = payload.get("pollSeconds", 5.0)
        if not all(isinstance(value, str) and value.strip() for value in (server, node_id, token)):
            raise ValueError("handoff_config_invalid")
        parsed = urlparse(server.strip())
        allow_http = os.environ.get("MATHAGENT_ACCELERATOR_ALLOW_HTTP", "") == "1"
        if parsed.scheme not in ({"https", "http"} if allow_http else {"https"}):
            raise ValueError("server_https_required")
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("server_origin_invalid")
        if not 1 <= len(node_id.strip()) <= 64 or not 20 <= len(token.strip()) <= 256:
            raise ValueError("handoff_config_invalid")
        if isinstance(poll_seconds, bool) or not isinstance(poll_seconds, (int, float)):
            raise ValueError("poll_interval_invalid")
        poll_seconds = float(poll_seconds)
        if not _POLL_MIN_SECONDS <= poll_seconds <= _POLL_MAX_SECONDS:
            raise ValueError("poll_interval_invalid")
        return cls(
            server=server.strip().rstrip("/"),
            node_id=node_id.strip(),
            token=token.strip(),
            poll_seconds=poll_seconds,
        )

    @classmethod
    def from_environment(cls) -> HandoffConfig | None:
        server = os.environ.get("MATHAGENT_ACCELERATOR_SERVER", "").strip()
        node_id = os.environ.get("MATHAGENT_ACCELERATOR_NODE_ID", "").strip()
        token = os.environ.get("MATHAGENT_ACCELERATOR_TOKEN", "").strip()
        if not server or not node_id or not token:
            return None
        parsed = urlparse(server)
        allow_http = os.environ.get("MATHAGENT_ACCELERATOR_ALLOW_HTTP", "") == "1"
        if parsed.scheme not in ({"https", "http"} if allow_http else {"https"}):
            raise DesktopHandoffError("server_https_required")
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.params
            or parsed.path not in {"", "/"}
        ):
            raise DesktopHandoffError("server_origin_invalid")
        if (
            not 1 <= len(node_id) <= 64
            or not 20 <= len(token) <= 256
            or any(ord(character) < 33 or ord(character) > 126 for character in (*node_id, *token))
        ):
            raise DesktopHandoffError("handoff_config_invalid")
        poll_seconds = float(os.environ.get("MATHAGENT_ACCELERATOR_POLL_SECONDS", "5"))
        if not _POLL_MIN_SECONDS <= poll_seconds <= _POLL_MAX_SECONDS:
            raise DesktopHandoffError("poll_interval_invalid")
        return cls(server=server.rstrip("/"), node_id=node_id, token=token, poll_seconds=poll_seconds)


class _ComputeGate:
    def __init__(self, limit: int):
        self._limit = limit
        self._active = 0
        self._condition = threading.Condition()

    def configure(self, limit: int) -> None:
        if not 1 <= limit <= _HANDOFF_WORKER_SLOTS:
            raise ValueError("cpu_compute_slots_invalid")
        with self._condition:
            self._limit = limit
            self._condition.notify_all()

    def __enter__(self):
        with self._condition:
            while self._active >= self._limit:
                self._condition.wait()
            self._active += 1
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        with self._condition:
            self._active = max(0, self._active - 1)
            self._condition.notify_all()


class _LeaseGuard:
    """Keep one lease alive while it waits for and uses a compute slot."""

    def __init__(self, transport, job_id: str, lease: str):
        self._transport = transport
        self._job_id = job_id
        self._lease = lease
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._error_code: str | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name=f"mathagent-lease-{self._job_id[:12]}",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            # A renew request has its own bounded HTTP timeout.  Never make a
            # completed OCR job wait indefinitely for a network thread that is
            # already being stopped.
            thread.join(timeout=2.0)
        self._thread = None

    def ensure_live(self) -> None:
        with self._lock:
            error_code = self._error_code
        if error_code:
            raise DesktopHandoffError(error_code)

    def _set_error(self, error_code: str) -> None:
        with self._lock:
            self._error_code = error_code

    def _run(self) -> None:
        delay = _LEASE_RENEW_SECONDS
        failures = 0
        while not self._stop.wait(delay):
            try:
                response = self._transport.json_request(
                    f"/api/accelerator/jobs/{self._job_id}/renew",
                    {"lease_id": self._lease},
                )
                if (
                    not isinstance(response, dict)
                    or str(response.get("status") or "") != "leased"
                ):
                    raise DesktopHandoffError("lease_rejected")
            except DesktopHandoffError as exc:
                failures += 1
                if exc.code in {"lease_rejected", "node_auth_failed"}:
                    self._set_error(exc.code)
                    return
                if failures >= _LEASE_RENEW_FAILURE_LIMIT:
                    self._set_error("lease_renew_failed")
                    return
                delay = _LEASE_RENEW_RETRY_SECONDS
            except Exception:
                failures += 1
                if failures >= _LEASE_RENEW_FAILURE_LIMIT:
                    self._set_error("lease_renew_failed")
                    return
                delay = _LEASE_RENEW_RETRY_SECONDS
            else:
                failures = 0
                delay = _LEASE_RENEW_SECONDS

@dataclass
class _PendingResult:
    job_id: str
    payload_json: str
    backend: str
    lease_guard: _LeaseGuard
    next_attempt_at: float = 0.0
    attempts: int = 0
    submitting: bool = False


class HttpTransport:
    def __init__(self, config: HandoffConfig):
        self._config = config

    def _headers(self, *, json_body: bool = False, lease: str | None = None) -> dict[str, str]:
        headers = {
            "X-Accelerator-Node": self._config.node_id,
            "X-Accelerator-Token": self._config.token,
            "User-Agent": "MathAgent-Windows-Desktop/1",
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        if lease:
            headers["X-Accelerator-Lease"] = lease
        return headers

    def json_request(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        path = _validate_relative_server_path(path, code="server_path_invalid")
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        request = Request(
            urljoin(self._config.server + "/", path.lstrip("/")),
            data=raw,
            headers=self._headers(json_body=True),
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            if exc.code == 401:
                raise DesktopHandoffError("node_auth_failed") from exc
            if exc.code == 409:
                raise DesktopHandoffError("lease_rejected") from exc
            if exc.code == 422:
                raise DesktopHandoffError("candidate_rejected") from exc
            raise DesktopHandoffError("server_http_error") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise DesktopHandoffError(_classify_connection_error(exc)) from exc
        if len(body) > _MAX_RESPONSE_BYTES:
            raise DesktopHandoffError("server_response_too_large")
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DesktopHandoffError("server_response_invalid") from exc
        if not isinstance(value, dict):
            raise DesktopHandoffError("server_response_invalid")
        return value

    def download(self, path: str, lease: str, target: Path, expected_sha: str) -> None:
        path = _validate_relative_server_path(path, code="server_path_invalid")
        request = Request(
            urljoin(self._config.server + "/", path.lstrip("/")),
            headers=self._headers(lease=lease),
            method="GET",
        )
        digest = hashlib.sha256()
        size = 0
        try:
            with urlopen(request, timeout=60) as response, target.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > _MAX_ASSET_BYTES:
                        raise DesktopHandoffError("asset_too_large")
                    digest.update(chunk)
                    output.write(chunk)
        except DesktopHandoffError:
            target.unlink(missing_ok=True)
            raise
        except HTTPError as exc:
            target.unlink(missing_ok=True)
            if exc.code == 401:
                raise DesktopHandoffError("node_auth_failed") from exc
            if exc.code == 409:
                raise DesktopHandoffError("lease_rejected") from exc
            raise DesktopHandoffError("asset_download_failed") from exc
        except (URLError, TimeoutError, OSError) as exc:
            target.unlink(missing_ok=True)
            raise DesktopHandoffError("asset_download_failed") from exc
        if digest.hexdigest() != expected_sha:
            target.unlink(missing_ok=True)
            raise DesktopHandoffError("download_hash_mismatch")

    def upload_artifact(
        self,
        *,
        job_id: str,
        kind: str,
        lease: str,
        source: Path,
        expected: dict[str, Any],
    ) -> None:
        job_id = _validate_path_segment(job_id, code="job_id_invalid")
        kind = _validate_path_segment(kind, code="artifact_kind_invalid")
        try:
            size = source.stat().st_size
            if size <= 0 or size > _MAX_ARTIFACT_BYTES:
                raise DesktopHandoffError("artifact_size_invalid")
            digest_builder = hashlib.sha256()
            with source.open("rb") as artifact:
                for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
                    digest_builder.update(chunk)
        except DesktopHandoffError:
            raise
        except OSError as exc:
            raise DesktopHandoffError("artifact_read_failed") from exc
        digest = digest_builder.hexdigest()
        if size != expected.get("byte_size") or digest != expected.get("sha256"):
            raise DesktopHandoffError("artifact_local_hash_mismatch")
        try:
            with source.open("rb") as artifact:
                request = Request(
                    urljoin(
                        self._config.server + "/",
                        f"/api/accelerator/jobs/{job_id}/artifacts/{kind}".lstrip("/"),
                    ),
                    data=artifact,
                    headers={
                        **self._headers(lease=lease),
                        "Content-Type": "image/png",
                        "Content-Length": str(size),
                    },
                    method="PUT",
                )
                with urlopen(request, timeout=60) as response:
                    body = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            if exc.code == 401:
                raise DesktopHandoffError("node_auth_failed") from exc
            if exc.code == 409:
                raise DesktopHandoffError("lease_rejected") from exc
            if exc.code in {413, 415, 422}:
                raise DesktopHandoffError("artifact_upload_rejected") from exc
            raise DesktopHandoffError("artifact_upload_failed") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise DesktopHandoffError("artifact_upload_failed") from exc
        if len(body) > _MAX_RESPONSE_BYTES:
            raise DesktopHandoffError("server_response_too_large")
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DesktopHandoffError("server_response_invalid") from exc
        if not isinstance(value, dict) or value.get("job_id") != job_id or value.get("kind") != kind:
            raise DesktopHandoffError("artifact_upload_response_invalid")
        for field in ("sha256", "byte_size", "width", "height", "channels", "format"):
            if value.get(field) != expected.get(field):
                raise DesktopHandoffError("artifact_upload_mismatch")


class DesktopHandoffRunner:
    def __init__(
        self,
        config: HandoffConfig,
        transport: HttpTransport | None = None,
        *,
        initially_paused: bool = False,
        gpu_enabled: bool = True,
        cpu_fallback_allowed: bool = True,
        cpu_worker_count: int = _CPU_COMPUTE_SLOTS,
        provider_profiles: Mapping[str, ProviderProfile] | None = None,
        provider_executor: ProviderExecutor | None = None,
        api_slots: int = _PROVIDER_WORKER_SLOTS,
    ):
        if cpu_worker_count not in _CPU_WORKER_COUNTS:
            raise ValueError("cpu_worker_count_invalid")
        if (
            isinstance(api_slots, bool)
            or not isinstance(api_slots, int)
            or not 0 <= api_slots <= 32
        ):
            raise ValueError("api_slots_invalid")
        raw_profiles = dict(provider_profiles or {})
        if any(
            not isinstance(name, str)
            or name not in _PROVIDER_PROFILE_NAMES
            or not isinstance(profile, ProviderProfile)
            for name, profile in raw_profiles.items()
        ):
            raise ValueError("provider_profiles_invalid")
        self._config = config
        self._transport = transport or HttpTransport(config)
        self._stop = threading.Event()
        # ``pause`` is deliberately scoped to image preprocessing. Provider
        # calls remain available while local CPU/GPU preprocessing is paused.
        self._image_pause = threading.Event()
        self._pause = self._image_pause  # compatibility for existing callers
        if initially_paused:
            self._image_pause.set()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._pending_results: dict[str, _PendingResult] = {}
        self._template_cache_lock = threading.Lock()
        self._template_cache_root = tempfile.TemporaryDirectory(
            prefix="mathagent-template-cache-"
        )
        self._template_cache_order: list[str] = []
        self._gpu_enabled = gpu_enabled
        self._cpu_fallback_allowed = cpu_fallback_allowed
        self._cpu_worker_count = cpu_worker_count
        self._provider_profiles: dict[str, ProviderProfile] = raw_profiles
        self._provider_executor = provider_executor or ProviderExecutor()
        self._provider_worker_count = api_slots
        image_compute_slots = _selected_image_compute_slots(
            gpu_enabled=gpu_enabled,
            cpu_fallback_allowed=cpu_fallback_allowed,
            cpu_worker_count=cpu_worker_count,
        )
        compute_slots = max(1, image_compute_slots)
        self._compute_slots = _ComputeGate(compute_slots)
        self._snapshot = HandoffSnapshot(
            configured=True,
            state=(
                "starting"
                if raw_profiles and api_slots
                else "paused" if initially_paused else "starting"
            ),
            cpu_compute_slots=cpu_worker_count,
            image_compute_slots=image_compute_slots,
            api_lease_slots=api_slots if raw_profiles else 0,
            provider_profiles=tuple(sorted(raw_profiles)),
        )

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="mathagent-desktop-handoff",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float | None = None) -> bool:
        self._stop.set()
        thread = self._thread
        if not thread:
            self._set(state="stopped", running=False, connected=False)
            return True
        self._set(state="stopping", running=True)
        thread.join(timeout)
        return not thread.is_alive()

    def pause(self) -> None:
        self._image_pause.set()
        snapshot = self.snapshot()
        self._set(
            state=("processing" if snapshot.api_processing else "paused"),
            running=True,
        )

    def resume(self) -> None:
        self._image_pause.clear()
        snapshot = self.snapshot()
        self._set(
            state="processing" if snapshot.processing else "idle",
            running=True,
        )

    def configure_provider_profiles(
        self,
        provider_profiles: Mapping[str, ProviderProfile] | None,
    ) -> None:
        """Replace local provider profiles without exposing credential values."""

        raw_profiles = dict(provider_profiles or {})
        if any(
            not isinstance(name, str)
            or name not in _PROVIDER_PROFILE_NAMES
            or not isinstance(profile, ProviderProfile)
            for name, profile in raw_profiles.items()
        ):
            raise ValueError("provider_profiles_invalid")
        with self._lock:
            self._provider_profiles = raw_profiles
            self._snapshot = replace(
                self._snapshot,
                api_lease_slots=(self._provider_worker_count if raw_profiles else 0),
                provider_profiles=tuple(sorted(raw_profiles)),
            )

    def configure_local_policy(
        self,
        *,
        gpu_enabled: bool,
        cpu_fallback_allowed: bool,
        cpu_worker_count: int,
    ) -> None:
        if cpu_worker_count not in _CPU_WORKER_COUNTS:
            raise ValueError("cpu_worker_count_invalid")
        image_compute_slots = _selected_image_compute_slots(
            gpu_enabled=gpu_enabled,
            cpu_fallback_allowed=cpu_fallback_allowed,
            cpu_worker_count=cpu_worker_count,
        )
        compute_slots = max(1, image_compute_slots)
        self._compute_slots.configure(compute_slots)
        with self._lock:
            self._gpu_enabled = bool(gpu_enabled)
            self._cpu_fallback_allowed = bool(cpu_fallback_allowed)
            self._cpu_worker_count = cpu_worker_count
            self._snapshot = replace(
                self._snapshot,
                cpu_compute_slots=cpu_worker_count,
                image_compute_slots=image_compute_slots,
            )

    def _materialize_template_asset(
        self,
        job: dict[str, Any],
        lease: str,
        target: Path,
    ) -> None:
        """Download one immutable template per runner and reuse it by SHA-256."""
        template_sha = str(job.get("template_sha256") or "").lower()
        template_url = str(job.get("template_url") or "")
        if len(template_sha) != 64 or any(
            character not in "0123456789abcdef" for character in template_sha
        ):
            raise DesktopHandoffError("template_sha256_invalid")

        cache_root = Path(self._template_cache_root.name)
        cache_path = cache_root / f"{template_sha}.img"
        with self._template_cache_lock:
            if not cache_path.is_file():
                temporary = cache_root / f".{template_sha}.{threading.get_ident()}.part"
                temporary.unlink(missing_ok=True)
                try:
                    self._transport.download(
                        template_url,
                        lease,
                        temporary,
                        template_sha,
                    )
                    temporary.replace(cache_path)
                finally:
                    temporary.unlink(missing_ok=True)
            if template_sha in self._template_cache_order:
                self._template_cache_order.remove(template_sha)
            self._template_cache_order.append(template_sha)
            while len(self._template_cache_order) > _TEMPLATE_ASSET_CACHE_ENTRIES:
                expired_sha = self._template_cache_order.pop(0)
                (cache_root / f"{expired_sha}.img").unlink(missing_ok=True)
            try:
                shutil.copyfile(cache_path, target)
            except OSError as exc:
                cache_path.unlink(missing_ok=True)
                if template_sha in self._template_cache_order:
                    self._template_cache_order.remove(template_sha)
                raise DesktopHandoffError("template_cache_read_failed") from exc

    def snapshot(self) -> HandoffSnapshot:
        with self._lock:
            return self._snapshot

    def _set(self, **changes: Any) -> None:
        with self._lock:
            self._snapshot = replace(self._snapshot, **changes)

    def _begin_job(self, *, api: bool = False) -> None:
        with self._lock:
            snapshot = self._snapshot
            self._snapshot = replace(
                snapshot,
                state="stopping" if self._stop.is_set() else "processing",
                running=True,
                remaining=snapshot.remaining + 1,
                processing=snapshot.processing + 1,
                last_job_status="leased",
                api_remaining=snapshot.api_remaining + (1 if api else 0),
                api_processing=snapshot.api_processing + (1 if api else 0),
                api_last_job_status="leased" if api else snapshot.api_last_job_status,
            )

    def _finish_job(
        self,
        *,
        accepted: bool,
        backend: str,
        error_code: str | None,
        job_status: str,
        api: bool = False,
        pending_result: bool = False,
    ) -> None:
        with self._lock:
            snapshot = self._snapshot
            processing = max(0, snapshot.processing - (0 if pending_result else 1))
            api_processing = max(
                0,
                snapshot.api_processing - (1 if api else 0),
            )
            state = (
                "stopping"
                if self._stop.is_set()
                else "processing"
                if processing
                else "paused" if self._image_pause.is_set() else "idle"
            )
            self._snapshot = replace(
                snapshot,
                state=state,
                running=True,
                backend=snapshot.backend if api else backend,
                completed=snapshot.completed + (1 if accepted else 0),
                failed=snapshot.failed + (0 if accepted else 1),
                remaining=max(0, snapshot.remaining - 1),
                processing=processing,
                pending_results=max(
                    0, snapshot.pending_results - (1 if pending_result else 0),
                ),
                last_error_code=error_code,
                last_job_status=job_status,
                api_processing=api_processing,
                api_completed=snapshot.api_completed + (
                    1 if api and accepted else 0
                ),
                api_failed=snapshot.api_failed + (
                    1 if api and not accepted else 0
                ),
                api_remaining=max(
                    0,
                    snapshot.api_remaining - (1 if api else 0),
                ),
                api_last_error_code=(
                    error_code if api else snapshot.api_last_error_code
                ),
                api_last_job_status=(
                    job_status if api else snapshot.api_last_job_status
                ),
            )

    def _queue_result(
        self,
        job_id: str,
        lease: str,
        candidate: dict[str, Any],
        backend: str,
        lease_guard: _LeaseGuard,
    ) -> None:
        # Serialize once, before relinquishing artifact ownership. A retry must
        # never re-run image processing or accidentally submit a mutated result.
        payload_json = json.dumps(
            {"lease_id": lease, "result": candidate},
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(payload_json.encode("utf-8")) > _RESULT_PAYLOAD_MAX_BYTES:
            raise DesktopHandoffError("result_payload_too_large")
        pending = _PendingResult(job_id, payload_json, backend, lease_guard)
        with self._lock:
            if job_id in self._pending_results:
                raise DesktopHandoffError("duplicate_pending_result")
            if len(self._pending_results) >= _RESULT_PENDING_LIMIT:
                raise DesktopHandoffError("result_queue_full")
            self._pending_results[job_id] = pending
            snapshot = self._snapshot
            self._snapshot = replace(
                snapshot,
                processing=max(0, snapshot.processing - 1),
                pending_results=snapshot.pending_results + 1,
                backend=backend,
                last_job_status="result_pending",
            )
        # Direct single-job callers still receive an immediate first receipt.
        # The running scheduler always uses its independent submission pool.
        if self._thread is None or not self._thread.is_alive():
            self._submit_result(pending)

    def _complete_result(
        self,
        pending: _PendingResult,
        *,
        accepted: bool,
        status: str,
        error_code: str | None,
    ) -> None:
        with self._lock:
            if self._pending_results.get(pending.job_id) is not pending:
                return
            del self._pending_results[pending.job_id]
        self._finish_job(
            accepted=accepted,
            backend=pending.backend,
            error_code=error_code,
            job_status=status,
            pending_result=True,
        )

    def _submit_result(self, pending: _PendingResult) -> None:
        with self._lock:
            if self._pending_results.get(pending.job_id) is not pending:
                return
        pending.attempts += 1
        # Keep queued, not-yet-submitted leases alive. Stop renew before POST:
        # the server changes state atomically and idempotent POST owns all later
        # confirmation retries, including an already consumed accepted result.
        pending.lease_guard.close()
        try:
            response = self._transport.json_request(
                f"/api/accelerator/jobs/{pending.job_id}/result",
                json.loads(pending.payload_json),
            )
            if (
                not isinstance(response, dict)
                or response.get("job_id", pending.job_id) != pending.job_id
            ):
                raise DesktopHandoffError("server_response_invalid")
            status = str(response.get("status") or "")
            validation_status = str(response.get("server_validation_status") or "")
            if (
                status == "superseded"
                and validation_status == "accepted"
                and response.get("job_id") != pending.job_id
            ):
                raise DesktopHandoffError("server_response_invalid")
            # The node's accepted computation is complete even if the business
            # selected another page-type candidate before this retry arrived.
            # /result still checks the original payload, device and lease.
            if status in {"validated", "consumed", "superseded"} and validation_status == "accepted":
                self._complete_result(
                    pending, accepted=True, status=status, error_code=None,
                )
                return
            if validation_status == "rejected" or status in {
                "server_fallback", "superseded", "failed", "cancelled", "expired",
            }:
                self._complete_result(
                    pending,
                    accepted=False,
                    status=status or "rejected",
                    error_code="server_candidate_contract_mismatch",
                )
                return
            if status not in {"candidate_received", "server_validating"}:
                raise DesktopHandoffError("server_response_invalid")
            error_code = None
        except DesktopHandoffError as exc:
            if exc.code in {"lease_rejected", "candidate_rejected"}:
                self._complete_result(
                    pending, accepted=False, status="rejected", error_code=exc.code,
                )
                return
            # Connection loss after POST is ambiguous. In particular, never
            # call /fail: the server may already have accepted the exact result.
            error_code = exc.code
        except Exception:
            error_code = "result_confirmation_error"
        delay = min(
            _RESULT_RETRY_MAX_SECONDS,
            _RESULT_RETRY_SECONDS * (2 ** min(pending.attempts - 1, 10)),
        )
        with self._lock:
            if self._pending_results.get(pending.job_id) is pending:
                pending.next_attempt_at = time.monotonic() + delay
                pending.submitting = False
                self._snapshot = replace(
                    self._snapshot,
                    last_error_code=error_code,
                    last_job_status="result_pending",
                )

    def _dispatch_results(
        self,
        executor: ThreadPoolExecutor,
        futures: set[Future[None]],
        *,
        drain_expired: bool = False,
    ) -> None:
        now = time.monotonic()
        with self._lock:
            due = [
                pending for pending in self._pending_results.values()
                if not pending.submitting
                and pending.next_attempt_at <= now
                and (not drain_expired or pending.attempts == 0)
            ][:_RESULT_SUBMIT_SLOTS - len(futures)]
            for pending in due:
                pending.submitting = True
        for pending in due:
            futures.add(executor.submit(self._submit_result, pending))

    def _heartbeat_payload(self) -> dict[str, Any]:
        snapshot = self.snapshot()
        with self._lock:
            gpu_enabled = self._gpu_enabled
            cpu_enabled = (
                self._cpu_fallback_allowed
                and self._cpu_worker_count in _CPU_WORKER_COUNTS[1:]
            )
            provider_profiles = tuple(sorted(self._provider_profiles))
            provider_models = {
                name: self._provider_profiles[name].model
                for name in provider_profiles
            }
            provider_worker_count = self._provider_worker_count
        image_enabled = gpu_enabled or cpu_enabled
        image_advertised = image_enabled and not self._image_pause.is_set()
        api_enabled = bool(provider_profiles) and provider_worker_count > 0
        if gpu_enabled and cpu_enabled:
            backend = "windows_nvidia_gpu_or_cpu"
        elif gpu_enabled:
            backend = "windows_nvidia_gpu"
        elif cpu_enabled:
            backend = "windows_cpu_opencv"
        else:
            backend = "none"
        stages: list[str] = []
        artifact_contracts: list[str] = []
        stage_slots: dict[str, dict[str, Any]] = {}
        if image_advertised:
            stages.extend(_IMAGE_STAGES)
            artifact_contracts.extend([
                _CLIENT_EVIDENCE_CONTRACT_VERSION,
                _CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION,
                _CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION,
                CLIENT_EVIDENCE_SLOT_GEOMETRY_CONTRACT_VERSION,
                _CANONICAL_TEMPLATE_CONTRACT_VERSION,
                TEMPLATE_OCR_EVIDENCE_CONTRACT_VERSION,
            ])
            stage_slots["image"] = {
                "stages": list(_IMAGE_STAGES),
                "capacity": _HANDOFF_WORKER_SLOTS,
            }
        if api_enabled:
            stages.append(_VISION_MODEL_STAGE)
            artifact_contracts.append(_VISION_MODEL_RESPONSE_CONTRACT_VERSION)
            stage_slots["provider_api"] = {
                "stages": [_VISION_MODEL_STAGE],
                "capacity": provider_worker_count,
            }
        image_processing = max(0, snapshot.processing - snapshot.api_processing)
        image_compute_slots = (
            snapshot.image_compute_slots if image_enabled else 0
        )
        image_free = (
            max(0, min(
                _HANDOFF_WORKER_SLOTS - image_processing,
                _RESULT_PENDING_LIMIT - snapshot.pending_results - image_processing,
            ))
            if image_advertised
            else 0
        )
        api_free = (
            max(0, provider_worker_count - snapshot.api_processing)
            if api_enabled
            else 0
        )
        lease_slots = (
            (_HANDOFF_WORKER_SLOTS if image_enabled else 0)
            + (provider_worker_count if api_enabled else 0)
        )
        capabilities: dict[str, Any] = {
            "backend": backend,
            # The server treats this as a hard admission gate.  It describes
            # physical preprocessing readiness, while ``stages`` and
            # ``free_slots`` describe whether work can be accepted right now.
            "hardware_ready": bool(image_enabled),
            "stages": stages,
            "artifact_contracts": artifact_contracts,
            "provider_profiles": list(provider_profiles) if api_enabled else [],
            "platform": "windows",
            "batch_size": 1,
            "lease_slots": lease_slots,
            "cpu_compute_slots": (
                snapshot.cpu_compute_slots if cpu_enabled else 0
            ),
            "image_compute_slots": image_compute_slots,
            "gpu_contexts": 1 if gpu_enabled else 0,
            "image_free_slots": image_free,
            "provider_api_free_slots": api_free,
        }
        if api_enabled:
            # Model names are public routing metadata; credentials and
            # endpoints remain exclusively in the local provider profiles.
            capabilities["provider_models"] = provider_models
        if stage_slots:
            capabilities["stage_slots"] = stage_slots
        return {
            "capabilities": capabilities,
            "algorithm_version": ALGORITHM_VERSION,
            "free_slots": image_free + api_free,
            "queue_depth": snapshot.processing,
            "estimated_finish_ms": None,
            "last_error_code": snapshot.last_error_code,
        }

    def _run(self) -> None:
        with self._lock:
            has_api = bool(self._provider_profiles) and self._provider_worker_count > 0
        self._set(
            running=True,
            state="connecting" if not self._image_pause.is_set() or has_api else "paused",
        )
        image_futures: set[Future[None]] = set()
        api_futures: set[Future[None]] = set()
        result_futures: set[Future[None]] = set()
        next_heartbeat = 0.0
        with ThreadPoolExecutor(
            max_workers=_HANDOFF_WORKER_SLOTS,
            thread_name_prefix="mathagent-handoff",
        ) as image_executor, ThreadPoolExecutor(
            max_workers=max(1, self._provider_worker_count),
            thread_name_prefix="mathagent-provider",
        ) as api_executor, ThreadPoolExecutor(
            max_workers=_RESULT_SUBMIT_SLOTS,
            thread_name_prefix="mathagent-result",
        ) as result_executor:
            while not self._stop.is_set():
                for futures in (image_futures, api_futures, result_futures):
                    for future in tuple(futures):
                        if not future.done():
                            continue
                        futures.remove(future)
                        try:
                            future.result()
                        except Exception:
                            self._set(last_error_code="worker_internal_error")

                self._dispatch_results(result_executor, result_futures)

                now = time.monotonic()
                try:
                    if now >= next_heartbeat:
                        self._transport.json_request(
                            "/api/accelerator/nodes/heartbeat",
                            self._heartbeat_payload(),
                        )
                        snapshot = self.snapshot()
                        self._set(
                            connected=True,
                            running=True,
                            last_error_code=(
                                None
                                if snapshot.last_error_code
                                in _RECOVERABLE_CONNECTION_ERRORS
                                else snapshot.last_error_code
                            ),
                            state=(
                                "processing"
                                if snapshot.processing
                                else (
                                    "idle"
                                    if self._provider_profiles
                                    else "paused"
                                    if self._image_pause.is_set()
                                    else "idle"
                                )
                            ),
                        )
                        next_heartbeat = now + _HEARTBEAT_SECONDS

                    claimed = False
                    with self._lock:
                        processing_enabled = self._gpu_enabled or (
                            self._cpu_fallback_allowed
                            and self._cpu_worker_count in _CPU_WORKER_COUNTS[1:]
                        )
                        provider_enabled = bool(self._provider_profiles) and (
                            self._provider_worker_count > 0
                        )
                    if not self._image_pause.is_set() and processing_enabled:
                        while len(image_futures) < _HANDOFF_WORKER_SLOTS:
                            if self._image_pause.is_set() or self._stop.is_set():
                                break
                            if (
                                len(image_futures) + self.snapshot().pending_results
                                >= _RESULT_PENDING_LIMIT
                            ):
                                break
                            leased = self._transport.json_request(
                                "/api/accelerator/jobs/lease",
                                {"stages": list(_IMAGE_STAGES)},
                            )
                            job = leased.get("job")
                            if not job:
                                break
                            if not isinstance(job, dict):
                                raise DesktopHandoffError("lease_payload_invalid")
                            if job.get("stage") == _VISION_MODEL_STAGE:
                                raise DesktopHandoffError("lease_stage_mismatch")
                            image_futures.add(image_executor.submit(self._process_job, job))
                            claimed = True
                            if self._image_pause.is_set() or self._stop.is_set():
                                # A claim already in flight when pause was set
                                # must be drained safely, but pause forbids
                                # filling any additional lease slots.
                                break

                    # Provider calls have their own pool and are intentionally
                    # independent from local image-processing availability.
                    if provider_enabled:
                        while len(api_futures) < self._provider_worker_count:
                            if self._stop.is_set():
                                break
                            leased = self._transport.json_request(
                                "/api/accelerator/jobs/lease",
                                {"stages": [_VISION_MODEL_STAGE]},
                            )
                            job = leased.get("job")
                            if not job:
                                break
                            if not isinstance(job, dict):
                                raise DesktopHandoffError("lease_payload_invalid")
                            if job.get("stage") != _VISION_MODEL_STAGE:
                                raise DesktopHandoffError("lease_stage_mismatch")
                            api_futures.add(
                                api_executor.submit(self._process_model_job, job)
                            )
                            claimed = True

                    delay = (
                        0.1
                        if image_futures or api_futures or result_futures
                        or self.snapshot().pending_results or claimed or self._image_pause.is_set()
                        else self._config.poll_seconds
                    )
                    self._stop.wait(delay)
                except DesktopHandoffError as exc:
                    self._set(
                        connected=False,
                        running=True,
                        state="error" if not self._image_pause.is_set() else "paused",
                        last_error_code=exc.code,
                    )
                    next_heartbeat = 0.0
                    self._stop.wait(self._config.poll_seconds)
                except Exception:
                    self._set(
                        connected=False,
                        running=True,
                        state="error" if not self._image_pause.is_set() else "paused",
                        last_error_code="worker_internal_error",
                    )
                    next_heartbeat = 0.0
                    self._stop.wait(self._config.poll_seconds)
            # Pause drains normally; stop stops leasing, lets claimed images
            # finish/upload, and gives receipts a finite retry window. An
            # in-flight HTTP request has the existing transport timeout. Never
            # turn unresolved receipts into success/failure just to exit.
            drain_deadline = time.monotonic() + _RESULT_SHUTDOWN_DRAIN_SECONDS
            while True:
                for futures in (image_futures, api_futures, result_futures):
                    for future in tuple(futures):
                        if future.done():
                            futures.remove(future)
                            try:
                                future.result()
                            except Exception:
                                self._set(last_error_code="worker_internal_error")
                expired = time.monotonic() >= drain_deadline
                self._dispatch_results(
                    result_executor, result_futures, drain_expired=expired,
                )
                with self._lock:
                    pending_unattempted = any(
                        pending.attempts == 0
                        for pending in self._pending_results.values()
                    )
                    pending_count = len(self._pending_results)
                if (
                    not image_futures and not api_futures and not result_futures
                    and not pending_unattempted and (not pending_count or expired)
                ):
                    break
                time.sleep(0.05)
        self._set(
            running=False,
            connected=False,
            state="stopped",
            remaining=self.snapshot().pending_results,
            processing=0,
            api_remaining=0,
            api_processing=0,
        )

    def _process_model_job(self, job: dict[str, Any]) -> None:
        """Execute one provider request without sharing image-processing slots."""

        job_id = str(job.get("job_id") or "")
        lease = str(job.get("lease_id") or "")
        if (
            not _SAFE_PATH_SEGMENT.fullmatch(job_id)
            or not 20 <= len(lease) <= 256
            or any(ord(character) < 33 or ord(character) > 126 for character in lease)
            or job.get("stage") != _VISION_MODEL_STAGE
        ):
            raise DesktopHandoffError("lease_payload_invalid")
        self._begin_job(api=True)
        failure_code: str | None = None
        lease_guard = _LeaseGuard(self._transport, job_id, lease)
        try:
            _validate_job_contract(job)
            request = dict(job["model_request"])
            profile_name = str(request["provider_profile"])
            with self._lock:
                profile = self._provider_profiles.get(profile_name)
            if profile is None:
                raise DesktopHandoffError("provider_profile_unavailable")

            # URLs are lease-bound server routes. They are used only for the
            # immediate download and are never passed to the provider executor.
            provider_assets = []
            for asset in request["assets"]:
                provider_assets.append({
                    key: value for key, value in asset.items() if key != "url"
                })
            request["assets"] = provider_assets

            lease_guard.start()
            with tempfile.TemporaryDirectory(prefix="mathagent-provider-job-") as directory:
                root = Path(directory)
                paths: list[Path] = []
                for asset in job["model_request"]["assets"]:
                    target = root / f"{asset['kind']}.bin"
                    self._transport.download(
                        str(asset["url"]),
                        lease,
                        target,
                        str(asset["sha256"]),
                    )
                    paths.append(target)
                    lease_guard.ensure_live()

                result = self._provider_executor.execute(request, paths, profile)
                lease_guard.ensure_live()
                output = result.get("content") if isinstance(result, dict) else None
                if not isinstance(output, str) or not output.strip():
                    raise ProviderExecutorError("provider_content_empty")
                response_payload = {
                    "contract_version": _VISION_MODEL_RESPONSE_CONTRACT_VERSION,
                    "request_digest": request["request_digest"],
                    "status": "completed",
                    "output_text": output,
                }
                lease_guard.close()
                response = self._transport.json_request(
                    f"/api/accelerator/jobs/{job_id}/result",
                    {"lease_id": lease, "result": response_payload},
                )
                status = str(response.get("status") or "")
                validation_status = str(response.get("server_validation_status") or "")
                accepted = status in {"validated", "consumed"} and (
                    validation_status in {"accepted", ""}
                )
                self._finish_job(
                    accepted=accepted,
                    backend="provider_api",
                    error_code=None if accepted else "server_candidate_contract_mismatch",
                    job_status=status or "candidate_received",
                    api=True,
                )
                return
        except ProviderExecutorError as exc:
            failure_code = exc.code
        except DesktopHandoffError as exc:
            failure_code = exc.code
        except RuntimeError as exc:
            failure_code = str(exc)[:64] or "provider_processing_failed"
        except Exception as exc:
            exception_name = type(exc).__name__.lower()
            safe_name = "".join(
                character for character in exception_name
                if character.isascii() and character.isalnum()
            )
            failure_code = f"provider_{safe_name or 'failed'}"[:64]
        finally:
            lease_guard.close()

        try:
            self._transport.json_request(
                f"/api/accelerator/jobs/{job_id}/fail",
                {"lease_id": lease, "error_code": failure_code or "provider_failed"},
            )
        except DesktopHandoffError:
            pass
        self._finish_job(
            accepted=False,
            backend="provider_api",
            error_code=failure_code or "provider_failed",
            job_status="failed",
            api=True,
        )

    def _process_job(self, job: dict[str, Any]) -> None:
        job_id = str(job.get("job_id") or "")
        lease = str(job.get("lease_id") or "")
        source_url = str(job.get("source_url") or "")
        template_url = str(job.get("template_url") or "")
        stage = str(job.get("stage") or "")
        if stage == _VISION_MODEL_STAGE:
            # A defensive dispatch guard for callers that invoke this method
            # directly; the normal runner routes this stage to its API pool.
            return self._process_model_job(job)
        if (
            not _SAFE_PATH_SEGMENT.fullmatch(job_id)
            or not 20 <= len(lease) <= 256
            or any(ord(character) < 33 or ord(character) > 126 for character in lease)
            or not source_url
            or (
                stage in {"template_registration", "student_multi_evidence"}
                and not template_url
            )
        ):
            raise DesktopHandoffError("lease_payload_invalid")
        self._begin_job()
        failure_code: str | None = None
        backend = "none"
        lease_guard = _LeaseGuard(self._transport, job_id, lease)
        receipt_owns_lease = False
        try:
            _validate_job_contract(job)
            lease_guard.start()
            with tempfile.TemporaryDirectory(prefix="mathagent-desktop-job-") as directory:
                root = Path(directory)
                source = root / "source.png"
                self._transport.download(
                    source_url,
                    lease,
                    source,
                    str(job.get("source_sha256") or ""),
                )
                lease_guard.ensure_live()
                if stage == _CANONICALIZATION_STAGE:
                    canonical = root / "canonical.png"
                    with self._compute_slots:
                        with _CANONICAL_COMPUTE_SLOT:
                            canonical_result = canonicalize_template(
                                root,
                                source,
                                canonical,
                            )
                    lease_guard.ensure_live()
                    backend = "windows_cpu_opencv"
                    candidate = _canonical_candidate(canonical_result)
                    manifest = candidate["artifact_manifest"]
                    uploads = (("canonical_template", canonical),)
                elif stage == TEMPLATE_OCR_EVIDENCE_STAGE:
                    artifact_path = root / "template-ocr-evidence.png"
                    with self._compute_slots:
                        evidence_result = render_template_ocr_evidence(
                            source,
                            job,
                            artifact_path,
                        )
                    lease_guard.ensure_live()
                    backend = "windows_cpu_pillow"
                    candidate = evidence_result.candidate
                    manifest = candidate["artifact_manifest"]
                    uploads = (
                        (
                            "template_ocr_evidence",
                            evidence_result.artifact_path,
                        ),
                    )
                else:
                    template = root / "template.img"
                    self._materialize_template_asset(job, lease, template)
                    lease_guard.ensure_live()
                    with self._compute_slots:
                        with self._lock:
                            allow_gpu = self._gpu_enabled
                            allow_cpu_fallback = (
                                self._cpu_fallback_allowed
                                and self._cpu_worker_count in _CPU_WORKER_COUNTS[1:]
                            )
                        outcome = process_candidate(
                            source,
                            template,
                            job,
                            artifact_output_dir=root / "artifacts",
                            allow_gpu=allow_gpu,
                            allow_cpu_fallback=allow_cpu_fallback,
                        )
                    lease_guard.ensure_live()
                    backend = outcome.backend
                    candidate = outcome.candidate
                    manifest = candidate["artifact_manifest"]
                    if (
                        manifest.get("contract_version")
                        in {
                            _CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION,
                            _CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION,
                        }
                    ):
                        if not outcome.evidence_paths:
                            raise RuntimeError("evidence_artifact_missing")
                        uploads = tuple(
                            (entry["kind"], path)
                            for entry, path in zip(
                                manifest["evidence_images"],
                                outcome.evidence_paths,
                            )
                        )
                    elif (
                        manifest.get("contract_version")
                        == _CLIENT_EVIDENCE_CONTRACT_VERSION
                    ):
                        if outcome.evidence_path is None:
                            raise RuntimeError("evidence_artifact_missing")
                        uploads = (("evidence", outcome.evidence_path),)
                    else:
                        raise RuntimeError("artifact_contract_unsupported")
                for kind, artifact_path in uploads:
                    self._transport.upload_artifact(
                        job_id=job_id,
                        kind=kind,
                        lease=lease,
                        source=artifact_path,
                        expected=(
                            next(
                                item
                                for item in manifest["evidence_images"]
                                if item["kind"] == kind
                            )
                            if "evidence_images" in manifest
                            else manifest[kind]
                        ),
                    )
                    lease_guard.ensure_live()
                self._queue_result(
                    job_id, lease, candidate, backend, lease_guard,
                )
                receipt_owns_lease = True
                return
        except DesktopHandoffError as exc:
            failure_code = exc.code
        except TemplateCanonicalizationError as exc:
            failure_code = exc.code[:64]
        except RuntimeError as exc:
            failure_code = str(exc)[:64] or "processing_failed"
        except Exception as exc:
            exception_name = type(exc).__name__.lower()
            safe_name = "".join(
                character
                for character in exception_name
                if character.isascii() and character.isalnum()
            )
            failure_code = f"processing_{safe_name or 'failed'}"[:64]
        finally:
            if not receipt_owns_lease:
                lease_guard.close()

        try:
            self._transport.json_request(
                f"/api/accelerator/jobs/{job_id}/fail",
                {"lease_id": lease, "error_code": failure_code or "processing_failed"},
            )
        except DesktopHandoffError:
            pass
        self._finish_job(
            accepted=False,
            backend=backend,
            error_code=failure_code or "processing_failed",
            job_status="failed",
        )


def start_from_environment(
    *,
    initially_paused: bool = False,
    gpu_enabled: bool = True,
    cpu_fallback_allowed: bool = True,
    cpu_worker_count: int = _CPU_COMPUTE_SLOTS,
) -> DesktopHandoffRunner | None:
    config = HandoffConfig.from_environment()
    if config is None:
        return None
    runner = DesktopHandoffRunner(
        config,
        initially_paused=initially_paused,
        gpu_enabled=gpu_enabled,
        cpu_fallback_allowed=cpu_fallback_allowed,
        cpu_worker_count=cpu_worker_count,
    )
    runner.start()
    return runner
