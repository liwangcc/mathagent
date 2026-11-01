from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import socket
import ssl
import threading
import time
from urllib.error import URLError
from urllib.request import ProxyHandler

import pytest

import mathagent_worker.handoff as handoff
from mathagent_worker.handoff import (
    DesktopHandoffError,
    DesktopHandoffRunner,
    HandoffConfig,
    HandoffSnapshot,
    HttpTransport,
)
from mathagent_worker.preprocessing import ProcessingOutcome
from mathagent_worker.template_ocr_evidence import TemplateOcrEvidenceResult
from mathagent_worker.provider_executor import ProviderExecutorError, ProviderProfile


def _config() -> HandoffConfig:
    return HandoffConfig(
        server="https://example.invalid",
        node_id="node-1",
        token="secret-token",
        poll_seconds=0.25,
    )


def test_platform_http_transport_uses_an_empty_proxy_handler() -> None:
    proxy_handlers = [
        handler
        for handler in handoff._DIRECT_OPENER.handlers
        if isinstance(handler, ProxyHandler)
    ]

    assert proxy_handlers == []


class FakeTransport:
    def __init__(self):
        self.requests = []
        self.downloads = []
        self.uploads = []

    def json_request(self, path, payload):
        self.requests.append((path, payload))
        if path.endswith("/result"):
            return {"status": "validated", "server_validation_status": "accepted"}
        if path.endswith("/fail"):
            return {"status": "queued"}
        return {}

    def download(self, path, lease, target, expected_sha):
        self.downloads.append((path, lease, expected_sha))
        target.write_bytes(b"image")

    def upload_artifact(self, **kwargs):
        self.uploads.append(kwargs)


def _job() -> dict:
    return {
        "job_id": "job-1",
        "lease_id": "l" * 32,
        "source_url": "/api/accelerator/jobs/job-1/assets/source",
        "template_url": "/api/accelerator/jobs/job-1/assets/template",
        "source_sha256": "a" * 64,
        "template_sha256": "b" * 64,
        "layout_snapshot_sha256": "c" * 64,
        "stage": "template_registration",
        "algorithm_version": "template-registration-v2",
        "layout_snapshot": {"page_type_id": "page-1", "answer_regions": []},
    }


def _canonical_job() -> dict:
    job = _job()
    job.pop("template_url")
    job["stage"] = "template_canonicalization"
    job["layout_snapshot"] = {"operation_id": "canonical-op"}
    return job


def _template_ocr_evidence_job() -> dict:
    job = _job()
    job["stage"] = "template_ocr_evidence"
    return job


def _model_job() -> dict:
    return {
        "job_id": "model-job-1",
        "lease_id": "m" * 32,
        "stage": "vision_model_request",
        "algorithm_version": "template-registration-v2",
        "model_request": {
            "contract_version": "vision-model-request-v1",
            "request_digest": "d" * 64,
            "provider_profile": "primary",
            "model": "gpt-5.6-sol",
            "prompt": "只按图片转录并判分。",
            "max_output_tokens": 32_768,
            "reasoning_effort": "high",
            "stream": True,
            "first_event_timeout_s": 60.0,
            "attempt_ttl_s": 180.0,
            "sse_idle_timeout_s": 120.0,
            "assets": [{
                "kind": "input_01",
                "sha256": "a" * 64,
                "byte_size": 5,
                "media_type": "image/png",
                "url": "/api/accelerator/jobs/model-job-1/assets/input_01",
            }],
        },
    }


def _provider_profile() -> ProviderProfile:
    return ProviderProfile(
        base_url="https://provider.example/v1",
        model="gpt-5.6-sol",
        api_key="local-provider-secret-key",
    )


def _outcome() -> ProcessingOutcome:
    return ProcessingOutcome(
        candidate={
            "status": "succeeded",
            "page_type_id": "page-1",
            "homography": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
            "good_matches": 10,
            "inliers": 9,
            "inlier_ratio": 0.9,
            "reprojection_rmse": 0.1,
            "coverage": 1.0,
            "artifact_sha256": "d" * 64,
            "artifact_manifest": {
                "contract_version": "client-evidence-v1",
                "evidence": {
                    "sha256": "e" * 64,
                    "byte_size": 100,
                    "width": 900,
                    "height": 160,
                    "channels": 3,
                    "format": "png",
                },
            },
        },
        backend="windows_nvidia_gpu",
        fallback_reason=None,
        elapsed_ms=1.0,
        aligned_page_path=Path("aligned-page.png"),
        source_mask_path=Path("source-mask.png"),
        evidence_path=Path("evidence.png"),
    )


def _client_evidence_outcome() -> ProcessingOutcome:
    return _outcome()


def test_environment_configuration_requires_https(monkeypatch) -> None:
    from mathagent_worker.handoff import HandoffConfig

    monkeypatch.setenv("MATHAGENT_ACCELERATOR_SERVER", "http://example.test")
    monkeypatch.setenv("MATHAGENT_ACCELERATOR_NODE_ID", "node")
    monkeypatch.setenv("MATHAGENT_ACCELERATOR_TOKEN", "token")

    with pytest.raises(DesktopHandoffError, match="server_https_required"):
        HandoffConfig.from_environment()


def test_process_job_submits_candidate_and_advances_progress(monkeypatch) -> None:
    transport = FakeTransport()
    runner = DesktopHandoffRunner(_config(), transport)
    monkeypatch.setattr(
        "mathagent_worker.handoff.process_candidate",
        lambda *_args, **_kwargs: _outcome(),
    )

    runner._process_job(_job())

    snapshot = runner.snapshot()
    assert snapshot.completed == 1
    assert snapshot.failed == 0
    assert snapshot.remaining == 0
    assert snapshot.processing == 0
    assert snapshot.backend == "windows_nvidia_gpu"
    assert snapshot.last_job_status == "validated"
    assert [item[0] for item in transport.downloads] == [
        "/api/accelerator/jobs/job-1/assets/source",
        "/api/accelerator/jobs/job-1/assets/template",
    ]
    assert [item["kind"] for item in transport.uploads] == ["evidence"]
    assert transport.requests[-1][0] == "/api/accelerator/jobs/job-1/result"
    assert "lease_id" in transport.requests[-1][1]


def test_model_job_uses_local_profile_and_submits_server_response_contract() -> None:
    transport = FakeTransport()
    captured = {}

    class FakeProviderExecutor:
        def execute(self, request, paths, profile):
            captured["request"] = request
            captured["paths"] = list(paths)
            captured["profile"] = profile
            return {"content": '{"questions":[]}' }

    profile = _provider_profile()
    runner = DesktopHandoffRunner(
        _config(),
        transport,
        provider_profiles={"primary": profile},
        provider_executor=FakeProviderExecutor(),
    )

    runner._process_model_job(_model_job())

    snapshot = runner.snapshot()
    assert snapshot.completed == 1
    assert snapshot.api_completed == 1
    assert snapshot.api_processing == 0
    assert captured["profile"] is profile
    assert captured["request"]["model"] == "gpt-5.6-sol"
    assert set(captured["request"]["assets"][0]) == {
        "kind", "sha256", "byte_size", "media_type"
    }
    assert transport.downloads[0][0].endswith("/assets/input_01")
    result_request = next(
        payload for path, payload in transport.requests if path.endswith("/result")
    )
    assert result_request["result"] == {
        "contract_version": "vision-model-response-v1",
        "request_digest": "d" * 64,
        "status": "completed",
        "output_text": '{"questions":[]}',
    }


def test_model_job_failure_returns_safe_code_without_retrying_same_lease() -> None:
    transport = FakeTransport()

    class RateLimitedExecutor:
        def execute(self, *_args, **_kwargs):
            raise ProviderExecutorError("provider_rate_limited")

    runner = DesktopHandoffRunner(
        _config(),
        transport,
        provider_profiles={"primary": _provider_profile()},
        provider_executor=RateLimitedExecutor(),
    )

    runner._process_model_job(_model_job())

    failure = next(
        payload for path, payload in transport.requests if path.endswith("/fail")
    )
    assert failure["error_code"] == "provider_rate_limited"
    snapshot = runner.snapshot()
    assert snapshot.api_failed == 1
    assert snapshot.api_last_error_code == "provider_rate_limited"


def test_model_job_rejects_absolute_asset_url_before_download() -> None:
    transport = FakeTransport()
    runner = DesktopHandoffRunner(
        _config(),
        transport,
        provider_profiles={"primary": _provider_profile()},
    )
    job = _model_job()
    job["model_request"]["assets"][0]["url"] = "https://attacker.invalid/image"

    runner._process_model_job(job)

    assert transport.downloads == []
    assert runner.snapshot().last_error_code == "vision_model_request_invalid"


def test_lease_renew_requires_server_leased_status(monkeypatch) -> None:
    class InvalidRenewTransport(FakeTransport):
        def __init__(self):
            super().__init__()
            self.renewed = threading.Event()

        def json_request(self, path, payload):
            self.requests.append((path, payload))
            if path.endswith("/renew"):
                self.renewed.set()
                return {}
            if path.endswith("/fail"):
                return {"status": "queued"}
            if path.endswith("/result"):
                return {"status": "validated", "server_validation_status": "accepted"}
            return {}

    release = threading.Event()

    def blocked_process(*_args, **_kwargs):
        assert release.wait(5.0)
        return _outcome()

    transport = InvalidRenewTransport()
    runner = DesktopHandoffRunner(_config(), transport)
    monkeypatch.setattr("mathagent_worker.handoff._LEASE_RENEW_SECONDS", 0.01)
    monkeypatch.setattr("mathagent_worker.handoff.process_candidate", blocked_process)
    thread = threading.Thread(target=lambda: runner._process_job(_job()))
    thread.start()
    try:
        assert transport.renewed.wait(5.0)
        release.set()
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        assert runner.snapshot().last_error_code == "lease_rejected"
        assert not any(path.endswith("/result") for path, _payload in transport.requests)
    finally:
        release.set()
        thread.join(timeout=5.0)


def test_provider_stage_is_advertised_without_image_hardware() -> None:
    runner = DesktopHandoffRunner(
        _config(),
        FakeTransport(),
        initially_paused=True,
        gpu_enabled=False,
        cpu_fallback_allowed=False,
        cpu_worker_count=0,
        provider_profiles={"primary": _provider_profile()},
        api_slots=8,
    )

    heartbeat = runner._heartbeat_payload()

    assert heartbeat["capabilities"]["stages"] == ["vision_model_request"]
    assert heartbeat["capabilities"]["provider_profiles"] == ["primary"]
    assert heartbeat["capabilities"]["provider_models"] == {
        "primary": "gpt-5.6-sol",
    }
    assert heartbeat["capabilities"]["stage_slots"]["provider_api"] == {
        "stages": ["vision_model_request"],
        "capacity": 8,
    }
    assert heartbeat["free_slots"] == 8


def test_process_jobs_reuse_same_verified_template_download(monkeypatch) -> None:
    transport = FakeTransport()
    runner = DesktopHandoffRunner(_config(), transport)
    monkeypatch.setattr(
        "mathagent_worker.handoff.process_candidate",
        lambda *_args, **_kwargs: _outcome(),
    )

    runner._process_job(_job())
    second = {
        **_job(),
        "job_id": "job-2",
        "source_url": "/api/accelerator/jobs/job-2/assets/source",
        "template_sha256": "B" * 64,
    }
    runner._process_job(second)

    assert [item[0] for item in transport.downloads] == [
        "/api/accelerator/jobs/job-1/assets/source",
        "/api/accelerator/jobs/job-1/assets/template",
        "/api/accelerator/jobs/job-2/assets/source",
    ]
    assert runner.snapshot().completed == 2


def test_concurrent_jobs_download_same_template_only_once(monkeypatch) -> None:
    transport = FakeTransport()
    runner = DesktopHandoffRunner(_config(), transport)
    template_started = threading.Event()
    release_template = threading.Event()
    original_download = transport.download

    def delayed_download(path, lease, target, expected_sha):
        if path.endswith("/assets/template"):
            template_started.set()
            assert release_template.wait(5)
        original_download(path, lease, target, expected_sha)

    transport.download = delayed_download
    monkeypatch.setattr(
        "mathagent_worker.handoff.process_candidate",
        lambda *_args, **_kwargs: _outcome(),
    )
    second = {
        **_job(),
        "job_id": "job-2",
        "source_url": "/api/accelerator/jobs/job-2/assets/source",
        "template_url": "/api/accelerator/jobs/job-2/assets/template",
    }

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(runner._process_job, _job())
        assert template_started.wait(5)
        second_future = executor.submit(runner._process_job, second)
        release_template.set()
        first_future.result(timeout=5)
        second_future.result(timeout=5)

    assert [item[0] for item in transport.downloads].count(
        "/api/accelerator/jobs/job-1/assets/template"
    ) == 1
    assert runner.snapshot().completed == 2


def test_multi_evidence_job_rejects_lease_without_template_before_download() -> None:
    transport = FakeTransport()
    runner = DesktopHandoffRunner(_config(), transport)
    job = {**_job(), "stage": "student_multi_evidence"}
    job.pop("template_url")

    with pytest.raises(DesktopHandoffError, match="lease_payload_invalid"):
        runner._process_job(job)

    assert transport.downloads == []


def test_process_job_uploads_only_final_client_evidence(monkeypatch) -> None:
    transport = FakeTransport()
    runner = DesktopHandoffRunner(_config(), transport)
    monkeypatch.setattr(
        "mathagent_worker.handoff.process_candidate",
        lambda *_args, **_kwargs: _client_evidence_outcome(),
    )

    runner._process_job(_job())

    assert runner.snapshot().completed == 1
    assert [item["kind"] for item in transport.uploads] == ["evidence"]
    assert transport.uploads[0]["source"] == Path("evidence.png")
    assert transport.uploads[0]["expected"] == (
        _client_evidence_outcome().candidate["artifact_manifest"]["evidence"]
    )


def test_process_job_rejects_legacy_aligned_artifact_contract(monkeypatch) -> None:
    transport = FakeTransport()
    runner = DesktopHandoffRunner(_config(), transport)
    outcome = _outcome()
    legacy_candidate = dict(outcome.candidate)
    legacy_candidate["artifact_manifest"] = {
        "contract_version": "aligned-page-v1",
        "aligned_page": {},
        "source_mask": {},
    }
    monkeypatch.setattr(
        "mathagent_worker.handoff.process_candidate",
        lambda *_args, **_kwargs: replace(
            outcome,
            candidate=legacy_candidate,
            evidence_path=None,
        ),
    )

    runner._process_job(_job())

    assert runner.snapshot().failed == 1
    assert runner.snapshot().last_error_code == "artifact_contract_unsupported"
    assert transport.uploads == []


def test_canonical_job_downloads_only_source_and_uploads_one_canonical_png(
    monkeypatch,
) -> None:
    transport = FakeTransport()
    runner = DesktopHandoffRunner(_config(), transport)

    class Result:
        canonical_path: Path
        canonicalization: dict

    def canonicalize(root, source, target):
        assert source.suffix == ".png"
        assert source.parent == Path(root)
        target.write_bytes(b"canonical-png")
        result = Result()
        result.canonical_path = target
        result.canonicalization = {
            "canonical_sha256": hashlib.sha256(b"canonical-png").hexdigest(),
            "canonical_width": 2064,
            "canonical_height": 2064,
        }
        return result

    monkeypatch.setattr("mathagent_worker.handoff.canonicalize_template", canonicalize)
    monkeypatch.setattr(
        "mathagent_worker.handoff.process_candidate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("registration processor was called")
        ),
    )

    runner._process_job(_canonical_job())

    assert runner.snapshot().completed == 1
    assert [item[0] for item in transport.downloads] == [
        "/api/accelerator/jobs/job-1/assets/source"
    ]
    assert [item["kind"] for item in transport.uploads] == [
        "canonical_template"
    ]
    submitted = transport.requests[-1][1]["result"]
    assert set(submitted) == {"status", "artifact_manifest", "canonicalization"}
    assert submitted["artifact_manifest"]["contract_version"] == (
        "client-canonical-template-v1"
    )


def test_template_ocr_evidence_job_downloads_only_canonical_and_uploads_montage(
    monkeypatch,
) -> None:
    transport = FakeTransport()
    runner = DesktopHandoffRunner(_config(), transport)
    candidate = {
        "status": "succeeded",
        "artifact_manifest": {
            "contract_version": "template-ocr-evidence-v2",
            "template_ocr_evidence": {
                "sha256": "e" * 64,
                "byte_size": 100,
                "width": 800,
                "height": 1000,
                "channels": 3,
                "format": "png",
            },
        },
        "input_identity": {
            "canonical_sha256": "a" * 64,
            "layout_snapshot_sha256": "c" * 64,
        },
    }

    def render(source, job, output):
        assert source.name == "source.png"
        assert job["stage"] == "template_ocr_evidence"
        output.write_bytes(b"e" * 100)
        return TemplateOcrEvidenceResult(output, candidate)

    monkeypatch.setattr(
        "mathagent_worker.handoff.render_template_ocr_evidence",
        render,
    )
    monkeypatch.setattr(
        "mathagent_worker.handoff.process_candidate",
        lambda *_args, **_kwargs: pytest.fail("registration processor was called"),
    )

    runner._process_job(_template_ocr_evidence_job())

    assert runner.snapshot().completed == 1
    assert runner.snapshot().backend == "windows_cpu_pillow"
    assert [item[0] for item in transport.downloads] == [
        "/api/accelerator/jobs/job-1/assets/source"
    ]
    assert [item["kind"] for item in transport.uploads] == [
        "template_ocr_evidence"
    ]
    submitted = transport.requests[-1][1]["result"]
    assert submitted == candidate


def test_http_artifact_upload_streams_file_without_read_bytes(tmp_path, monkeypatch):
    source = tmp_path / "canonical.png"
    payload = b"streamed-canonical-artifact"
    source.write_bytes(payload)
    expected = {
        "sha256": hashlib.sha256(payload).hexdigest(),
        "byte_size": len(payload),
        "width": 10,
        "height": 10,
        "channels": 3,
        "format": "png",
    }

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            return json.dumps({
                "job_id": "job-1",
                "kind": "canonical_template",
                **expected,
            }).encode("utf-8")

    def fake_urlopen(request, timeout):
        assert timeout == 60
        assert request.data.read() == payload
        return Response()

    monkeypatch.setattr("mathagent_worker.handoff.urlopen", fake_urlopen)
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("read_bytes must not be used")
        ),
    )

    HttpTransport(_config()).upload_artifact(
        job_id="job-1",
        kind="canonical_template",
        lease="l" * 32,
        source=source,
        expected=expected,
    )


def test_http_transport_rejects_absolute_server_paths(monkeypatch):
    monkeypatch.setattr(
        "mathagent_worker.handoff.urlopen",
        lambda *_args, **_kwargs: pytest.fail("network must not be called"),
    )
    transport = HttpTransport(_config())

    with pytest.raises(DesktopHandoffError, match="server_path_invalid"):
        transport.json_request("https://attacker.invalid/steal", {})


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (URLError(socket.gaierror("name resolution failed")), "server_dns_error"),
        (
            URLError(ssl.SSLCertVerificationError(1, "certificate verify failed")),
            "server_tls_certificate_error",
        ),
        (TimeoutError("request timed out"), "server_timeout"),
    ],
)
def test_http_transport_reports_a_specific_connection_failure(
    monkeypatch,
    error,
    expected_code,
):
    monkeypatch.setattr(
        "mathagent_worker.handoff.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )

    with pytest.raises(DesktopHandoffError) as raised:
        HttpTransport(_config()).json_request("/api/accelerator/nodes/heartbeat", {})

    assert raised.value.code == expected_code


def test_long_running_job_renews_lease_until_compute_finishes(monkeypatch) -> None:
    class RenewingTransport(FakeTransport):
        def __init__(self):
            super().__init__()
            self.renewed = threading.Event()
            self.lock = threading.Lock()

        def json_request(self, path, payload):
            with self.lock:
                self.requests.append((path, payload))
            if path.endswith("/renew"):
                self.renewed.set()
                return {"status": "leased"}
            if path.endswith("/result"):
                return {
                    "status": "validated",
                    "server_validation_status": "accepted",
                }
            if path.endswith("/fail"):
                return {"status": "queued"}
            return {}

    release = threading.Event()

    def blocked_process(*_args, **_kwargs):
        assert release.wait(5.0)
        return _outcome()

    transport = RenewingTransport()
    runner = DesktopHandoffRunner(_config(), transport)
    monkeypatch.setattr("mathagent_worker.handoff._LEASE_RENEW_SECONDS", 0.01)
    monkeypatch.setattr("mathagent_worker.handoff.process_candidate", blocked_process)
    thread = threading.Thread(target=lambda: runner._process_job(_job()))
    thread.start()
    try:
        assert transport.renewed.wait(5.0)
        release.set()
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        assert runner.snapshot().completed == 1
        renew_index = next(
            index
            for index, (path, _payload) in enumerate(transport.requests)
            if path.endswith("/renew")
        )
        result_index = next(
            index
            for index, (path, _payload) in enumerate(transport.requests)
            if path.endswith("/result")
        )
        assert renew_index < result_index
    finally:
        release.set()
        thread.join(timeout=5.0)


def test_transient_lease_renew_failure_is_retried(monkeypatch) -> None:
    class RetryTransport(FakeTransport):
        def __init__(self):
            super().__init__()
            self.renew_calls = 0
            self.retried = threading.Event()
            self.lock = threading.Lock()

        def json_request(self, path, payload):
            with self.lock:
                self.requests.append((path, payload))
                if path.endswith("/renew"):
                    self.renew_calls += 1
                    if self.renew_calls == 1:
                        raise DesktopHandoffError("server_unreachable")
                    self.retried.set()
                    return {"status": "leased"}
            if path.endswith("/result"):
                return {
                    "status": "validated",
                    "server_validation_status": "accepted",
                }
            if path.endswith("/fail"):
                return {"status": "queued"}
            return {}

    release = threading.Event()

    def blocked_process(*_args, **_kwargs):
        assert release.wait(5.0)
        return _outcome()

    transport = RetryTransport()
    runner = DesktopHandoffRunner(_config(), transport)
    monkeypatch.setattr("mathagent_worker.handoff._LEASE_RENEW_SECONDS", 0.01)
    monkeypatch.setattr("mathagent_worker.handoff._LEASE_RENEW_RETRY_SECONDS", 0.01)
    monkeypatch.setattr("mathagent_worker.handoff.process_candidate", blocked_process)
    thread = threading.Thread(target=lambda: runner._process_job(_job()))
    thread.start()
    try:
        assert transport.retried.wait(5.0)
        release.set()
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        assert transport.renew_calls >= 2
        assert runner.snapshot().completed == 1
    finally:
        release.set()
        thread.join(timeout=5.0)


def test_gpu_only_runner_disables_unqualified_cpu_fallback(monkeypatch) -> None:
    transport = FakeTransport()
    runner = DesktopHandoffRunner(
        _config(),
        transport,
        gpu_enabled=True,
        cpu_fallback_allowed=False,
        cpu_worker_count=0,
    )
    captured = []

    def process(*_args, **kwargs):
        captured.append((kwargs["allow_gpu"], kwargs["allow_cpu_fallback"]))
        return _outcome()

    monkeypatch.setattr("mathagent_worker.handoff.process_candidate", process)

    runner._process_job(_job())

    assert captured == [(True, False)]
    assert runner.snapshot().completed == 1
    assert runner.snapshot().cpu_compute_slots == 0


def test_unqualified_runner_starts_paused_and_never_leases() -> None:
    class PausedTransport(FakeTransport):
        def __init__(self):
            super().__init__()
            self.lock = threading.Lock()
            self.lease_calls = 0
            self.heartbeats = []

        def json_request(self, path, payload):
            with self.lock:
                self.requests.append((path, payload))
                if path.endswith("/heartbeat"):
                    self.heartbeats.append(payload)
                    return {}
                if path.endswith("/lease"):
                    self.lease_calls += 1
                    return {"job": _job()}
                raise AssertionError(path)

    transport = PausedTransport()
    runner = DesktopHandoffRunner(
        _config(),
        transport,
        initially_paused=True,
        gpu_enabled=False,
        cpu_fallback_allowed=False,
        cpu_worker_count=0,
    )
    runner.start()
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not transport.heartbeats:
            time.sleep(0.01)
        assert transport.heartbeats
        assert transport.lease_calls == 0
        assert transport.heartbeats[0]["free_slots"] == 0
        assert transport.heartbeats[0]["capabilities"]["backend"] == "none"
        assert transport.heartbeats[0]["capabilities"]["stages"] == []
        assert transport.heartbeats[0]["capabilities"]["cpu_compute_slots"] == 0
        assert runner.snapshot().state == "paused"
    finally:
        runner.stop()


def test_processing_failure_reports_bounded_code_and_advances_failed(monkeypatch) -> None:
    transport = FakeTransport()
    runner = DesktopHandoffRunner(_config(), transport)

    def broken(*_args, **_kwargs):
        raise RuntimeError("gpu_operation_failed")

    monkeypatch.setattr("mathagent_worker.handoff.process_candidate", broken)
    runner._process_job(_job())

    snapshot = runner.snapshot()
    assert snapshot.completed == 0
    assert snapshot.failed == 1
    assert snapshot.remaining == 0
    assert snapshot.processing == 0
    assert snapshot.last_error_code == "gpu_operation_failed"
    assert transport.requests[-1] == (
        "/api/accelerator/jobs/job-1/fail",
        {"lease_id": "l" * 32, "error_code": "gpu_operation_failed"},
    )


def test_unexpected_failure_reports_only_safe_exception_class(monkeypatch) -> None:
    transport = FakeTransport()
    runner = DesktopHandoffRunner(_config(), transport)

    def broken(*_args, **_kwargs):
        raise KeyError("sensitive-local-detail")

    monkeypatch.setattr("mathagent_worker.handoff.process_candidate", broken)
    runner._process_job(_job())

    snapshot = runner.snapshot()
    assert snapshot.failed == 1
    assert snapshot.last_error_code == "processing_keyerror"
    assert "sensitive" not in repr(snapshot.as_status())


def test_server_rejected_candidate_counts_as_failed_not_completed(monkeypatch) -> None:
    class RejectedTransport(FakeTransport):
        def json_request(self, path, payload):
            self.requests.append((path, payload))
            if path.endswith("/result"):
                return {
                    "status": "server_fallback",
                    "server_validation_status": "rejected",
                }
            return {}

    transport = RejectedTransport()
    runner = DesktopHandoffRunner(_config(), transport)
    monkeypatch.setattr(
        "mathagent_worker.handoff.process_candidate",
        lambda *_args, **_kwargs: _outcome(),
    )

    runner._process_job(_job())

    snapshot = runner.snapshot()
    assert snapshot.completed == 0
    assert snapshot.failed == 1
    assert snapshot.remaining == 0
    assert snapshot.last_error_code == "server_candidate_contract_mismatch"
    assert snapshot.last_job_status == "server_fallback"


def test_snapshot_never_contains_server_token_or_local_path() -> None:
    runner = DesktopHandoffRunner(_config(), FakeTransport())
    status = runner.snapshot().as_status()
    rendered = repr(status)

    assert "secret-token" not in rendered
    assert "example.invalid" not in rendered
    assert "\\" not in rendered


def test_heartbeat_reports_remaining_not_total_lease_slots() -> None:
    runner = DesktopHandoffRunner(_config(), FakeTransport())

    runner._begin_job()

    payload = runner._heartbeat_payload()
    assert payload["capabilities"]["lease_slots"] == 8
    assert payload["capabilities"]["stages"] == [
        "template_registration",
        "student_multi_evidence",
        "template_canonicalization",
        "template_ocr_evidence",
    ]
    assert payload["capabilities"]["artifact_contracts"] == [
        "client-evidence-v1",
        "client-evidence-multi-v3",
        "client-evidence-columns-v1",
        "client-evidence-slot-geometry-v1",
        "client-canonical-template-v1",
        "template-ocr-evidence-v2",
    ]
    assert payload["free_slots"] == 7
    assert payload["queue_depth"] == 1
    assert payload["capabilities"]["image_compute_slots"] == 8
    assert payload["capabilities"]["stage_slots"]["image"]["capacity"] == 8
    assert payload["capabilities"]["hardware_ready"] is True


def test_successful_heartbeat_clears_only_transport_error() -> None:
    class RecoveringTransport(FakeTransport):
        def __init__(self):
            super().__init__()
            self.heartbeat_count = 0
            self.recovered = threading.Event()

        def json_request(self, path, payload):
            self.requests.append((path, payload))
            if path.endswith("/heartbeat"):
                self.heartbeat_count += 1
                if self.heartbeat_count == 1:
                    raise DesktopHandoffError("server_unreachable")
                self.recovered.set()
                return {}
            if path.endswith("/lease"):
                return {"job": None}
            return super().json_request(path, payload)

    transport = RecoveringTransport()
    runner = DesktopHandoffRunner(
        _config(),
        transport,
        initially_paused=True,
        gpu_enabled=False,
        cpu_fallback_allowed=False,
        cpu_worker_count=0,
    )
    runner._set(last_error_code="server_unreachable")
    runner.start()
    try:
        assert transport.recovered.wait(2.0)
        assert runner.snapshot().connected is True
        assert runner.snapshot().last_error_code is None
    finally:
        runner.stop(timeout=2.0)


def test_unqualified_unpaused_runner_advertises_no_stage_and_never_leases() -> None:
    class UnavailableTransport(FakeTransport):
        def __init__(self):
            super().__init__()
            self.heartbeat = threading.Event()
            self.lease_calls = 0

        def json_request(self, path, payload):
            self.requests.append((path, payload))
            if path.endswith("/heartbeat"):
                self.heartbeat.set()
                return {}
            if path.endswith("/lease"):
                self.lease_calls += 1
                return {"job": _job()}
            raise AssertionError(path)

    transport = UnavailableTransport()
    runner = DesktopHandoffRunner(
        _config(),
        transport,
        initially_paused=False,
        gpu_enabled=False,
        cpu_fallback_allowed=False,
        cpu_worker_count=0,
    )

    runner.start()
    try:
        assert transport.heartbeat.wait(2.0)
        time.sleep(0.3)
        heartbeat = next(
            payload
            for path, payload in transport.requests
            if path.endswith("/heartbeat")
        )
        assert heartbeat["capabilities"]["stages"] == []
        assert heartbeat["free_slots"] == 0
        assert transport.lease_calls == 0
    finally:
        runner.stop()


def test_unsupported_stage_is_failed_before_asset_download(monkeypatch) -> None:
    transport = FakeTransport()
    runner = DesktopHandoffRunner(_config(), transport)
    monkeypatch.setattr(
        "mathagent_worker.handoff.process_candidate",
        lambda *_args, **_kwargs: pytest.fail("processing must not run"),
    )
    job = {**_job(), "stage": "document_render"}

    runner._process_job(job)

    assert transport.downloads == []
    failure_path, failure_payload = transport.requests[-1]
    assert failure_path.endswith("/job-1/fail")
    assert failure_payload["error_code"] == "unsupported_accelerator_stage"
    assert runner.snapshot().last_error_code == "unsupported_accelerator_stage"


def test_runner_leases_eight_jobs_and_uses_eight_gpu_compute(monkeypatch) -> None:
    class ConcurrentTransport(FakeTransport):
        def __init__(self):
            super().__init__()
            self.lock = threading.Lock()
            self.jobs = [
                {**_job(), "job_id": f"job-{index}"}
                for index in range(8)
            ]

        def json_request(self, path, payload):
            with self.lock:
                self.requests.append((path, payload))
                if path.endswith("/heartbeat"):
                    return {}
                if path.endswith("/lease"):
                    return {"job": self.jobs.pop(0) if self.jobs else None}
                if path.endswith("/result"):
                    return {
                        "status": "validated",
                        "server_validation_status": "accepted",
                    }
                raise AssertionError(path)

        def download(self, path, lease, target, expected_sha):
            with self.lock:
                self.downloads.append((path, lease, expected_sha))
            target.write_bytes(b"image")

        def upload_artifact(self, **kwargs):
            with self.lock:
                self.uploads.append(kwargs)

    transport = ConcurrentTransport()
    runner = DesktopHandoffRunner(_config(), transport)
    release = threading.Event()
    eight_entered = threading.Event()
    active_lock = threading.Lock()
    active = 0
    max_active = 0

    def bounded_process(*_args, **_kwargs):
        nonlocal active, max_active
        with active_lock:
            active += 1
            max_active = max(max_active, active)
            if active == 8:
                eight_entered.set()
        try:
            assert release.wait(5.0)
            return _outcome()
        finally:
            with active_lock:
                active -= 1

    monkeypatch.setattr(
        "mathagent_worker.handoff.process_candidate",
        bounded_process,
    )
    runner.start()
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if eight_entered.is_set() and runner.snapshot().processing == 8:
                break
            time.sleep(0.01)
        assert eight_entered.is_set()
        assert runner.snapshot().processing == 8
        release.set()
        while time.monotonic() < deadline:
            if runner.snapshot().completed == 8:
                break
            time.sleep(0.01)
        snapshot = runner.snapshot()
        assert snapshot.completed == 8
        assert snapshot.failed == 0
        assert snapshot.remaining == 0
        assert snapshot.processing == 0
        assert max_active == 8
        heartbeat = next(
            payload
            for path, payload in transport.requests
            if path.endswith("/heartbeat")
        )
        assert heartbeat["free_slots"] == 8
        assert heartbeat["capabilities"]["cpu_compute_slots"] == 3
        assert heartbeat["capabilities"]["image_compute_slots"] == 8
        assert heartbeat["capabilities"]["stage_slots"]["image"]["capacity"] == 8
        assert heartbeat["capabilities"]["gpu_contexts"] == 1
        assert len(transport.uploads) == 8
    finally:
        release.set()
        runner.stop()


def test_cpu_fallback_keeps_three_image_compute_slots() -> None:
    runner = DesktopHandoffRunner(
        _config(),
        FakeTransport(),
        gpu_enabled=False,
        cpu_fallback_allowed=True,
        cpu_worker_count=3,
    )

    assert runner.snapshot().image_compute_slots == 3
    heartbeat = runner._heartbeat_payload()
    assert heartbeat["capabilities"]["lease_slots"] == 8
    assert heartbeat["capabilities"]["image_compute_slots"] == 3
    assert heartbeat["capabilities"]["cpu_compute_slots"] == 3
    assert heartbeat["capabilities"]["hardware_ready"] is True


def test_heartbeat_marks_hardware_unavailable_when_no_local_route() -> None:
    runner = DesktopHandoffRunner(
        _config(),
        FakeTransport(),
        gpu_enabled=False,
        cpu_fallback_allowed=False,
        cpu_worker_count=0,
    )

    heartbeat = runner._heartbeat_payload()

    assert heartbeat["capabilities"]["hardware_ready"] is False
    assert heartbeat["free_slots"] == 0
    assert heartbeat["capabilities"]["stages"] == []


def test_pause_stops_new_leases_but_resume_continues(monkeypatch) -> None:
    class OfferedTransport(FakeTransport):
        def __init__(self):
            super().__init__()
            self.lock = threading.Lock()
            self.offer_second = threading.Event()
            self.first_leased = False
            self.second_leased = False

        def json_request(self, path, payload):
            with self.lock:
                self.requests.append((path, payload))
                if path.endswith("/heartbeat"):
                    return {}
                if path.endswith("/lease"):
                    if not self.first_leased:
                        self.first_leased = True
                        return {"job": {**_job(), "job_id": "job-first"}}
                    if self.offer_second.is_set() and not self.second_leased:
                        self.second_leased = True
                        return {"job": {**_job(), "job_id": "job-second"}}
                    return {"job": None}
                if path.endswith("/result"):
                    return {
                        "status": "validated",
                        "server_validation_status": "accepted",
                    }
                raise AssertionError(path)

    transport = OfferedTransport()
    runner = DesktopHandoffRunner(_config(), transport)
    monkeypatch.setattr(
        "mathagent_worker.handoff.process_candidate",
        lambda *_args, **_kwargs: _outcome(),
    )
    runner.start()
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and runner.snapshot().completed < 1:
            time.sleep(0.01)
        assert runner.snapshot().completed == 1
        runner.pause()
        transport.offer_second.set()
        time.sleep(0.4)
        assert runner.snapshot().completed == 1
        assert transport.second_leased is False
        runner.resume()
        while time.monotonic() < deadline and runner.snapshot().completed < 2:
            time.sleep(0.01)
        assert runner.snapshot().completed == 2
        assert transport.second_leased is True
    finally:
        runner.stop()


def test_lease_rejection_after_upload_cleans_local_artifacts(monkeypatch) -> None:
    class LeaseRejectedTransport(FakeTransport):
        def json_request(self, path, payload):
            self.requests.append((path, payload))
            if path.endswith("/result") or path.endswith("/fail"):
                raise DesktopHandoffError("lease_rejected")
            return {}

    created: list[Path] = []

    def materialize(*_args, artifact_output_dir, **_kwargs):
        root = Path(artifact_output_dir)
        root.mkdir(parents=True, exist_ok=True)
        evidence = root / "evidence.png"
        evidence.write_bytes(b"e" * 100)
        created.append(evidence)
        return replace(
            _outcome(),
            evidence_path=evidence,
        )

    transport = LeaseRejectedTransport()
    runner = DesktopHandoffRunner(_config(), transport)
    monkeypatch.setattr("mathagent_worker.handoff.process_candidate", materialize)

    runner._process_job(_job())

    snapshot = runner.snapshot()
    assert len(transport.uploads) == 1
    assert snapshot.completed == 0
    assert snapshot.failed == 1
    assert snapshot.processing == 0
    assert snapshot.last_error_code == "lease_rejected"
    assert created and all(not path.exists() for path in created)


def test_future_protocol_exception_is_reclaimed_and_runner_continues(monkeypatch) -> None:
    class RecoveringTransport(FakeTransport):
        def __init__(self):
            super().__init__()
            self.lock = threading.Lock()
            self.bad_leased = False
            self.offer_good = threading.Event()
            self.good_leased = False

        def json_request(self, path, payload):
            with self.lock:
                self.requests.append((path, payload))
                if path.endswith("/heartbeat"):
                    return {}
                if path.endswith("/lease"):
                    if not self.bad_leased:
                        self.bad_leased = True
                        return {"job": {"job_id": "malformed"}}
                    if self.offer_good.is_set() and not self.good_leased:
                        self.good_leased = True
                        return {"job": {**_job(), "job_id": "good"}}
                    return {"job": None}
                if path.endswith("/result"):
                    return {
                        "status": "validated",
                        "server_validation_status": "accepted",
                    }
                raise AssertionError(path)

    transport = RecoveringTransport()
    runner = DesktopHandoffRunner(_config(), transport)
    monkeypatch.setattr(
        "mathagent_worker.handoff.process_candidate",
        lambda *_args, **_kwargs: _outcome(),
    )
    runner.start()
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if runner.snapshot().last_error_code == "worker_internal_error":
                break
            time.sleep(0.01)
        assert runner.snapshot().last_error_code == "worker_internal_error"
        transport.offer_good.set()
        while time.monotonic() < deadline and runner.snapshot().completed < 1:
            time.sleep(0.01)
        assert runner.snapshot().completed == 1
        assert runner.snapshot().processing == 0
        assert transport.good_leased is True
    finally:
        runner.stop()


def test_pause_during_inflight_lease_drains_only_that_lease(monkeypatch) -> None:
    class BlockingLeaseTransport(FakeTransport):
        def __init__(self):
            super().__init__()
            self.lease_started = threading.Event()
            self.release_lease = threading.Event()
            self.lease_calls = 0

        def json_request(self, path, payload):
            self.requests.append((path, payload))
            if path.endswith("/heartbeat"):
                return {}
            if path.endswith("/lease"):
                self.lease_calls += 1
                if self.lease_calls == 1:
                    self.lease_started.set()
                    assert self.release_lease.wait(5.0)
                    return {"job": {**_job(), "job_id": "inflight"}}
                return {"job": {**_job(), "job_id": f"extra-{self.lease_calls}"}}
            if path.endswith("/result"):
                return {
                    "status": "validated",
                    "server_validation_status": "accepted",
                }
            raise AssertionError(path)

    transport = BlockingLeaseTransport()
    runner = DesktopHandoffRunner(_config(), transport)
    monkeypatch.setattr(
        "mathagent_worker.handoff.process_candidate",
        lambda *_args, **_kwargs: _outcome(),
    )
    runner.start()
    try:
        assert transport.lease_started.wait(5.0)
        runner.pause()
        transport.release_lease.set()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and runner.snapshot().completed < 1:
            time.sleep(0.01)
        assert runner.snapshot().completed == 1
        time.sleep(0.2)
        assert transport.lease_calls == 1
        assert runner.snapshot().state == "paused"
    finally:
        transport.release_lease.set()
        runner.stop()


def test_stop_waits_for_inflight_job_before_reporting_stopped(monkeypatch) -> None:
    class OneJobTransport(FakeTransport):
        def __init__(self):
            super().__init__()
            self.offered = False

        def json_request(self, path, payload):
            self.requests.append((path, payload))
            if path.endswith("/heartbeat"):
                return {}
            if path.endswith("/lease"):
                if not self.offered:
                    self.offered = True
                    return {"job": _job()}
                return {"job": None}
            if path.endswith("/result"):
                return {
                    "status": "validated",
                    "server_validation_status": "accepted",
                }
            raise AssertionError(path)

    entered = threading.Event()
    release = threading.Event()

    def blocked_process(*_args, **_kwargs):
        entered.set()
        assert release.wait(5.0)
        return _outcome()

    runner = DesktopHandoffRunner(_config(), OneJobTransport())
    monkeypatch.setattr(
        "mathagent_worker.handoff.process_candidate",
        blocked_process,
    )
    runner.start()
    assert entered.wait(5.0)
    stopped = []
    stopper = threading.Thread(target=lambda: stopped.append(runner.stop()))
    stopper.start()
    try:
        time.sleep(0.1)
        assert stopper.is_alive()
        assert runner.snapshot().state == "stopping"
        release.set()
        stopper.join(timeout=5.0)
        assert not stopper.is_alive()
        assert stopped == [True]
        assert runner.snapshot().state == "stopped"
        assert runner.snapshot().completed == 1
    finally:
        release.set()
        stopper.join(timeout=5.0)


@pytest.mark.parametrize("terminal_status", ["consumed", "superseded"])
def test_result_timeouts_retry_identical_payload_without_reprocessing_or_false_failure(
    monkeypatch, terminal_status,
) -> None:
    class TimeoutTransport(FakeTransport):
        def json_request(self, path, payload):
            self.requests.append((path, payload))
            if path.endswith("/result"):
                attempts = sum(item[0].endswith("/result") for item in self.requests)
                if attempts < 3:
                    raise DesktopHandoffError("server_timeout")
                return {
                    "job_id": "job-1",
                    "status": terminal_status,
                    "server_validation_status": "accepted",
                }
            return {}

    outcome = _outcome()
    calls = []
    transport = TimeoutTransport()
    runner = DesktopHandoffRunner(_config(), transport)

    def process(*args, **kwargs):
        calls.append(True)
        return outcome

    monkeypatch.setattr(handoff, "process_candidate", process)
    runner._process_job(_job())
    pending = runner._pending_results["job-1"]
    first_retry = pending.next_attempt_at
    assert runner.snapshot().processing == 0
    assert runner.snapshot().pending_results == 1
    assert runner.snapshot().remaining == 1
    assert runner.snapshot().failed == runner.snapshot().completed == 0
    assert runner.snapshot().last_job_status == "result_pending"
    assert first_retry > time.monotonic()
    outcome.candidate["good_matches"] = 999

    runner._submit_result(pending)
    assert pending.next_attempt_at > first_retry
    assert runner.snapshot().failed == runner.snapshot().completed == 0
    runner._submit_result(pending)
    runner._submit_result(pending)  # A completed receipt cannot count twice.

    submitted = [payload for path, payload in transport.requests if path.endswith("/result")]
    assert len(submitted) == 3
    assert submitted[0] == submitted[1] == submitted[2]
    assert submitted[0]["result"]["good_matches"] == 10
    assert not any(path.endswith("/fail") for path, _ in transport.requests)
    assert len(calls) == len(transport.uploads) == 1
    assert runner.snapshot().completed == 1
    assert runner.snapshot().failed == 0
    assert runner.snapshot().remaining == runner.snapshot().pending_results == 0
    assert runner.snapshot().last_job_status == terminal_status


@pytest.mark.parametrize("response", [
    {"status": "candidate_received", "server_validation_status": "pending"},
    {"status": "server_validating", "server_validation_status": "pending"},
    {"status": "validated"},
    {"status": "consumed"},
    {"status": "superseded", "server_validation_status": "accepted"},
    {"job_id": "another-job", "status": "superseded", "server_validation_status": "accepted"},
    {"job_id": "another-job", "status": "validated", "server_validation_status": "accepted"},
])
def test_result_requires_authoritative_acceptance_for_the_same_job(monkeypatch, response) -> None:
    class PendingTransport(FakeTransport):
        def json_request(self, path, payload):
            self.requests.append((path, payload))
            return response if path.endswith("/result") else {}

    transport = PendingTransport()
    runner = DesktopHandoffRunner(_config(), transport)
    monkeypatch.setattr(handoff, "process_candidate", lambda *_args, **_kwargs: _outcome())
    runner._process_job(_job())
    snapshot = runner.snapshot()
    assert snapshot.pending_results == snapshot.remaining == 1
    assert snapshot.failed == snapshot.completed == snapshot.processing == 0
    assert not any(path.endswith("/fail") for path, _ in transport.requests)


@pytest.mark.parametrize("validation_status", [None, "pending", "rejected"])
def test_superseded_receipt_without_acceptance_is_not_success(monkeypatch, validation_status):
    class SupersededTransport(FakeTransport):
        def json_request(self, path, payload):
            self.requests.append((path, payload))
            if path.endswith("/result"):
                return {
                    "job_id": "job-1",
                    "status": "superseded",
                    "server_validation_status": validation_status,
                }
            return {}

    transport = SupersededTransport()
    runner = DesktopHandoffRunner(_config(), transport)
    monkeypatch.setattr(handoff, "process_candidate", lambda *_args, **_kwargs: _outcome())
    runner._process_job(_job())
    assert runner.snapshot().completed == 0
    assert runner.snapshot().failed == 1
    assert runner.snapshot().pending_results == runner.snapshot().remaining == 0
    assert runner.snapshot().last_job_status == "superseded"
    assert not any(path.endswith("/fail") for path, _ in transport.requests)


class _DelayedReceiptTransport(FakeTransport):
    def __init__(self, count):
        super().__init__()
        self.lock = threading.Lock()
        self.jobs = [{**_job(), "job_id": f"job-{index}"} for index in range(count)]
        self.result_started = threading.Event()
        self.release_results = threading.Event()
        self.result_active = 0
        self.result_peak = 0

    def json_request(self, path, payload):
        with self.lock:
            self.requests.append((path, payload))
            if path.endswith("/lease"):
                return {"job": self.jobs.pop(0) if self.jobs else None}
            if not path.endswith("/result"):
                return {}
            self.result_active += 1
            self.result_peak = max(self.result_peak, self.result_active)
        self.result_started.set()
        try:
            assert self.release_results.wait(5.0)
            return {
                "job_id": path.split("/")[-2],
                "status": "validated",
                "server_validation_status": "accepted",
            }
        finally:
            with self.lock:
                self.result_active -= 1


def test_pending_receipts_do_not_occupy_image_processing_slots(monkeypatch) -> None:
    transport = _DelayedReceiptTransport(24)
    runner = DesktopHandoffRunner(_config(), transport)
    monkeypatch.setattr(handoff, "process_candidate", lambda *_args, **_kwargs: _outcome())
    runner.start()
    try:
        assert transport.result_started.wait(3.0)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and runner.snapshot().pending_results < 24:
            time.sleep(0.01)
        snapshot = runner.snapshot()
        assert snapshot.pending_results == snapshot.remaining == 24
        assert snapshot.processing == snapshot.completed == snapshot.failed == 0
        assert transport.jobs == []
        assert len(transport.uploads) == 24
        assert transport.result_peak == handoff._RESULT_SUBMIT_SLOTS
        assert runner._heartbeat_payload()["free_slots"] == 8
        assert snapshot.as_status()["pendingResults"] == 24
        transport.release_results.set()
        while time.monotonic() < deadline and runner.snapshot().completed < 24:
            time.sleep(0.01)
        assert runner.snapshot().completed == 24
        assert runner.snapshot().remaining == 0
        assert runner.snapshot().failed == 0
    finally:
        transport.release_results.set()
        assert runner.stop(timeout=5.0)


def test_receipt_backlog_applies_bounded_admission_without_blocking_pause(monkeypatch) -> None:
    monkeypatch.setattr(handoff, "_RESULT_PENDING_LIMIT", 4)
    monkeypatch.setattr(handoff, "_RESULT_SUBMIT_SLOTS", 2)
    transport = _DelayedReceiptTransport(10)
    runner = DesktopHandoffRunner(_config(), transport)
    monkeypatch.setattr(handoff, "process_candidate", lambda *_args, **_kwargs: _outcome())
    runner.start()
    try:
        assert transport.result_started.wait(3.0)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and runner.snapshot().pending_results < 4:
            time.sleep(0.01)
        assert runner.snapshot().pending_results == 4
        assert runner._heartbeat_payload()["free_slots"] == 0
        time.sleep(0.2)
        assert len(transport.jobs) == 6
        assert transport.result_peak == 2
        runner.pause()
        transport.release_results.set()
        while time.monotonic() < deadline and runner.snapshot().completed < 4:
            time.sleep(0.01)
        assert runner.snapshot().completed == 4
        assert runner.snapshot().pending_results == 0
        assert len(transport.jobs) == 6
        assert runner.snapshot().state == "paused"
    finally:
        transport.release_results.set()
        assert runner.stop(timeout=5.0)


def test_stop_waits_for_receipt_without_claiming_success_early(monkeypatch) -> None:
    transport = _DelayedReceiptTransport(1)
    runner = DesktopHandoffRunner(_config(), transport)
    monkeypatch.setattr(handoff, "process_candidate", lambda *_args, **_kwargs: _outcome())
    runner.start()
    try:
        assert transport.result_started.wait(3.0)
        assert runner.stop(timeout=0.03) is False
        snapshot = runner.snapshot()
        assert snapshot.state == "stopping"
        assert snapshot.processing == snapshot.failed == snapshot.completed == 0
        assert snapshot.pending_results == snapshot.remaining == 1
        transport.release_results.set()
        assert runner.stop(timeout=3.0)
        assert runner.snapshot().completed == 1
        assert runner.snapshot().state == "stopped"
    finally:
        transport.release_results.set()
        runner.stop(timeout=5.0)


def test_stop_bounds_receipt_retries_and_retains_honest_unconfirmed_count(monkeypatch) -> None:
    class OfflineTransport(FakeTransport):
        def json_request(self, path, payload):
            self.requests.append((path, payload))
            if path.endswith("/result"):
                raise DesktopHandoffError("server_timeout")
            return {}

    monkeypatch.setattr(handoff, "_RESULT_SHUTDOWN_DRAIN_SECONDS", 0.1)
    transport = OfflineTransport()
    runner = DesktopHandoffRunner(_config(), transport)
    monkeypatch.setattr(handoff, "process_candidate", lambda *_args, **_kwargs: _outcome())
    runner._process_job(_job())
    runner.start()
    assert runner.stop(timeout=2.0)
    snapshot = runner.snapshot()
    assert snapshot.state == "stopped"
    assert not snapshot.running
    assert snapshot.pending_results == snapshot.remaining == 1
    assert snapshot.failed == snapshot.completed == snapshot.processing == 0
    assert len([path for path, _ in transport.requests if path.endswith("/result")]) <= 2
    assert not any(path.endswith("/fail") for path, _ in transport.requests)

    # Same-runner recovery confirms the original result without downloading or
    # computing again, after a previously bounded shutdown while disconnected.
    transport.json_request = FakeTransport.json_request.__get__(transport)
    runner.start()
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and runner.snapshot().completed < 1:
            time.sleep(0.01)
        assert runner.snapshot().completed == 1
        assert runner.snapshot().remaining == runner.snapshot().pending_results == 0
        assert len(transport.uploads) == 1
    finally:
        assert runner.stop(timeout=2.0)


def test_queued_result_keeps_lease_alive_until_its_first_submission(monkeypatch) -> None:
    class RenewingTransport(_DelayedReceiptTransport):
        def json_request(self, path, payload):
            if path.endswith("/renew"):
                with self.lock:
                    self.requests.append((path, payload))
                return {"status": "leased"}
            return super().json_request(path, payload)

    monkeypatch.setattr(handoff, "_RESULT_SUBMIT_SLOTS", 1)
    monkeypatch.setattr(handoff, "_LEASE_RENEW_SECONDS", 0.03)
    transport = RenewingTransport(3)
    runner = DesktopHandoffRunner(_config(), transport)
    monkeypatch.setattr(handoff, "process_candidate", lambda *_args, **_kwargs: _outcome())
    runner.start()
    try:
        assert transport.result_started.wait(3.0)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and runner.snapshot().pending_results < 3:
            time.sleep(0.01)
        time.sleep(0.1)
        with transport.lock:
            requests = list(transport.requests)
        submitted = {path.split("/")[-2] for path, _ in requests if path.endswith("/result")}
        renewed = {path.split("/")[-2] for path, _ in requests if path.endswith("/renew")}
        assert len(submitted) == 1
        assert {"job-0", "job-1", "job-2"} - submitted <= renewed
        assert runner.snapshot().pending_results == 3
        transport.release_results.set()
        while time.monotonic() < deadline and runner.snapshot().completed < 3:
            time.sleep(0.01)
        assert runner.snapshot().completed == 3
        assert runner.snapshot().failed == 0
    finally:
        transport.release_results.set()
        assert runner.stop(timeout=5.0)
