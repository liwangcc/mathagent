#!/usr/bin/env python3
"""Bounded six-lease outbound client for one persistent CV-CUDA context."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from worker.cvcuda_batch_processor import CVCudaBatchProcessor
from worker.accelerator_client import (
    _EVIDENCE_STAGES,
    _SUPPORTED_ARTIFACT_CONTRACTS,
    _SUPPORTED_STAGES,
    _artifact_upload_items,
    _artifact_manifest_entry,
    _materialize_result,
    _process_auxiliary_stage,
    _upload_artifact,
    _validate_job_contract,
)

LEASE_SLOTS = 6
DOWNLOAD_SLOTS = 6
GPU_MICROBATCH = 2
UPLOAD_SLOTS = 2
GPU_CONTEXTS = 1
AUXILIARY_SLOTS = 3
LEASE_RENEW_SECONDS = 20.0
LEASE_RENEW_FAILURE_LIMIT = 3
MAX_ASSET_BYTES = 30 * 1024 * 1024

logger = logging.getLogger("mathagent.cvcuda_worker")
_stop_requested = threading.Event()


@dataclass
class LeaseItem:
    job: dict
    root: tempfile.TemporaryDirectory
    source: Path
    template: Path | None
    download_ms: int = 0


def _headers(node_id: str, token: str, *, lease: str | None = None) -> dict[str, str]:
    value = {
        "X-Accelerator-Node": node_id,
        "X-Accelerator-Token": token,
        "User-Agent": "MathAgent-CVCUDA-Accelerator/1",
    }
    if lease:
        value["X-Accelerator-Lease"] = lease
    return value


def _post(base: str, path: str, node_id: str, token: str, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = _headers(node_id, token)
    headers["Content-Type"] = "application/json"
    with urlopen(Request(base.rstrip("/") + path, data=body, headers=headers, method="POST"), timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _download(base: str, path: str, node_id: str, token: str, lease: str, target: Path) -> None:
    size = 0
    try:
        with urlopen(
            Request(
                base.rstrip("/") + path,
                headers=_headers(node_id, token, lease=lease),
            ),
            timeout=60,
        ) as response, target.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_ASSET_BYTES:
                    raise RuntimeError("asset_too_large")
                output.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _lease_one(base: str, node_id: str, token: str) -> dict | None:
    return _post(base, "/api/accelerator/jobs/lease", node_id, token, {}).get("job")


def _heartbeat_payload(
    processor,
    *,
    active: int,
    algorithm_version: str = "template-registration-v2",
    last_error_code: str | None = None,
) -> dict:
    return {
        "capabilities": {
            "backend": processor.backend,
            "stages": list(_SUPPORTED_STAGES),
            "artifact_contracts": list(_SUPPORTED_ARTIFACT_CONTRACTS),
            "batch_size": GPU_MICROBATCH,
            "max_batch_size": GPU_MICROBATCH,
            "lease_slots": LEASE_SLOTS,
            "download_slots": DOWNLOAD_SLOTS,
            "upload_slots": UPLOAD_SLOTS,
            "gpu_contexts": GPU_CONTEXTS,
            "auxiliary_slots": AUXILIARY_SLOTS,
        },
        "algorithm_version": algorithm_version,
        "free_slots": max(0, LEASE_SLOTS - active),
        "queue_depth": active,
        "estimated_finish_ms": None,
        "last_error_code": last_error_code,
    }


class LeaseKeeper:
    def __init__(
        self,
        base: str,
        node_id: str,
        token: str,
        processor,
        algorithm_version: str,
    ):
        self.base = base
        self.node_id = node_id
        self.token = token
        self.processor = processor
        self.algorithm_version = algorithm_version
        self._jobs: dict[str, dict] = {}
        self._renew_failures: dict[str, int] = {}
        self._lost: dict[str, str] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="lease-keeper", daemon=True)
        self._thread.start()

    def add(self, job: dict) -> None:
        with self._lock:
            self._jobs[job["job_id"]] = job
            self._renew_failures.pop(job["job_id"], None)
            self._lost.pop(job["job_id"], None)

    def discard(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)
            self._renew_failures.pop(job_id, None)
            self._lost.pop(job_id, None)

    def ensure_live(self, job_id: str) -> None:
        with self._lock:
            error_code = self._lost.get(job_id)
        if error_code:
            raise RuntimeError(error_code)

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self._jobs.values())

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _renew_succeeded(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._renew_failures.pop(job_id, None)

    def _renew_failed(self, job_id: str, *, permanent_code: str = "") -> None:
        with self._lock:
            if job_id not in self._jobs:
                return
            failures = self._renew_failures.get(job_id, 0) + 1
            self._renew_failures[job_id] = failures
            if permanent_code:
                self._lost[job_id] = permanent_code
            elif failures >= LEASE_RENEW_FAILURE_LIMIT:
                self._lost[job_id] = "lease_renew_failed"

    def _run(self) -> None:
        while not self._stop.wait(LEASE_RENEW_SECONDS):
            jobs = self.snapshot()
            error_code = None
            for job in jobs:
                job_id = job["job_id"]
                try:
                    _post(
                        self.base,
                        f"/api/accelerator/jobs/{job_id}/renew",
                        self.node_id,
                        self.token,
                        {"lease_id": job["lease_id"]},
                    )
                    self._renew_succeeded(job_id)
                except HTTPError as exc:
                    permanent_code = (
                        "node_auth_failed" if exc.code == 401
                        else "lease_rejected" if exc.code == 409
                        else ""
                    )
                    self._renew_failed(job_id, permanent_code=permanent_code)
                    error_code = permanent_code or "lease_renew_http_error"
                    logger.exception("lease renew failed job_id=%s", job_id)
                except Exception as exc:
                    self._renew_failed(job_id)
                    error_code = f"renew_{type(exc).__name__.lower()}"[:64]
                    logger.exception("lease renew failed job_id=%s", job_id)
            try:
                payload = _heartbeat_payload(
                    self.processor,
                    active=len(jobs),
                    algorithm_version=self.algorithm_version,
                    last_error_code=error_code,
                )
                _post(
                    self.base,
                    "/api/accelerator/nodes/heartbeat",
                    self.node_id,
                    self.token,
                    payload,
                )
            except Exception:
                logger.exception("in-flight heartbeat failed active=%d", len(jobs))


def _fail_job(base: str, node_id: str, token: str, job: dict, exc: Exception) -> None:
    error_code = (
        str(exc)[:64]
        if isinstance(exc, RuntimeError) and str(exc)
        else type(exc).__name__.lower()[:64]
    )
    logger.exception("accelerator job failed job_id=%s error_code=%s", job["job_id"], error_code)
    try:
        _post(
            base,
            f"/api/accelerator/jobs/{job['job_id']}/fail",
            node_id,
            token,
            {"lease_id": job["lease_id"], "error_code": error_code},
        )
    except Exception:
        logger.exception("failed to report job failure job_id=%s", job["job_id"])


def _download_item(base: str, node_id: str, token: str, job: dict) -> LeaseItem:
    started = time.monotonic()
    directory = tempfile.TemporaryDirectory(prefix="mathagent-cvcuda-")
    root = Path(directory.name)
    source = root / "source.png"
    template = root / "template.img"
    try:
        _download(base, job["source_url"], node_id, token, job["lease_id"], source)
        if _sha(source) != job["source_sha256"]:
            raise RuntimeError("download_hash_mismatch")
        if job.get("stage") in _EVIDENCE_STAGES:
            _download(
                base,
                job["template_url"],
                node_id,
                token,
                job["lease_id"],
                template,
            )
            if _sha(template) != job["template_sha256"]:
                raise RuntimeError("download_hash_mismatch")
        else:
            template = None
        return LeaseItem(
            job,
            directory,
            source,
            template,
            download_ms=round((time.monotonic() - started) * 1000),
        )
    except Exception:
        directory.cleanup()
        raise


def _complete_prepared(
    base: str,
    node_id: str,
    token: str,
    keeper: LeaseKeeper,
    prepared: list[tuple[LeaseItem, dict, object, int]],
    settled_ids: set[str],
) -> None:
    if not prepared:
        return
    with ThreadPoolExecutor(max_workers=UPLOAD_SLOTS) as executor:
        pending_uploads = {}
        upload_started = {}
        ready = []
        for item, result, artifacts, _processing_ms in prepared:
            job_id = item.job["job_id"]
            try:
                keeper.ensure_live(job_id)
                upload_items = _artifact_upload_items(
                    artifacts,
                    result["artifact_manifest"],
                )
                upload_started[job_id] = time.monotonic()
                pending_uploads[job_id] = [
                    executor.submit(
                        _upload_artifact,
                        base,
                        job_id=job_id,
                        kind=kind,
                        node_id=node_id,
                        token=token,
                        lease=item.job["lease_id"],
                        source=artifact_path,
                        expected=_artifact_manifest_entry(
                            result["artifact_manifest"],
                            kind,
                        ),
                    )
                    for kind, artifact_path in upload_items
                ]
                ready.append((item, result, artifacts, _processing_ms))
            except Exception as exc:
                _fail_job(base, node_id, token, item.job, exc)
                settled_ids.add(job_id)
                keeper.discard(job_id)
        for item, result, _artifacts, processing_ms in ready:
            job_id = item.job["job_id"]
            try:
                for future in pending_uploads[job_id]:
                    future.result()
                keeper.ensure_live(job_id)
                upload_ms = round(
                    (time.monotonic() - upload_started[job_id]) * 1000
                )
                metrics = result.get("worker_metrics")
                if isinstance(metrics, dict):
                    metrics["upload_ms"] = upload_ms
                response = _post(
                    base,
                    f"/api/accelerator/jobs/{job_id}/result",
                    node_id,
                    token,
                    {"lease_id": item.job["lease_id"], "result": result},
                )
                settled_ids.add(job_id)
                logger.info(
                    "job completed job_id=%s stage=%s status=%s download_ms=%d "
                    "processing_ms=%d upload_ms=%d",
                    job_id,
                    item.job.get("stage"),
                    response.get("status", "candidate_received"),
                    item.download_ms,
                    processing_ms,
                    upload_ms,
                )
            except Exception as exc:
                _fail_job(base, node_id, token, item.job, exc)
                settled_ids.add(job_id)
            finally:
                keeper.discard(job_id)


def _prepare_auxiliary_item(
    item: LeaseItem,
    keeper: LeaseKeeper,
) -> tuple[LeaseItem, dict, object, int]:
    job_id = item.job["job_id"]
    keeper.ensure_live(job_id)
    started = time.monotonic()
    result, artifacts = _process_auxiliary_stage(
        item.source,
        item.job,
        Path(item.root.name),
    )
    keeper.ensure_live(job_id)
    return (
        item,
        result,
        artifacts,
        round((time.monotonic() - started) * 1000),
    )


def run(args) -> int:
    node_id = os.environ.get("MATHAGENT_ACCELERATOR_NODE_ID", "").strip()
    token = os.environ.get("MATHAGENT_ACCELERATOR_TOKEN", "").strip()
    if not node_id or not token:
        raise RuntimeError("protected node credential is unavailable")
    processor = CVCudaBatchProcessor(batch_size=GPU_MICROBATCH)
    keeper = LeaseKeeper(
        args.server,
        node_id,
        token,
        processor,
        args.algorithm_version,
    )
    keeper.start()
    try:
        while not _stop_requested.is_set():
            leases: list[LeaseItem] = []
            claimed_jobs: list[dict] = []
            settled_ids: set[str] = set()
            try:
                heartbeat = _heartbeat_payload(
                    processor,
                    active=0,
                    algorithm_version=args.algorithm_version,
                )
                _post(
                    args.server,
                    "/api/accelerator/nodes/heartbeat",
                    node_id,
                    token,
                    heartbeat,
                )
                started = time.monotonic()
                while len(claimed_jobs) < LEASE_SLOTS and (
                    not claimed_jobs
                    or time.monotonic() - started < args.batch_wait_seconds
                ) and not _stop_requested.is_set():
                    job = _lease_one(args.server, node_id, token)
                    if not job:
                        if claimed_jobs:
                            time.sleep(0.05)
                            continue
                        break
                    try:
                        _validate_job_contract(job, args.algorithm_version)
                    except Exception as exc:
                        _fail_job(args.server, node_id, token, job, exc)
                        continue
                    claimed_jobs.append(job)
                    keeper.add(job)
                if not claimed_jobs:
                    if args.once:
                        return 0
                    _stop_requested.wait(args.poll_seconds)
                    continue
                with ThreadPoolExecutor(
                    max_workers=min(DOWNLOAD_SLOTS, len(claimed_jobs))
                ) as executor:
                    futures = [
                        executor.submit(
                            _download_item, args.server, node_id, token, job
                        )
                        for job in claimed_jobs
                    ]
                    for job, future in zip(claimed_jobs, futures):
                        item = None
                        try:
                            item = future.result()
                            keeper.ensure_live(job["job_id"])
                            leases.append(item)
                        except Exception as exc:
                            if item is not None:
                                item.root.cleanup()
                            _fail_job(args.server, node_id, token, job, exc)
                            keeper.discard(job["job_id"])
                            settled_ids.add(job["job_id"])

                registration_leases = [
                    item
                    for item in leases
                    if item.job.get("stage") in _EVIDENCE_STAGES
                ]
                auxiliary_leases = [
                    item
                    for item in leases
                    if item.job.get("stage") not in _EVIDENCE_STAGES
                ]
                with ThreadPoolExecutor(
                    max_workers=min(AUXILIARY_SLOTS, max(1, len(auxiliary_leases)))
                ) as auxiliary_executor:
                    auxiliary_futures = {
                        auxiliary_executor.submit(
                            _prepare_auxiliary_item,
                            item,
                            keeper,
                        ): item
                        for item in auxiliary_leases
                    }
                    for offset in range(0, len(registration_leases), GPU_MICROBATCH):
                        microbatch = registration_leases[
                            offset:offset + GPU_MICROBATCH
                        ]
                        live_microbatch = []
                        for item in microbatch:
                            try:
                                keeper.ensure_live(item.job["job_id"])
                                if item.template is None:
                                    raise RuntimeError("template_asset_missing")
                                live_microbatch.append(item)
                            except Exception as exc:
                                _fail_job(args.server, node_id, token, item.job, exc)
                                keeper.discard(item.job["job_id"])
                                settled_ids.add(item.job["job_id"])
                        microbatch = live_microbatch
                        if not microbatch:
                            continue
                        payloads = [{
                            "source_path": str(item.source),
                            "template_path": str(item.template),
                            "template_sha256": item.job["template_sha256"],
                            "layout_snapshot": item.job["layout_snapshot"],
                            "layout_snapshot_sha256": item.job["layout_snapshot_sha256"],
                        } for item in microbatch]
                        processing_started = time.monotonic()
                        try:
                            results = processor.process_batch(payloads)
                            if len(results) != len(microbatch):
                                raise RuntimeError("gpu_result_count_mismatch")
                        except Exception as exc:
                            if len(microbatch) > 1:
                                results = []
                                for item, payload in zip(microbatch, payloads):
                                    try:
                                        results.append(
                                            processor.process_batch([payload])[0]
                                        )
                                    except Exception as item_exc:
                                        _fail_job(
                                            args.server,
                                            node_id,
                                            token,
                                            item.job,
                                            item_exc,
                                        )
                                        keeper.discard(item.job["job_id"])
                                        settled_ids.add(item.job["job_id"])
                                        results.append(None)
                            else:
                                _fail_job(
                                    args.server,
                                    node_id,
                                    token,
                                    microbatch[0].job,
                                    exc,
                                )
                                keeper.discard(microbatch[0].job["job_id"])
                                settled_ids.add(microbatch[0].job["job_id"])
                                continue
                        processing_ms = round(
                            (time.monotonic() - processing_started) * 1000
                        )
                        prepared = []
                        for item, raw_result in zip(microbatch, results):
                            if raw_result is None:
                                continue
                            try:
                                keeper.ensure_live(item.job["job_id"])
                                raw_result.setdefault("worker_metrics", {}).update({
                                    "download_ms": item.download_ms,
                                    "processing_ms": processing_ms,
                                })
                                result, artifacts = _materialize_result(
                                    item.source,
                                    item.template,
                                    item.job,
                                    raw_result,
                                    Path(item.root.name) / "artifacts",
                                )
                                prepared.append((
                                    item,
                                    result,
                                    artifacts,
                                    processing_ms,
                                ))
                            except Exception as exc:
                                _fail_job(
                                    args.server,
                                    node_id,
                                    token,
                                    item.job,
                                    exc,
                                )
                                keeper.discard(item.job["job_id"])
                                settled_ids.add(item.job["job_id"])
                        _complete_prepared(
                            args.server,
                            node_id,
                            token,
                            keeper,
                            prepared,
                            settled_ids,
                        )

                    auxiliary_prepared = []
                    for future, item in auxiliary_futures.items():
                        try:
                            auxiliary_prepared.append(future.result())
                        except Exception as exc:
                            _fail_job(
                                args.server,
                                node_id,
                                token,
                                item.job,
                                exc,
                            )
                            keeper.discard(item.job["job_id"])
                            settled_ids.add(item.job["job_id"])
                    _complete_prepared(
                        args.server,
                        node_id,
                        token,
                        keeper,
                        auxiliary_prepared,
                        settled_ids,
                    )
                if args.once:
                    return 0
            except Exception as exc:
                for job in claimed_jobs:
                    if job["job_id"] in settled_ids:
                        continue
                    _fail_job(args.server, node_id, token, job, exc)
                    keeper.discard(job["job_id"])
                    settled_ids.add(job["job_id"])
                if args.once:
                    raise
                time.sleep(args.poll_seconds)
            finally:
                for item in leases:
                    item.root.cleanup()
        return 0
    finally:
        keeper.close()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda _signum, _frame: _stop_requested.set())
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--algorithm-version", default="template-registration-v2")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--batch-wait-seconds", type=float, default=0.25)
    parser.add_argument("--once", action="store_true")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
