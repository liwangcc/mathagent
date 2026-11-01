"""Scheduling contracts for the six-lease CV-CUDA opportunity node."""
from __future__ import annotations

import hashlib
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from worker import cvcuda_batch_client as client


class _Root:
    def __init__(self, path: Path):
        self.name = str(path)
        self.cleaned = False

    def cleanup(self):
        self.cleaned = True


def test_six_lease_slots_are_processed_as_three_two_item_microbatches(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("MATHAGENT_ACCELERATOR_NODE_ID", "node-1")
    monkeypatch.setenv("MATHAGENT_ACCELERATOR_TOKEN", "token-1")
    jobs = [
        {
            "job_id": f"job-{index}",
            "lease_id": f"lease-{index}",
            "template_sha256": "a" * 64,
            "source_sha256": "b" * 64,
            "layout_snapshot_sha256": "c" * 64,
            "stage": "template_registration",
            "algorithm_version": "template-registration-v2",
            "layout_snapshot": {
                "page_type_id": "page_1",
                "answer_regions": [{
                    "question_number": "1",
                    "question_type": "solve",
                    "answer_bbox": [100, 100, 900, 900],
                }],
            },
        }
        for index in range(6)
    ]
    pending = list(jobs)
    heartbeats = []
    result_posts = []
    uploads = []
    roots = []
    processor_batches = []
    upload_lock = threading.Lock()
    upload_active = 0
    upload_max_active = 0

    class FakeProcessor:
        backend = "cvcuda_single_context_v2"

        def __init__(self, batch_size):
            assert batch_size == 2

        def process_batch(self, items):
            processor_batches.append(len(items))
            return [
                {
                    "status": "succeeded",
                    "page_type_id": "page_1",
                    "homography": [
                        [1.0, 0.0, 0.0],
                        [0.0, 1.0, 0.0],
                        [0.0, 0.0, 1.0],
                    ],
                    "worker_metrics": {},
                }
                for _item in items
            ]

    def fake_post(_base, path, _node_id, _token, payload):
        if path.endswith("/heartbeat"):
            heartbeats.append(payload)
            return {}
        if path.endswith("/result"):
            job_id = path.split("/")[-2]
            assert sum(
                1 for _args, kwargs in uploads
                if kwargs["job_id"] == job_id
            ) == 1
            result_posts.append((path, payload))
            return {"status": "validated"}
        if path.endswith("/fail"):
            raise AssertionError("no task should fail")
        return {}

    def fake_download(_base, _node_id, _token, job):
        root = _Root(tmp_path / job["job_id"])
        roots.append(root)
        return client.LeaseItem(
            job=job,
            root=root,
            source=tmp_path / f"{job['job_id']}-source.png",
            template=tmp_path / f"{job['job_id']}-template.png",
        )

    def fake_materialize(_source, _template, job, result, _output_dir):
        manifest = {
            "contract_version": "client-evidence-v1",
            "evidence": {"sha256": "d" * 64},
        }
        candidate = {**result, "artifact_manifest": manifest}
        artifacts = SimpleNamespace(
            evidence_path=tmp_path / f"{job['job_id']}-evidence.png",
        )
        return candidate, artifacts

    monkeypatch.setattr(client, "CVCudaBatchProcessor", FakeProcessor)
    monkeypatch.setattr(client, "_post", fake_post)
    monkeypatch.setattr(
        client,
        "_lease_one",
        lambda *_args: pending.pop(0) if pending else None,
    )
    monkeypatch.setattr(client, "_download_item", fake_download)
    monkeypatch.setattr(client, "_materialize_result", fake_materialize)

    def fake_upload(*args, **kwargs):
        nonlocal upload_active, upload_max_active
        with upload_lock:
            upload_active += 1
            upload_max_active = max(upload_max_active, upload_active)
        try:
            time.sleep(0.01)
            with upload_lock:
                uploads.append((args, kwargs))
        finally:
            with upload_lock:
                upload_active -= 1

    monkeypatch.setattr(client, "_upload_artifact", fake_upload)
    args = SimpleNamespace(
        server="https://example.invalid",
        algorithm_version="template-registration-v2",
        poll_seconds=0.01,
        batch_wait_seconds=0.01,
        once=True,
    )

    assert client.run(args) == 0
    assert heartbeats[0]["free_slots"] == 6
    assert heartbeats[0]["capabilities"] == {
        "backend": "cvcuda_single_context_v2",
            "stages": [
                "template_registration",
                "student_multi_evidence",
                "template_canonicalization",
                "template_ocr_evidence",
            ],
        "artifact_contracts": [
            "client-evidence-v1",
            "client-evidence-multi-v3",
            "client-evidence-columns-v1",
            "client-canonical-template-v1",
            "template-ocr-evidence-v2",
        ],
        "batch_size": 2,
        "max_batch_size": 2,
        "lease_slots": 6,
        "download_slots": 6,
        "upload_slots": 2,
        "gpu_contexts": 1,
        "auxiliary_slots": 3,
    }
    assert processor_batches == [2, 2, 2]
    assert len(uploads) == 6
    assert upload_max_active == 2
    assert len(result_posts) == 6
    assert all(
        post[1]["result"]["worker_metrics"]["download_ms"] == 0
        for post in result_posts
    )
    assert all(
        post[1]["result"]["worker_metrics"]["processing_ms"] >= 0
        for post in result_posts
    )
    assert all(
        post[1]["result"]["worker_metrics"]["upload_ms"] >= 0
        for post in result_posts
    )
    assert all(root.cleaned for root in roots)


def test_auxiliary_stages_bypass_cvcuda_microbatch(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MATHAGENT_ACCELERATOR_NODE_ID", "node-1")
    monkeypatch.setenv("MATHAGENT_ACCELERATOR_TOKEN", "token-1")
    jobs = [
        {
            "job_id": "registration",
            "lease_id": "lease-registration",
            "template_sha256": "a" * 64,
            "source_sha256": "b" * 64,
            "layout_snapshot_sha256": "c" * 64,
            "stage": "template_registration",
            "algorithm_version": "template-registration-v2",
            "layout_snapshot": {"page_type_id": "page_1"},
        },
        {
            "job_id": "canonical",
            "lease_id": "lease-canonical",
            "source_sha256": "d" * 64,
            "stage": "template_canonicalization",
            "algorithm_version": "template-registration-v2",
        },
        {
            "job_id": "ocr",
            "lease_id": "lease-ocr",
            "source_sha256": "e" * 64,
            "layout_snapshot_sha256": "f" * 64,
            "stage": "template_ocr_evidence",
            "algorithm_version": "template-registration-v2",
        },
    ]
    pending = list(jobs)
    gpu_job_ids = []
    auxiliary_job_ids = []
    uploads = []
    results = []

    class FakeProcessor:
        backend = "cvcuda_single_context_v2"

        def __init__(self, batch_size):
            assert batch_size == 2

        def process_batch(self, items):
            assert len(items) == 1
            assert items[0]["template_path"].endswith("template.png")
            gpu_job_ids.append("registration")
            return [{
                "status": "succeeded",
                "page_type_id": "page_1",
                "homography": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                "worker_metrics": {},
            }]

    def fake_download(_base, _node_id, _token, job):
        registration = job["stage"] == "template_registration"
        return client.LeaseItem(
            job=job,
            root=_Root(tmp_path / job["job_id"]),
            source=tmp_path / f"{job['job_id']}-source.png",
            template=(tmp_path / "template.png") if registration else None,
        )

    def fake_materialize(_source, _template, job, raw_result, _output_dir):
        return (
            {
                **raw_result,
                "artifact_manifest": {
                    "contract_version": "client-evidence-v1",
                    "evidence": {},
                },
            },
            SimpleNamespace(evidence_path=tmp_path / "evidence.png"),
        )

    def fake_auxiliary(source, job, _output_dir):
        assert source.name.endswith("source.png")
        auxiliary_job_ids.append(job["job_id"])
        if job["stage"] == "template_canonicalization":
            return (
                {
                    "status": "succeeded",
                    "artifact_manifest": {
                        "contract_version": "client-canonical-template-v1",
                        "canonical_template": {},
                    },
                    "canonicalization": {},
                },
                SimpleNamespace(canonical_path=tmp_path / "canonical.png"),
            )
        return (
            {
                "status": "succeeded",
                "artifact_manifest": {
                    "contract_version": "template-ocr-evidence-v2",
                    "template_ocr_evidence": {},
                },
                "input_identity": {
                    "canonical_sha256": job["source_sha256"],
                    "layout_snapshot_sha256": job["layout_snapshot_sha256"],
                },
            },
            SimpleNamespace(artifact_path=tmp_path / "ocr.png"),
        )

    def fake_post(_base, path, _node_id, _token, payload):
        if path.endswith("/heartbeat"):
            return {}
        if path.endswith("/result"):
            results.append((path.split("/")[-2], payload["result"]))
            return {"status": "validated"}
        if path.endswith("/fail"):
            pytest.fail(f"mixed task failed: {payload}")
        raise AssertionError(path)

    monkeypatch.setattr(client, "CVCudaBatchProcessor", FakeProcessor)
    monkeypatch.setattr(client, "_post", fake_post)
    monkeypatch.setattr(
        client,
        "_lease_one",
        lambda *_args: pending.pop(0) if pending else None,
    )
    monkeypatch.setattr(client, "_download_item", fake_download)
    monkeypatch.setattr(client, "_materialize_result", fake_materialize)
    monkeypatch.setattr(client, "_process_auxiliary_stage", fake_auxiliary)
    monkeypatch.setattr(
        client,
        "_upload_artifact",
        lambda *_args, **kwargs: uploads.append(
            (kwargs["job_id"], kwargs["kind"])
        ),
    )
    args = SimpleNamespace(
        server="https://example.invalid",
        algorithm_version="template-registration-v2",
        poll_seconds=0.01,
        batch_wait_seconds=0.01,
        once=True,
    )

    assert client.run(args) == 0
    assert gpu_job_ids == ["registration"]
    assert sorted(auxiliary_job_ids) == ["canonical", "ocr"]
    assert sorted(uploads) == [
        ("canonical", "canonical_template"),
        ("ocr", "template_ocr_evidence"),
        ("registration", "evidence"),
    ]
    assert sorted(job_id for job_id, _result in results) == [
        "canonical",
        "ocr",
        "registration",
    ]


@pytest.mark.parametrize(
    "stage",
    ["template_canonicalization", "template_ocr_evidence"],
)
def test_auxiliary_download_fetches_only_source(stage, monkeypatch) -> None:
    source_bytes = b"canonical-png"
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    downloads = []

    def fake_download(_base, path, _node_id, _token, _lease, target):
        downloads.append(path)
        target.write_bytes(source_bytes)

    monkeypatch.setattr(client, "_download", fake_download)
    item = client._download_item(
        "https://example.invalid",
        "node-1",
        "token-1",
        {
            "job_id": "auxiliary",
            "lease_id": "lease-auxiliary",
            "source_url": "/source",
            "template_url": "/template",
            "source_sha256": source_sha256,
            "template_sha256": source_sha256,
            "stage": stage,
        },
    )
    try:
        assert downloads == ["/source"]
        assert item.template is None
    finally:
        item.root.cleanup()


def test_invalid_upload_contract_does_not_block_prepared_sibling(
    tmp_path,
    monkeypatch,
) -> None:
    posts = []
    uploads = []
    discarded = []

    class Keeper:
        def ensure_live(self, _job_id):
            return None

        def discard(self, job_id):
            discarded.append(job_id)

    def post(_base, path, _node_id, _token, payload):
        posts.append((path, payload))
        return {"status": "validated"}

    monkeypatch.setattr(client, "_post", post)
    monkeypatch.setattr(
        client,
        "_upload_artifact",
        lambda *_args, **kwargs: uploads.append(kwargs["job_id"]),
    )
    good = client.LeaseItem(
        {
            "job_id": "good",
            "lease_id": "lease-good",
            "stage": "template_ocr_evidence",
        },
        _Root(tmp_path / "good"),
        tmp_path / "good-source.png",
        None,
    )
    bad = client.LeaseItem(
        {
            "job_id": "bad",
            "lease_id": "lease-bad",
            "stage": "template_canonicalization",
        },
        _Root(tmp_path / "bad"),
        tmp_path / "bad-source.png",
        None,
    )
    prepared = [
        (
            bad,
            {
                "artifact_manifest": {
                    "contract_version": "unknown-v1",
                },
            },
            SimpleNamespace(),
            1,
        ),
        (
            good,
            {
                "status": "succeeded",
                "artifact_manifest": {
                    "contract_version": "template-ocr-evidence-v2",
                    "template_ocr_evidence": {},
                },
                "input_identity": {},
            },
            SimpleNamespace(artifact_path=tmp_path / "ocr.png"),
            1,
        ),
    ]
    settled = set()

    client._complete_prepared(
        "https://example.invalid",
        "node-1",
        "token-1",
        Keeper(),
        prepared,
        settled,
    )

    assert uploads == ["good"]
    assert any(path.endswith("/bad/fail") for path, _payload in posts)
    assert any(path.endswith("/good/result") for path, _payload in posts)
    assert settled == {"bad", "good"}
    assert sorted(discarded) == ["bad", "good"]


def test_one_download_error_does_not_fail_a_successful_sibling(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MATHAGENT_ACCELERATOR_NODE_ID", "node-1")
    monkeypatch.setenv("MATHAGENT_ACCELERATOR_TOKEN", "token-1")
    jobs = [{
        "job_id": f"job-{index}",
        "lease_id": f"lease-{index}",
        "template_sha256": "a" * 64,
        "source_sha256": "b" * 64,
        "layout_snapshot_sha256": "c" * 64,
        "stage": "template_registration",
        "algorithm_version": "template-registration-v2",
        "layout_snapshot": {"page_type_id": "page_1", "answer_regions": []},
    } for index in range(2)]
    pending = list(jobs)
    failed = []
    completed = []

    class FakeProcessor:
        backend = "cvcuda_single_context_v2"

        def __init__(self, batch_size):
            assert batch_size == 2

        def process_batch(self, items):
            return [{
                "status": "succeeded",
                "page_type_id": "page_1",
                "homography": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                "worker_metrics": {},
            } for _item in items]

    def fake_post(_base, path, _node_id, _token, payload):
        if path.endswith("/heartbeat"):
            return {}
        if path.endswith("/fail"):
            failed.append((path, payload))
            return {"status": "queued"}
        if path.endswith("/result"):
            completed.append(path.split("/")[-2])
            return {"status": "validated"}
        raise AssertionError(path)

    def fake_download(_base, _node, _token, job):
        if job["job_id"] == "job-1":
            raise OSError("disk unavailable")
        return client.LeaseItem(
            job,
            _Root(tmp_path / job["job_id"]),
            tmp_path / "source.png",
            tmp_path / "template.png",
        )

    monkeypatch.setattr(client, "CVCudaBatchProcessor", FakeProcessor)
    monkeypatch.setattr(client, "_post", fake_post)
    monkeypatch.setattr(
        client,
        "_lease_one",
        lambda *_args: pending.pop(0) if pending else None,
    )
    monkeypatch.setattr(client, "_download_item", fake_download)
    monkeypatch.setattr(client, "_upload_artifact", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        client,
        "_materialize_result",
        lambda _source, _template, job, result, _output: (
            {**result, "artifact_manifest": {
                "contract_version": "client-evidence-v1",
                "evidence": {"sha256": "d" * 64},
            }},
            SimpleNamespace(
                evidence_path=tmp_path / f"{job['job_id']}-evidence.png",
            ),
        ),
    )
    args = SimpleNamespace(
        server="https://example.invalid",
        algorithm_version="template-registration-v2",
        poll_seconds=0.01,
        batch_wait_seconds=0.01,
        once=True,
    )

    assert client.run(args) == 0
    assert failed == [(
        "/api/accelerator/jobs/job-1/fail",
        {"lease_id": "lease-1", "error_code": "oserror"},
    )]
    assert completed == ["job-0"]


def test_lease_keeper_renews_and_reports_active_slots(monkeypatch) -> None:
    calls = []
    keeper = client.LeaseKeeper(
        "https://example.invalid",
        "node-1",
        "token-1",
        SimpleNamespace(backend="cvcuda_single_context_v2"),
        "template-registration-v2",
    )
    keeper.add({"job_id": "job-1", "lease_id": "lease-1"})
    monkeypatch.setattr(client, "LEASE_RENEW_SECONDS", 0.01)
    monkeypatch.setattr(
        client,
        "_post",
        lambda _base, path, _node, _token, payload: calls.append((path, payload)) or {},
    )

    keeper.start()
    deadline = time.monotonic() + 1
    while len(calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    keeper.close()

    assert any(path.endswith("/job-1/renew") for path, _payload in calls)
    heartbeat = next(payload for path, payload in calls if path.endswith("/heartbeat"))
    assert heartbeat["free_slots"] == 5
    assert heartbeat["queue_depth"] == 1


def test_unsupported_stage_is_failed_before_download(monkeypatch) -> None:
    monkeypatch.setenv("MATHAGENT_ACCELERATOR_NODE_ID", "node-1")
    monkeypatch.setenv("MATHAGENT_ACCELERATOR_TOKEN", "token-1")
    pending = [{
        "job_id": "job-unsupported",
        "lease_id": "lease-unsupported",
        "stage": "document_render",
        "algorithm_version": "template-registration-v2",
    }]
    failures = []

    class FakeProcessor:
        backend = "cvcuda_single_context_v2"

        def __init__(self, batch_size):
            assert batch_size == 2

        def process_batch(self, _items):
            pytest.fail("GPU processing must not run")

    def fake_post(_base, path, _node_id, _token, payload):
        if path.endswith("/heartbeat"):
            return {}
        if path.endswith("/fail"):
            failures.append(payload)
            return {"status": "queued"}
        raise AssertionError(path)

    monkeypatch.setattr(client, "CVCudaBatchProcessor", FakeProcessor)
    monkeypatch.setattr(client, "_post", fake_post)
    monkeypatch.setattr(
        client,
        "_lease_one",
        lambda *_args: pending.pop(0) if pending else None,
    )
    monkeypatch.setattr(
        client,
        "_download_item",
        lambda *_args: pytest.fail("download must not run"),
    )
    args = SimpleNamespace(
        server="https://example.invalid",
        algorithm_version="template-registration-v2",
        poll_seconds=0.01,
        batch_wait_seconds=0.01,
        once=True,
    )

    assert client.run(args) == 0
    assert failures == [{
        "lease_id": "lease-unsupported",
        "error_code": "unsupported_accelerator_stage",
    }]


def test_download_rejects_oversized_response_without_partial_file(
    tmp_path, monkeypatch
) -> None:
    class Response:
        def __init__(self):
            self._chunks = [b"1234", b"5", b""]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size):
            return self._chunks.pop(0)

    target = tmp_path / "asset.bin"
    monkeypatch.setattr(client, "MAX_ASSET_BYTES", 4)
    monkeypatch.setattr(client, "urlopen", lambda *_args, **_kwargs: Response())

    with pytest.raises(RuntimeError, match="asset_too_large"):
        client._download(
            "https://example.invalid",
            "/asset",
            "node-1",
            "token-1",
            "lease-1",
            target,
        )

    assert not target.exists()


def test_lease_keeper_marks_server_rejected_lease_as_lost(monkeypatch) -> None:
    keeper = client.LeaseKeeper(
        "https://example.invalid",
        "node-1",
        "token-1",
        SimpleNamespace(backend="cvcuda_single_context_v2"),
        "template-registration-v2",
    )
    keeper.add({"job_id": "job-1", "lease_id": "lease-1"})
    rejected = threading.Event()
    monkeypatch.setattr(client, "LEASE_RENEW_SECONDS", 0.01)

    def reject(_base, path, _node, _token, _payload):
        if path.endswith("/renew"):
            rejected.set()
            raise HTTPError(path, 409, "conflict", {}, None)
        return {}

    monkeypatch.setattr(client, "_post", reject)
    keeper.start()
    try:
        assert rejected.wait(1.0)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                keeper.ensure_live("job-1")
            except RuntimeError as exc:
                assert str(exc) == "lease_rejected"
                break
            time.sleep(0.01)
        else:
            raise AssertionError("rejected lease was not marked lost")
    finally:
        keeper.close()


def test_lease_keeper_allows_one_transient_renew_failure(monkeypatch) -> None:
    keeper = client.LeaseKeeper(
        "https://example.invalid",
        "node-1",
        "token-1",
        SimpleNamespace(backend="cvcuda_single_context_v2"),
        "template-registration-v2",
    )
    keeper.add({"job_id": "job-1", "lease_id": "lease-1"})
    recovered = threading.Event()
    renew_calls = 0
    monkeypatch.setattr(client, "LEASE_RENEW_SECONDS", 0.01)

    def transient(_base, path, _node, _token, _payload):
        nonlocal renew_calls
        if path.endswith("/renew"):
            renew_calls += 1
            if renew_calls == 1:
                raise OSError("temporary outage")
            recovered.set()
        return {}

    monkeypatch.setattr(client, "_post", transient)
    keeper.start()
    try:
        assert recovered.wait(1.0)
        keeper.ensure_live("job-1")
        assert renew_calls >= 2
    finally:
        keeper.close()


def test_stop_requested_drains_claimed_job_without_leasing_another(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MATHAGENT_ACCELERATOR_NODE_ID", "node-1")
    monkeypatch.setenv("MATHAGENT_ACCELERATOR_TOKEN", "token-1")
    client._stop_requested.clear()
    job = {
        "job_id": "job-1",
        "lease_id": "lease-1",
        "template_sha256": "a" * 64,
        "source_sha256": "b" * 64,
        "layout_snapshot_sha256": "c" * 64,
        "stage": "template_registration",
        "algorithm_version": "template-registration-v2",
        "layout_snapshot": {"page_type_id": "page_1", "answer_regions": []},
    }
    lease_calls = 0
    completed = []

    class FakeProcessor:
        backend = "cvcuda_single_context_v2"

        def __init__(self, batch_size):
            assert batch_size == 2

        def process_batch(self, _items):
            return [{
                "status": "succeeded",
                "page_type_id": "page_1",
                "homography": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                "worker_metrics": {},
            }]

    def fake_lease(*_args):
        nonlocal lease_calls
        lease_calls += 1
        if lease_calls == 1:
            client._stop_requested.set()
            return job
        return None

    def fake_download(*_args):
        return client.LeaseItem(
            job,
            _Root(tmp_path),
            tmp_path / "source.png",
            tmp_path / "template.png",
        )

    def fake_post(_base, path, _node, _token, _payload):
        if path.endswith("/result"):
            completed.append(path)
            return {"status": "validated"}
        return {}

    monkeypatch.setattr(client, "CVCudaBatchProcessor", FakeProcessor)
    monkeypatch.setattr(client, "_lease_one", fake_lease)
    monkeypatch.setattr(client, "_download_item", fake_download)
    monkeypatch.setattr(client, "_post", fake_post)
    monkeypatch.setattr(client, "_upload_artifact", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        client,
        "_materialize_result",
        lambda _source, _template, _job, result, _output: (
            {**result, "artifact_manifest": {
                "contract_version": "client-evidence-v1",
                "evidence": {"sha256": "d" * 64},
            }},
            SimpleNamespace(
                evidence_path=tmp_path / "evidence.png",
            ),
        ),
    )
    args = SimpleNamespace(
        server="https://example.invalid",
        algorithm_version="template-registration-v2",
        poll_seconds=0.01,
        batch_wait_seconds=0.01,
        once=False,
    )

    try:
        assert client.run(args) == 0
    finally:
        client._stop_requested.clear()

    assert lease_calls == 1
    assert completed == ["/api/accelerator/jobs/job-1/result"]
