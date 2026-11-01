#!/usr/bin/env python
"""Opportunity-based MathAgent preprocessing node client.

Only outbound HTTP(S) is used. Credentials come from the environment for this
reference runner; a production service must load them from protected storage.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

_MAX_ARTIFACT_BYTES = 30 * 1024 * 1024
_MAX_ASSET_BYTES = 30 * 1024 * 1024
_LEASE_RENEW_SECONDS = 15.0
_LEASE_RENEW_RETRY_SECONDS = 2.0
_LEASE_RENEW_FAILURE_LIMIT = 3
_REGISTRATION_STAGE = "template_registration"
_STUDENT_MULTI_EVIDENCE_STAGE = "student_multi_evidence"
_CANONICALIZATION_STAGE = "template_canonicalization"
_TEMPLATE_OCR_EVIDENCE_STAGE = "template_ocr_evidence"
_EVIDENCE_STAGES = (
    _REGISTRATION_STAGE,
    _STUDENT_MULTI_EVIDENCE_STAGE,
)
_SUPPORTED_STAGES = (
    _REGISTRATION_STAGE,
    _STUDENT_MULTI_EVIDENCE_STAGE,
    _CANONICALIZATION_STAGE,
    _TEMPLATE_OCR_EVIDENCE_STAGE,
)
_CLIENT_EVIDENCE_CONTRACT_VERSION = "client-evidence-v1"
_CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION = "client-evidence-multi-v3"
_CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION = "client-evidence-columns-v1"
_CANONICAL_TEMPLATE_CONTRACT_VERSION = "client-canonical-template-v1"
_TEMPLATE_OCR_EVIDENCE_CONTRACT_VERSION = "template-ocr-evidence-v2"
_SUPPORTED_ARTIFACT_CONTRACTS = (
    _CLIENT_EVIDENCE_CONTRACT_VERSION,
    _CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION,
    _CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION,
    _CANONICAL_TEMPLATE_CONTRACT_VERSION,
    _TEMPLATE_OCR_EVIDENCE_CONTRACT_VERSION,
)
_CANONICAL_COMPUTE_SLOT = threading.BoundedSemaphore(1)


def _headers(node_id: str, token: str, *, json_body: bool = False, lease: str | None = None) -> dict[str, str]:
    headers = {
        "X-Accelerator-Node": node_id,
        "X-Accelerator-Token": token,
        "User-Agent": "MathAgent-Accelerator/1",
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    if lease:
        headers["X-Accelerator-Lease"] = lease
    return headers


def _json_request(base: str, path: str, node_id: str, token: str, payload: dict) -> dict:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        base.rstrip("/") + path,
        data=raw,
        headers=_headers(node_id, token, json_body=True),
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


class _LeaseGuard:
    def __init__(
        self,
        base: str,
        node_id: str,
        token: str,
        job_id: str,
        lease: str,
    ):
        self._base = base
        self._node_id = node_id
        self._token = token
        self._job_id = job_id
        self._lease = lease
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._error_code: str | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name=f"accelerator-lease-{self._job_id[:12]}",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        self._thread = None

    def ensure_live(self) -> None:
        with self._lock:
            error_code = self._error_code
        if error_code:
            raise RuntimeError(error_code)

    def _set_error(self, error_code: str) -> None:
        with self._lock:
            self._error_code = error_code

    def _run(self) -> None:
        delay = _LEASE_RENEW_SECONDS
        failures = 0
        while not self._stop.wait(delay):
            try:
                _json_request(
                    self._base,
                    f"/api/accelerator/jobs/{self._job_id}/renew",
                    self._node_id,
                    self._token,
                    {"lease_id": self._lease},
                )
            except HTTPError as exc:
                failures += 1
                if exc.code in {401, 409}:
                    self._set_error(
                        "node_auth_failed" if exc.code == 401 else "lease_rejected"
                    )
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


def _download(base: str, path: str, node_id: str, token: str, lease: str, target: Path) -> None:
    request = Request(
        base.rstrip("/") + path,
        headers=_headers(node_id, token, lease=lease),
        method="GET",
    )
    size = 0
    try:
        with urlopen(request, timeout=60) as response, target.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > _MAX_ASSET_BYTES:
                    raise RuntimeError("asset_too_large")
                output.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise


def _upload_artifact(
    base: str,
    *,
    job_id: str,
    kind: str,
    node_id: str,
    token: str,
    lease: str,
    source: Path,
    expected: dict,
) -> None:
    raw = source.read_bytes()
    if not raw or len(raw) > _MAX_ARTIFACT_BYTES:
        raise RuntimeError("artifact_size_invalid")
    if len(raw) != expected.get("byte_size") or hashlib.sha256(raw).hexdigest() != expected.get("sha256"):
        raise RuntimeError("artifact_local_hash_mismatch")
    request = Request(
        base.rstrip("/") + f"/api/accelerator/jobs/{job_id}/artifacts/{kind}",
        data=raw,
        headers={
            **_headers(node_id, token, lease=lease),
            "Content-Type": "image/png",
            "Content-Length": str(len(raw)),
        },
        method="PUT",
    )
    with urlopen(request, timeout=60) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict) or value.get("job_id") != job_id or value.get("kind") != kind:
        raise RuntimeError("artifact_upload_response_invalid")
    for field in ("sha256", "byte_size", "width", "height", "channels", "format"):
        if value.get(field) != expected.get(field):
            raise RuntimeError("artifact_upload_mismatch")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_job_contract(job: dict, algorithm_version: str) -> None:
    if job.get("stage") not in _SUPPORTED_STAGES:
        raise RuntimeError("unsupported_accelerator_stage")
    if job.get("algorithm_version") != algorithm_version:
        raise RuntimeError("algorithm_version_mismatch")


def _canonical_candidate(result) -> dict:
    metadata = dict(result.canonicalization)
    try:
        byte_size = result.canonical_path.stat().st_size
        sha256 = str(metadata["canonical_sha256"])
        width = int(metadata["canonical_width"])
        height = int(metadata["canonical_height"])
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise RuntimeError("canonical_artifact_invalid") from exc
    if byte_size <= 0 or byte_size > _MAX_ARTIFACT_BYTES:
        raise RuntimeError("canonical_artifact_invalid")
    return {
        "status": "succeeded",
        "artifact_manifest": {
            "contract_version": _CANONICAL_TEMPLATE_CONTRACT_VERSION,
            "canonical_template": {
                "sha256": sha256,
                "byte_size": byte_size,
                "width": width,
                "height": height,
                "channels": 3,
                "format": "png",
            },
        },
        "canonicalization": metadata,
    }


def _process_auxiliary_stage(
    source: Path,
    job: dict,
    output_dir: Path,
):
    stage = job.get("stage")
    if stage == _CANONICALIZATION_STAGE:
        from desktop.worker.mathagent_worker.canonicalization import (
            TemplateCanonicalizationError,
            canonicalize_template,
        )

        try:
            with _CANONICAL_COMPUTE_SLOT:
                result = canonicalize_template(
                    output_dir,
                    source,
                    output_dir / "canonical.png",
                )
        except TemplateCanonicalizationError as exc:
            raise RuntimeError(exc.code) from exc
        return _canonical_candidate(result), result
    if stage == _TEMPLATE_OCR_EVIDENCE_STAGE:
        from desktop.worker.mathagent_worker.template_ocr_evidence import (
            render_template_ocr_evidence,
        )

        result = render_template_ocr_evidence(
            source,
            job,
            output_dir / "template-ocr-evidence.png",
        )
        return result.candidate, result
    raise RuntimeError("unsupported_accelerator_stage")


def _process_cpu(source: Path, template: Path, layout_snapshot: dict) -> dict:
    """Fit one raw-source-pixel to raw-template-pixel OpenCV candidate."""

    import cv2
    import numpy as np

    from services.accelerator_artifacts import decode_raw_source, decode_raw_template

    page_type_id = str(layout_snapshot.get("page_type_id") or "").strip()
    question_numbers = layout_snapshot.get("question_numbers")
    if not page_type_id or not isinstance(question_numbers, list) or not question_numbers:
        raise RuntimeError("layout_snapshot_invalid")
    source_image = decode_raw_source(source)
    template_image = decode_raw_template(template)

    def extract(image):
        height, width = image.shape[:2]
        scale = min(1.0, 1280.0 / float(max(height, width)))
        out_width = max(1, round(width * scale))
        out_height = max(1, round(height * scale))
        working = image
        if (out_width, out_height) != (width, height):
            working = cv2.resize(
                image, (out_width, out_height), interpolation=cv2.INTER_AREA
            )
        gray = cv2.cvtColor(working, cv2.COLOR_BGR2GRAY)
        cv2.setNumThreads(1)
        points, descriptors = cv2.SIFT_create(
            nfeatures=3000,
            contrastThreshold=0.03,
        ).detectAndCompute(gray, None)
        if descriptors is None or len(points) < 2:
            raise RuntimeError("sift_descriptors_missing")
        inverse = 1.0 / scale if scale else 1.0
        coordinates = np.float32([
            [point.pt[0] * inverse, point.pt[1] * inverse]
            for point in points
        ])
        return coordinates, descriptors.astype(np.float32, copy=False)

    source_points, source_descriptors = extract(source_image)
    target_points, target_descriptors = extract(template_image)
    pairs = cv2.BFMatcher(cv2.NORM_L2, crossCheck=False).knnMatch(
        source_descriptors, target_descriptors, k=2
    )
    good = [
        pair[0]
        for pair in pairs
        if len(pair) == 2 and pair[0].distance < 0.58 * pair[1].distance
    ]
    if len(good) < 25:
        raise RuntimeError("too_few_matches")
    source_array = np.float32([
        source_points[item.queryIdx] for item in good
    ]).reshape(-1, 1, 2)
    target_array = np.float32([
        target_points[item.trainIdx] for item in good
    ]).reshape(-1, 1, 2)
    method = getattr(cv2, "USAC_ACCURATE", cv2.RANSAC)
    matrix, raw_mask = cv2.findHomography(
        source_array, target_array, method, 7.0
    )
    if matrix is None or raw_mask is None or not np.isfinite(matrix).all():
        raise RuntimeError("homography_failed")
    if abs(float(matrix[2, 2])) < 1e-12:
        raise RuntimeError("homography_singular")
    matrix = matrix / matrix[2, 2]
    mask = raw_mask.ravel().astype(bool)
    inliers = int(mask.sum())
    if inliers < 25:
        raise RuntimeError("too_few_inliers")
    projected = cv2.perspectiveTransform(
        source_array.reshape(-1, 2)[mask].reshape(-1, 1, 2), matrix
    ).reshape(-1, 2)
    expected = target_array.reshape(-1, 2)[mask]
    rmse = float(np.sqrt(np.mean(np.sum((projected - expected) ** 2, axis=1))))
    if not math.isfinite(rmse):
        raise RuntimeError("reprojection_non_finite")
    return {
        "status": "succeeded",
        "page_type_id": page_type_id,
        "homography": matrix.tolist(),
        "good_matches": len(good),
        "inliers": inliers,
        "inlier_ratio": inliers / len(good),
        "reprojection_rmse": rmse,
        "coverage": 0.0,
        "worker_metrics": {
            "backend": "opencv_cpu_reference",
            "feature_max_edge": 1280,
            "lowe_ratio": 0.58,
            "homography_threshold": 7.0,
        },
    }


def _report_failure(base: str, job: dict, node_id: str, token: str, code: str) -> None:
    _json_request(
        base,
        f"/api/accelerator/jobs/{job['job_id']}/fail",
        node_id,
        token,
        {"lease_id": job["lease_id"], "error_code": code[:64]},
    )


def _process(args, source: Path, template: Path, job: dict) -> dict:
    if args.backend == "cvcuda_single_context_v2":
        from worker.cvcuda_batch_processor import CVCudaBatchProcessor

        processor = getattr(args, "_gpu_processor", None)
        if processor is None:
            processor = CVCudaBatchProcessor(batch_size=2)
            args._gpu_processor = processor
        return processor.process_batch([{
            "source_path": str(source),
            "template_path": str(template),
            "template_sha256": job["template_sha256"],
            "layout_snapshot": job["layout_snapshot"],
            "layout_snapshot_sha256": job["layout_snapshot_sha256"],
        }])[0]
    if args.backend != "opencv_cpu_reference":
        raise RuntimeError("unsupported_accelerator_backend")
    return _process_cpu(source, template, job["layout_snapshot"])


def _materialize_result(
    source: Path,
    template: Path,
    job: dict,
    result: dict,
    output_dir: Path,
):
    import numpy as np

    from services.accelerator_artifacts import (
        candidate_artifact_sha256,
        client_full_column_evidence_manifest,
        client_evidence_manifest,
        client_multi_evidence_manifest,
        decode_raw_template,
        decode_uploaded_aligned_page,
        decode_uploaded_source_mask,
        materialize_global_artifacts,
    )
    from services.template_evidence import (
        build_evidence_from_aligned,
        build_full_column_difference_evidence_from_aligned,
        build_multi_evidence_from_aligned,
    )

    artifacts = materialize_global_artifacts(
        source,
        template,
        result.get("homography"),
        output_dir,
    )
    source_mask = decode_uploaded_source_mask(artifacts.source_mask_path)
    coverage = float(np.count_nonzero(source_mask == 255)) / float(source_mask.size)
    multi_image = job.get("stage") == _STUDENT_MULTI_EVIDENCE_STAGE
    legacy_focus_sheets = multi_image and (
        isinstance(job.get("layout_snapshot"), dict)
        and (
            "_legacy_answer_regions" in job["layout_snapshot"]
            or "answer_regions" in job["layout_snapshot"]
        )
    )
    evidence_builder = (
        build_multi_evidence_from_aligned
        if legacy_focus_sheets
        else (
            build_full_column_difference_evidence_from_aligned
            if multi_image
            else build_evidence_from_aligned
        )
    )
    evidence = evidence_builder(
        decode_uploaded_aligned_page(artifacts.aligned_page_path),
        source_mask,
        decode_raw_template(template)[:, :, ::-1],
        job["layout_snapshot"],
        output_dir / "evidence",
        diagnostics={
            "validation_mode": (
                "trusted_client_evidence_multi_v3"
                if legacy_focus_sheets
                else (
                    "trusted_client_evidence_full_column_v1"
                    if multi_image
                    else "trusted_client_evidence_v1"
                )
            ),
            "source_mask_replayed": False,
            "client_preprocessing_accepted": True,
        },
    )
    evidence_paths = (
        tuple(Path(path) for path in evidence.evidence_paths)
        if evidence.evidence_paths
        else (Path(evidence.evidence_path),)
    )
    manifest = (
        client_multi_evidence_manifest(
            artifacts.manifest,
            list(evidence_paths),
            list(evidence.diagnostics.get("evidence_sheets") or []),
        )
        if legacy_focus_sheets
        else (
            client_full_column_evidence_manifest(
                artifacts.manifest,
                list(evidence_paths),
                [
                    {
                        "sheet_id": column["sheet_id"],
                        "question_numbers": list(
                            column.get("question_numbers") or []
                        ),
                    }
                    for column in evidence.diagnostics.get("evidence_columns") or []
                ],
            )
            if multi_image
            else client_evidence_manifest(artifacts.manifest, evidence_paths[0])
        )
    )
    candidate = dict(result)
    candidate["coverage"] = coverage
    incomplete = [
        str(value)
        for value in evidence.diagnostics.get("incomplete_answer_regions") or []
    ]
    candidate["answer_source_complete"] = not incomplete
    candidate["incomplete_answer_regions"] = [str(value) for value in incomplete]
    candidate["artifact_manifest"] = manifest
    candidate["question_numbers"] = [str(value) for value in evidence.question_numbers]
    candidate["blank_question_numbers"] = [
        str(value) for value in evidence.blank_question_numbers
    ]
    evidence_sheets = (
        list(evidence.diagnostics.get("evidence_sheets") or [])
        if legacy_focus_sheets
        else [
            {
                "sheet_id": column["sheet_id"],
                "question_numbers": list(column.get("question_numbers") or []),
            }
            for column in evidence.diagnostics.get("evidence_columns") or []
        ]
    )
    candidate["evidence_diagnostics"] = (
        {
            "ownership": evidence.diagnostics.get("ownership", ""),
            "answer_source_coverage": evidence.diagnostics.get(
                "answer_source_coverage", []
            ),
            "incomplete_answer_regions": incomplete,
            "evidence_sheets": evidence_sheets,
        }
        if legacy_focus_sheets
        else (
            {
                "ownership": evidence.diagnostics.get("ownership", ""),
                "evidence_sheets": evidence_sheets,
            }
            if multi_image
            else {
            "ownership": evidence.diagnostics.get("ownership", ""),
            "answer_source_coverage": evidence.diagnostics.get(
                "answer_source_coverage", []
            ),
            "incomplete_answer_regions": evidence.diagnostics.get(
                "incomplete_answer_regions", []
            ),
            "evidence_sheets": [],
            }
        )
    )
    candidate["artifact_sha256"] = candidate_artifact_sha256(
        page_type_id=str(candidate["page_type_id"]),
        source_sha256=job["source_sha256"],
        template_sha256=job["template_sha256"],
        layout_snapshot_sha256=job["layout_snapshot_sha256"],
        homography=candidate["homography"],
        artifact_manifest=manifest,
    )
    metrics = dict(candidate.get("worker_metrics") or {})
    metrics["artifact_contract_version"] = manifest[
        "contract_version"
    ]
    metrics["answer_source_coverage"] = evidence.diagnostics.get(
        "answer_source_coverage", []
    )
    candidate["worker_metrics"] = metrics
    from dataclasses import replace

    return candidate, replace(
        artifacts,
        manifest=manifest,
        evidence_path=evidence_paths[0],
        evidence_paths=evidence_paths,
    )


def _artifact_upload_items(artifacts, manifest: dict) -> tuple[tuple[str, Path], ...]:
    contract = manifest.get("contract_version")
    if contract in {
        _CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION,
        _CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION,
    }:
        evidence_paths = tuple(getattr(artifacts, "evidence_paths", ()) or ())
        images = manifest.get("evidence_images")
        if (
            not isinstance(images, list)
            or len(images) != len(evidence_paths)
            or not evidence_paths
        ):
            raise RuntimeError("evidence_artifact_missing")
        return tuple(
            (str(image["kind"]), Path(path))
            for image, path in zip(images, evidence_paths)
        )
    if contract == _CLIENT_EVIDENCE_CONTRACT_VERSION:
        evidence_path = getattr(artifacts, "evidence_path", None)
        if evidence_path is None or "evidence" not in manifest:
            raise RuntimeError("evidence_artifact_missing")
        return (("evidence", Path(evidence_path)),)
    if contract == _CANONICAL_TEMPLATE_CONTRACT_VERSION:
        canonical_path = getattr(artifacts, "canonical_path", None)
        if canonical_path is None or "canonical_template" not in manifest:
            raise RuntimeError("canonical_artifact_missing")
        return (("canonical_template", Path(canonical_path)),)
    if contract == _TEMPLATE_OCR_EVIDENCE_CONTRACT_VERSION:
        artifact_path = getattr(artifacts, "artifact_path", None)
        if artifact_path is None or "template_ocr_evidence" not in manifest:
            raise RuntimeError("template_ocr_evidence_artifact_missing")
        return (("template_ocr_evidence", Path(artifact_path)),)
    raise RuntimeError("artifact_contract_unsupported")


def _artifact_manifest_entry(manifest: dict, kind: str) -> dict:
    if manifest.get("contract_version") in {
        _CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION,
        _CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION,
    }:
        return next(
            item
            for item in manifest.get("evidence_images") or []
            if item.get("kind") == kind
        )
    return manifest[kind]


def run(args) -> int:
    node_id = os.environ.get("MATHAGENT_ACCELERATOR_NODE_ID", "").strip()
    token = os.environ.get("MATHAGENT_ACCELERATOR_TOKEN", "").strip()
    if not node_id or not token:
        print("Missing MATHAGENT_ACCELERATOR_NODE_ID or MATHAGENT_ACCELERATOR_TOKEN", file=sys.stderr)
        return 2
    while True:
        job = None
        lease_guard = None
        try:
            _json_request(args.server, "/api/accelerator/nodes/heartbeat", node_id, token, {
                "capabilities": {
                    "backend": args.backend,
                    "stages": list(_SUPPORTED_STAGES),
                    "artifact_contracts": list(_SUPPORTED_ARTIFACT_CONTRACTS),
                    "batch_size": 1,
                    "max_batch_size": 2,
                    "lease_slots": 1,
                },
                "algorithm_version": args.algorithm_version,
                "free_slots": 1,
                "queue_depth": 0,
                "estimated_finish_ms": None,
            })
            response = _json_request(args.server, "/api/accelerator/jobs/lease", node_id, token, {})
            job = response.get("job")
            if not job:
                if args.once:
                    return 0
                time.sleep(args.poll_seconds)
                continue
            _validate_job_contract(job, args.algorithm_version)
            lease = job["lease_id"]
            lease_guard = _LeaseGuard(
                args.server,
                node_id,
                token,
                job["job_id"],
                lease,
            )
            lease_guard.start()
            with tempfile.TemporaryDirectory(prefix="mathagent-accelerator-") as directory:
                root = Path(directory)
                source = root / "source.png"
                _download(args.server, job["source_url"], node_id, token, lease, source)
                if _sha256(source) != job["source_sha256"]:
                    raise RuntimeError("download_hash_mismatch")
                lease_guard.ensure_live()
                if job["stage"] in _EVIDENCE_STAGES:
                    template = root / "template.img"
                    _download(
                        args.server,
                        job["template_url"],
                        node_id,
                        token,
                        lease,
                        template,
                    )
                    if _sha256(template) != job["template_sha256"]:
                        raise RuntimeError("download_hash_mismatch")
                    lease_guard.ensure_live()
                    result, artifacts = _materialize_result(
                        source,
                        template,
                        job,
                        _process(args, source, template, job),
                        root / "artifacts",
                    )
                else:
                    result, artifacts = _process_auxiliary_stage(
                        source,
                        job,
                        root,
                    )
                lease_guard.ensure_live()
                for kind, artifact_path in _artifact_upload_items(
                    artifacts,
                    result["artifact_manifest"],
                ):
                    _upload_artifact(
                        args.server,
                        job_id=job["job_id"],
                        kind=kind,
                        node_id=node_id,
                        token=token,
                        lease=lease,
                        source=artifact_path,
                        expected=_artifact_manifest_entry(
                            result["artifact_manifest"],
                            kind,
                        ),
                    )
                    lease_guard.ensure_live()
                lease_guard.close()
                response = _json_request(
                    args.server,
                    f"/api/accelerator/jobs/{job['job_id']}/result",
                    node_id,
                    token,
                    {"lease_id": lease, "result": result},
                )
                print(json.dumps({"job_id": job["job_id"], "status": response["status"]}, ensure_ascii=False))
            if args.once:
                return 0
        except (HTTPError, URLError, TimeoutError, RuntimeError, ValueError, KeyError, StopIteration) as exc:
            code = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__.lower()
            if job and job.get("lease_id"):
                try:
                    _report_failure(args.server, job, node_id, token, code)
                except Exception:
                    pass
            print(f"accelerator error: {type(exc).__name__}: {exc}", file=sys.stderr)
            if args.once:
                return 1
            time.sleep(args.poll_seconds)
        finally:
            if lease_guard is not None:
                lease_guard.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True, help="MathAgent HTTPS base URL")
    parser.add_argument("--algorithm-version", default="template-registration-v2")
    parser.add_argument(
        "--backend",
        choices=("opencv_cpu_reference", "cvcuda_single_context_v2"),
        default="opencv_cpu_reference",
    )
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
