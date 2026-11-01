from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from worker import accelerator_client as client


def test_reference_worker_rejects_legacy_aligned_artifact_contract(tmp_path):
    with pytest.raises(RuntimeError, match="artifact_contract_unsupported"):
        client._artifact_upload_items(
            SimpleNamespace(
                aligned_page_path=tmp_path / "aligned.png",
                source_mask_path=tmp_path / "mask.png",
            ),
            {
                "contract_version": "aligned-page-v1",
                "aligned_page": {},
                "source_mask": {},
            },
        )


@pytest.mark.parametrize(
    ("contract", "kind", "path_attribute"),
    [
        ("client-evidence-v1", "evidence", "evidence_path"),
        (
            "client-canonical-template-v1",
            "canonical_template",
            "canonical_path",
        ),
        (
            "template-ocr-evidence-v2",
            "template_ocr_evidence",
            "artifact_path",
        ),
    ],
)
def test_reference_worker_maps_each_contract_to_one_artifact_kind(
    tmp_path,
    contract,
    kind,
    path_attribute,
) -> None:
    artifact = tmp_path / f"{kind}.png"
    artifacts = SimpleNamespace(**{path_attribute: artifact})

    assert client._artifact_upload_items(
        artifacts,
        {"contract_version": contract, kind: {}},
    ) == ((kind, artifact),)


def test_reference_worker_maps_ordered_multi_evidence_artifacts(tmp_path) -> None:
    paths = tuple(tmp_path / f"evidence-{position}.png" for position in (1, 2))
    manifest = {
        "contract_version": "client-evidence-multi-v3",
        "evidence_images": [
            {"kind": "evidence_01"},
            {"kind": "evidence_02"},
        ],
    }

    assert client._artifact_upload_items(
        SimpleNamespace(evidence_paths=paths),
        manifest,
    ) == (("evidence_01", paths[0]), ("evidence_02", paths[1]))
    assert client._artifact_manifest_entry(manifest, "evidence_02") == {
        "kind": "evidence_02"
    }


def test_reference_worker_renews_during_long_compute(monkeypatch, tmp_path):
    source_bytes = b"source"
    template_bytes = b"template"
    renewed = threading.Event()
    calls: list[str] = []
    lease_offered = False

    def json_request(_base, path, _node_id, _token, _payload):
        nonlocal lease_offered
        calls.append(path)
        if path.endswith("/heartbeat"):
            return {}
        if path.endswith("/lease"):
            if lease_offered:
                return {"job": None}
            lease_offered = True
            return {"job": {
                "job_id": "job-1",
                "lease_id": "l" * 32,
                "source_url": "/source",
                "template_url": "/template",
                "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
                "template_sha256": hashlib.sha256(template_bytes).hexdigest(),
                "stage": "template_registration",
                "algorithm_version": "template-registration-v2",
                "layout_snapshot": {},
            }}
        if path.endswith("/renew"):
            renewed.set()
            return {"status": "leased"}
        if path.endswith("/result"):
            return {"status": "validated"}
        if path.endswith("/fail"):
            return {"status": "queued"}
        raise AssertionError(path)

    def download(_base, path, _node_id, _token, _lease, target):
        target.write_bytes(source_bytes if path == "/source" else template_bytes)

    def process(*_args, **_kwargs):
        assert renewed.wait(5.0)
        return {"status": "succeeded"}

    evidence = tmp_path / "evidence.png"
    evidence.write_bytes(b"evidence")
    manifest = {
        "contract_version": "client-evidence-v1",
        "evidence": {},
    }

    monkeypatch.setenv("MATHAGENT_ACCELERATOR_NODE_ID", "node-1")
    monkeypatch.setenv("MATHAGENT_ACCELERATOR_TOKEN", "token-1")
    monkeypatch.setattr(client, "_LEASE_RENEW_SECONDS", 0.01)
    monkeypatch.setattr(client, "_json_request", json_request)
    monkeypatch.setattr(client, "_download", download)
    monkeypatch.setattr(client, "_process", process)
    monkeypatch.setattr(
        client,
        "_materialize_result",
        lambda *_args, **_kwargs: (
            {"artifact_manifest": manifest},
            SimpleNamespace(
                evidence_path=Path(evidence),
            ),
        ),
    )
    monkeypatch.setattr(client, "_upload_artifact", lambda *_args, **_kwargs: None)
    args = SimpleNamespace(
        server="https://example.invalid",
        backend="opencv_cpu_reference",
        algorithm_version="template-registration-v2",
        poll_seconds=0.01,
        once=True,
    )

    assert client.run(args) == 0
    assert any(
        path.endswith("/heartbeat")
        for path in calls
    )
    renew_index = next(
        index for index, path in enumerate(calls) if path.endswith("/renew")
    )
    result_index = next(
        index for index, path in enumerate(calls) if path.endswith("/result")
    )
    assert renew_index < result_index


@pytest.mark.parametrize(
    ("stage", "contract", "kind", "path_attribute"),
    [
        (
            "template_canonicalization",
            "client-canonical-template-v1",
            "canonical_template",
            "canonical_path",
        ),
        (
            "template_ocr_evidence",
            "template-ocr-evidence-v2",
            "template_ocr_evidence",
            "artifact_path",
        ),
    ],
)
def test_reference_worker_routes_auxiliary_stage_without_template_or_gpu(
    monkeypatch,
    tmp_path,
    stage,
    contract,
    kind,
    path_attribute,
) -> None:
    source_bytes = b"source"
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    offered = False
    downloads = []
    uploads = []
    results = []

    def json_request(_base, path, _node_id, _token, payload):
        nonlocal offered
        if path.endswith("/heartbeat"):
            return {}
        if path.endswith("/lease"):
            if offered:
                return {"job": None}
            offered = True
            return {"job": {
                "job_id": "job-auxiliary",
                "lease_id": "l" * 32,
                "source_url": "/source",
                "source_sha256": source_sha256,
                "stage": stage,
                "algorithm_version": "template-registration-v2",
            }}
        if path.endswith("/result"):
            results.append(payload["result"])
            return {"status": "validated"}
        if path.endswith("/fail"):
            pytest.fail(f"auxiliary task failed: {payload}")
        raise AssertionError(path)

    def download(_base, path, _node_id, _token, _lease, target):
        downloads.append(path)
        target.write_bytes(source_bytes)

    artifact = tmp_path / f"{kind}.png"
    candidate = {
        "status": "succeeded",
        "artifact_manifest": {
            "contract_version": contract,
            kind: {},
        },
    }
    if stage == "template_ocr_evidence":
        candidate["input_identity"] = {
            "canonical_sha256": source_sha256,
            "layout_snapshot_sha256": "c" * 64,
        }

    def process_auxiliary(source, job, output_dir):
        assert source.name == "source.png"
        assert job["stage"] == stage
        assert output_dir.is_dir()
        return candidate, SimpleNamespace(**{path_attribute: artifact})

    monkeypatch.setenv("MATHAGENT_ACCELERATOR_NODE_ID", "node-1")
    monkeypatch.setenv("MATHAGENT_ACCELERATOR_TOKEN", "token-1")
    monkeypatch.setattr(client, "_json_request", json_request)
    monkeypatch.setattr(client, "_download", download)
    monkeypatch.setattr(client, "_process_auxiliary_stage", process_auxiliary)
    monkeypatch.setattr(
        client,
        "_process",
        lambda *_args: pytest.fail("GPU/registration processor must not run"),
    )
    monkeypatch.setattr(
        client,
        "_upload_artifact",
        lambda *_args, **kwargs: uploads.append(kwargs),
    )

    args = SimpleNamespace(
        server="https://example.invalid",
        backend="opencv_cpu_reference",
        algorithm_version="template-registration-v2",
        poll_seconds=0.01,
        once=True,
    )

    assert client.run(args) == 0
    assert downloads == ["/source"]
    assert [upload["kind"] for upload in uploads] == [kind]
    assert results == [candidate]


def test_reference_worker_rejects_unsupported_stage_before_download(
    monkeypatch,
) -> None:
    calls: list[tuple[str, dict]] = []
    offered = False

    def json_request(_base, path, _node_id, _token, payload):
        nonlocal offered
        calls.append((path, payload))
        if path.endswith("/heartbeat"):
            return {}
        if path.endswith("/lease"):
            if offered:
                return {"job": None}
            offered = True
            return {"job": {
                "job_id": "job-unsupported",
                "lease_id": "l" * 32,
                "stage": "document_render",
                "algorithm_version": "template-registration-v2",
            }}
        if path.endswith("/fail"):
            return {"status": "queued"}
        raise AssertionError(path)

    monkeypatch.setenv("MATHAGENT_ACCELERATOR_NODE_ID", "node-1")
    monkeypatch.setenv("MATHAGENT_ACCELERATOR_TOKEN", "token-1")
    monkeypatch.setattr(client, "_json_request", json_request)
    monkeypatch.setattr(
        client,
        "_download",
        lambda *_args, **_kwargs: pytest.fail("download must not run"),
    )
    args = SimpleNamespace(
        server="https://example.invalid",
        backend="opencv_cpu_reference",
        algorithm_version="template-registration-v2",
        poll_seconds=0.01,
        once=True,
    )

    assert client.run(args) == 1
    heartbeat = next(
        payload for path, payload in calls if path.endswith("/heartbeat")
    )
    assert heartbeat["capabilities"]["stages"] == [
        "template_registration",
        "student_multi_evidence",
        "template_canonicalization",
        "template_ocr_evidence",
    ]
    assert heartbeat["capabilities"]["artifact_contracts"] == [
        "client-evidence-v1",
        "client-evidence-multi-v3",
        "client-evidence-columns-v1",
        "client-canonical-template-v1",
        "template-ocr-evidence-v2",
    ]
    failure = next(payload for path, payload in calls if path.endswith("/fail"))
    assert failure["error_code"] == "unsupported_accelerator_stage"
