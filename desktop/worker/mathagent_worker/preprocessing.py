from __future__ import annotations

import json
import math
import shutil
import struct
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from .gpu_backend import (
    CPU_BACKEND,
    GPU_BACKEND,
    GPU_BACKEND_VERSION,
    GpuSelfTestResult,
    run_gpu_self_test,
)

ALGORITHM_VERSION = "template-registration-v2"
FEATURE_MAX_EDGE = 1280
SIFT_MAX_FEATURES = 3000
SIFT_CONTRAST_THRESHOLD = 0.03
LOWE_RATIO = 0.58
RANSAC_REPROJECTION_THRESHOLD = 7.0
GPU_OPERATION_TIMEOUT_SECONDS = 20.0
MIN_GOOD_MATCHES = 25
MAX_DECODE_PIXELS = 16_000_000

_GPU_QUEUE_LOCK = threading.Lock()
_GPU_TEMPLATE_CACHE_LOCK = threading.Lock()
_CPU_TEMPLATE_CACHE_LOCK = threading.Lock()
_GPU_SELF_TEST_CACHE_LOCK = threading.Lock()
_GPU_TEMPLATE_CACHE: dict[tuple[str, str], tuple[Any, Any, Any]] = {}
_CPU_TEMPLATE_CACHE: dict[tuple[str, str], tuple[Any, Any, Any]] = {}
_GPU_SELF_TEST_CACHE: GpuSelfTestResult | None = None
_FEATURE_RECORD = struct.Struct("<ffff128s")


class GpuCandidateRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class ProcessingOutcome:
    candidate: dict[str, Any]
    backend: str
    fallback_reason: str | None
    elapsed_ms: float
    aligned_page_path: Path
    source_mask_path: Path
    evidence_path: Path | None = None
    evidence_paths: tuple[Path, ...] = ()


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _validate_identity(job: dict[str, Any]) -> tuple[str, str, str, str]:
    if job.get("stage") not in {"template_registration", "student_multi_evidence"}:
        raise RuntimeError("unsupported_accelerator_stage")
    if job.get("algorithm_version") != ALGORITHM_VERSION:
        raise RuntimeError("algorithm_version_mismatch")
    layout = job.get("layout_snapshot")
    if not isinstance(layout, dict):
        raise RuntimeError("layout_snapshot_invalid")
    page_type_id = str(layout.get("page_type_id") or "").strip()
    layout_sha = str(job.get("layout_snapshot_sha256") or "")
    source_sha = str(job.get("source_sha256") or "")
    template_sha = str(job.get("template_sha256") or "")
    if (
        not page_type_id
        or any(len(value) != 64 for value in (layout_sha, source_sha, template_sha))
        or any(
            character not in "0123456789abcdef"
            for value in (layout_sha, source_sha, template_sha)
            for character in value
        )
    ):
        raise RuntimeError("job_identity_invalid")
    return page_type_id, layout_sha, source_sha, template_sha


def _validated_encoded_dimensions(path: Path) -> tuple[int, int]:
    try:
        with Image.open(path) as encoded:
            width, height = encoded.size
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise RuntimeError("image_decode_failed") from exc
    if height < 32 or width < 32 or width * height > MAX_DECODE_PIXELS:
        raise RuntimeError("image_dimensions_invalid")
    return height, width


def _safe_decode(path: Path):
    import cv2

    expected_shape = _validated_encoded_dimensions(path)
    image = cv2.imread(
        str(path),
        cv2.IMREAD_COLOR | getattr(cv2, "IMREAD_IGNORE_ORIENTATION", 0),
    )
    if image is None or image.ndim != 3 or image.shape[2] != 3:
        raise RuntimeError("image_decode_failed")
    height, width = image.shape[:2]
    if (
        height < 32
        or width < 32
        or height * width > MAX_DECODE_PIXELS
        or (height, width) != expected_shape
    ):
        raise RuntimeError("image_dimensions_invalid")
    return image


def _resize_gray(image):
    import cv2

    height, width = image.shape[:2]
    scale = min(1.0, FEATURE_MAX_EDGE / float(max(height, width)))
    out_w = max(1, round(width * scale))
    out_h = max(1, round(height * scale))
    if (out_w, out_h) != (width, height):
        image = cv2.resize(image, (out_w, out_h), interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), scale


def _decode_gpu_features(raw: bytes, *, inverse_scale: float):
    import cv2
    import numpy as np

    if len(raw) < 4:
        raise RuntimeError("gpu_feature_output_invalid")
    count = struct.unpack_from("<I", raw, 0)[0]
    if count <= 0 or count > 100_000 or len(raw) != 4 + count * _FEATURE_RECORD.size:
        raise RuntimeError("gpu_feature_output_invalid")
    points: list[Any] = []
    descriptors = np.empty((count, 128), dtype=np.float32)
    for index in range(count):
        x, y, sigma, orientation, descriptor = _FEATURE_RECORD.unpack_from(
            raw, 4 + index * _FEATURE_RECORD.size
        )
        values = (x, y, sigma, orientation)
        if not all(math.isfinite(value) for value in values):
            raise RuntimeError("gpu_feature_non_finite")
        points.append(
            cv2.KeyPoint(
                x * inverse_scale,
                y * inverse_scale,
                max(sigma * 2.0 * inverse_scale, 0.01),
                orientation,
            )
        )
        descriptors[index] = [value for value in descriptor]
    return points, descriptors


def _extract_gpu(path: Path, _output_root: Path):
    image = _safe_decode(path)
    gray, scale = _resize_gray(image)
    inverse_scale = 1.0 / scale if scale else 1.0
    with _GPU_QUEUE_LOCK:
        from .vksift_resident import extract_resident

        raw, elapsed_ms = extract_resident(
            gray,
            timeout_seconds=GPU_OPERATION_TIMEOUT_SECONDS,
        )
        points, descriptors = _decode_gpu_features(
            raw,
            inverse_scale=inverse_scale,
        )
        return image.shape[:2], points, descriptors, elapsed_ms, "resident"


def _extract_cpu(path: Path):
    import cv2

    image = _safe_decode(path)
    gray, scale = _resize_gray(image)
    started = time.perf_counter()
    cv2.setNumThreads(1)
    sift = cv2.SIFT_create(
        nfeatures=SIFT_MAX_FEATURES,
        contrastThreshold=SIFT_CONTRAST_THRESHOLD,
    )
    points, descriptors = sift.detectAndCompute(gray, None)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if descriptors is None or len(points) < 2:
        raise RuntimeError("sift_descriptors_missing")
    inverse = 1.0 / scale if scale else 1.0
    if inverse != 1.0:
        points = [
            cv2.KeyPoint(
                point.pt[0] * inverse,
                point.pt[1] * inverse,
                max(point.size * inverse, 0.01),
                point.angle,
                point.response,
                point.octave,
                point.class_id,
            )
            for point in points
        ]
    return image.shape[:2], points, descriptors, elapsed_ms


def _coverage(source_shape, template_shape, homography) -> float:
    import cv2
    import numpy as np

    source_mask = np.ones(source_shape, dtype=np.uint8)
    height, width = template_shape
    warped = cv2.warpPerspective(
        source_mask,
        homography,
        (width, height),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    return float(np.count_nonzero(warped)) / float(warped.size)


def _fit_candidate(source, template, job, *, backend: str, processing_ms: float):
    import cv2
    import numpy as np

    source_shape, source_points, source_desc = source
    template_shape, target_points, target_desc = template
    if len(source_desc) < 2 or len(target_desc) < 2:
        raise RuntimeError("sift_descriptors_missing")
    pairs = cv2.BFMatcher(cv2.NORM_L2, crossCheck=False).knnMatch(
        source_desc.astype(np.float32, copy=False),
        target_desc.astype(np.float32, copy=False),
        k=2,
    )
    good = [
        pair[0]
        for pair in pairs
        if len(pair) == 2 and pair[0].distance < LOWE_RATIO * pair[1].distance
    ]
    if len(good) < MIN_GOOD_MATCHES:
        raise RuntimeError("too_few_matches")
    source_array = np.float32([source_points[item.queryIdx].pt for item in good]).reshape(-1, 1, 2)
    target_array = np.float32([target_points[item.trainIdx].pt for item in good]).reshape(-1, 1, 2)
    method = getattr(cv2, "USAC_ACCURATE", cv2.RANSAC)
    threshold = RANSAC_REPROJECTION_THRESHOLD
    matrix, raw_mask = cv2.findHomography(source_array, target_array, method, threshold)
    if matrix is None or raw_mask is None or not np.isfinite(matrix).all():
        raise RuntimeError("homography_failed")
    if abs(float(matrix[2, 2])) < 1e-12:
        raise RuntimeError("homography_singular")
    matrix = matrix / matrix[2, 2]
    mask = raw_mask.ravel().astype(bool)
    inliers = int(mask.sum())
    if inliers < MIN_GOOD_MATCHES:
        raise RuntimeError("too_few_inliers")
    projected = cv2.perspectiveTransform(
        source_array.reshape(-1, 2)[mask].reshape(-1, 1, 2), matrix
    ).reshape(-1, 2)
    expected = target_array.reshape(-1, 2)[mask]
    rmse = float(np.sqrt(np.mean(np.sum((projected - expected) ** 2, axis=1))))
    coverage = _coverage(source_shape, template_shape, matrix)
    page_type_id, _layout_sha, _source_sha, _template_sha = _validate_identity(job)
    homography = [[float(value) for value in row] for row in matrix.tolist()]
    candidate = {
        "status": "succeeded",
        "page_type_id": page_type_id,
        "homography": homography,
        "good_matches": len(good),
        "inliers": inliers,
        "inlier_ratio": inliers / len(good),
        "reprojection_rmse": rmse,
        "coverage": coverage,
        "answer_source_complete": False,
        "incomplete_answer_regions": ["artifact_not_materialized"],
        "worker_metrics": {
            "backend": backend,
            "backend_version": GPU_BACKEND_VERSION if backend == GPU_BACKEND else "opencv-cpu-v1",
            "feature_max_edge": FEATURE_MAX_EDGE,
            "max_features": SIFT_MAX_FEATURES,
            "lowe_ratio": LOWE_RATIO,
            "homography_method": "usac_accurate" if method != cv2.RANSAC else "ransac",
            "homography_threshold": threshold,
            "processing_time_ms": round(processing_ms, 3),
        },
    }
    _canonical(candidate)
    return candidate


def _gpu_candidate(source_path: Path, template_path: Path, job: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="mathagent-gpu-candidate-") as directory:
        root = Path(directory)
        (
            source_shape,
            source_points,
            source_desc,
            source_ms,
            source_runtime,
        ) = _extract_gpu(source_path, root)
        _page_type, _layout_sha, _source_sha, template_sha = _validate_identity(job)
        template_cache_key = (ALGORITHM_VERSION, template_sha)
        template_cache_hit = False
        with _GPU_TEMPLATE_CACHE_LOCK:
            cached = _GPU_TEMPLATE_CACHE.get(template_cache_key)
            if cached is None:
                (
                    template_shape,
                    target_points,
                    target_desc,
                    template_ms,
                    template_runtime,
                ) = _extract_gpu(template_path, root)
                _GPU_TEMPLATE_CACHE.clear()
                _GPU_TEMPLATE_CACHE[template_cache_key] = (
                    template_shape,
                    target_points,
                    target_desc,
                )
            else:
                template_shape, target_points, target_desc = cached
                template_ms = 0.0
                template_runtime = "feature_cache"
                template_cache_hit = True
        candidate = _fit_candidate(
            (source_shape, source_points, source_desc),
            (template_shape, target_points, target_desc),
            job,
            backend=GPU_BACKEND,
            processing_ms=source_ms + template_ms,
        )
        candidate["worker_metrics"].update({
            "template_cache_hit": template_cache_hit,
            "source_feature_time_ms": round(source_ms, 3),
            "template_feature_time_ms": round(template_ms, 3),
            "source_gpu_runtime": source_runtime,
            "template_gpu_runtime": template_runtime,
        })
        return candidate


def _cpu_candidate(source_path: Path, template_path: Path, job: dict[str, Any]) -> dict[str, Any]:
    source_shape, source_points, source_desc, source_ms = _extract_cpu(source_path)
    _page_type, _layout_sha, _source_sha, template_sha = _validate_identity(job)
    template_cache_key = (ALGORITHM_VERSION, template_sha)
    template_cache_hit = False
    with _CPU_TEMPLATE_CACHE_LOCK:
        cached = _CPU_TEMPLATE_CACHE.get(template_cache_key)
        if cached is None:
            template_shape, target_points, target_desc, template_ms = _extract_cpu(
                template_path
            )
            _CPU_TEMPLATE_CACHE.clear()
            _CPU_TEMPLATE_CACHE[template_cache_key] = (
                template_shape,
                target_points,
                target_desc,
            )
        else:
            template_shape, target_points, target_desc = cached
            template_ms = 0.0
            template_cache_hit = True
    candidate = _fit_candidate(
        (source_shape, source_points, source_desc),
        (template_shape, target_points, target_desc),
        job,
        backend=CPU_BACKEND,
        processing_ms=source_ms + template_ms,
    )
    candidate["worker_metrics"].update({
        "template_cache_hit": template_cache_hit,
        "source_feature_time_ms": round(source_ms, 3),
        "template_feature_time_ms": round(template_ms, 3),
    })
    return candidate


def _materialize_candidate(
    source_path: Path,
    template_path: Path,
    job: dict[str, Any],
    candidate: dict[str, Any],
    output_dir: Path,
) -> tuple[dict[str, Any], Path, Path, tuple[Path, ...]]:
    import numpy as np

    from services.accelerator_artifacts import (
        BOUND_CLIENT_FULL_COLUMN_VALIDATOR,
        LOCAL_FULL_COLUMN_CHECK_VERSION,
        CLIENT_EVIDENCE_SLOT_GEOMETRY_CONTRACT_VERSION,
        layout_requires_slot_geometry,
        validate_slot_geometry_receipt,
        candidate_artifact_sha256,
        client_full_column_evidence_manifest,
        client_evidence_manifest,
        client_multi_evidence_manifest,
        decode_raw_template,
        decode_uploaded_aligned_page,
        decode_uploaded_source_mask,
        materialize_global_artifacts,
        replay_and_verify_artifacts,
        validate_bound_client_quality_report,
    )
    from services.template_evidence import (
        build_evidence_from_aligned,
        build_full_column_difference_evidence_from_aligned,
        build_multi_evidence_from_aligned,
    )

    page_type_id, layout_sha, source_sha, template_sha = _validate_identity(job)
    if candidate.get("page_type_id") != page_type_id:
        raise RuntimeError("candidate_page_type_mismatch")
    started = time.perf_counter()
    artifacts = materialize_global_artifacts(
        source_path,
        template_path,
        candidate.get("homography"),
        output_dir,
    )
    multi_image = job.get("stage") == "student_multi_evidence"
    legacy_focus_sheets = multi_image and (
        isinstance(job.get("layout_snapshot"), dict)
        and (
            "_legacy_answer_regions" in job["layout_snapshot"]
            or "answer_regions" in job["layout_snapshot"]
        )
    )
    local_quality = None
    local_validation_ms = 0.0
    if multi_image and not legacy_focus_sheets:
        validation_started = time.perf_counter()
        local_quality = replay_and_verify_artifacts(
            source_path=source_path,
            template_path=template_path,
            homography=candidate["homography"],
            artifact_manifest=artifacts.manifest,
            aligned_page_path=artifacts.aligned_page_path,
            source_mask_path=artifacts.source_mask_path,
        )
        local_validation_ms = (time.perf_counter() - validation_started) * 1000.0
        aligned_rgb = local_quality.aligned_rgb
        source_mask = local_quality.source_mask
        template_rgb = local_quality.template_rgb
    else:
        aligned_rgb = decode_uploaded_aligned_page(artifacts.aligned_page_path)
        source_mask = decode_uploaded_source_mask(artifacts.source_mask_path)
        template_rgb = decode_raw_template(template_path)[:, :, ::-1]
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
        aligned_rgb,
        source_mask,
        template_rgb,
        job["layout_snapshot"],
        output_dir,
        diagnostics={
            "validation_mode": (
                "trusted_client_evidence_multi_v3"
                if legacy_focus_sheets
                else (
                    BOUND_CLIENT_FULL_COLUMN_VALIDATOR
                    if multi_image
                    else "trusted_client_evidence_v1"
                )
            )
        },
    )
    coverage = float(np.count_nonzero(source_mask == 255)) / float(source_mask.size)
    diagnostics = evidence.diagnostics
    answer_coverage = list(diagnostics.get("answer_source_coverage") or [])
    incomplete = [str(value) for value in diagnostics.get("incomplete_answer_regions", [])]
    evidence_paths = (
        tuple(Path(path) for path in evidence.evidence_paths)
        if evidence.evidence_paths
        else (Path(evidence.evidence_path),)
    )
    manifest = (
        client_multi_evidence_manifest(
            artifacts.manifest,
            list(evidence_paths),
            list(diagnostics.get("evidence_sheets") or []),
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
                    for column in diagnostics.get("evidence_columns") or []
                ],
            )
            if multi_image
            else client_evidence_manifest(artifacts.manifest, evidence_paths[0])
        )
    )
    del aligned_rgb, source_mask, template_rgb
    result = dict(candidate)
    result["coverage"] = coverage
    result["answer_source_complete"] = not incomplete
    result["incomplete_answer_regions"] = incomplete
    result["question_numbers"] = list(evidence.question_numbers)
    result["blank_question_numbers"] = list(evidence.blank_question_numbers)
    evidence_sheets = (
        list(diagnostics.get("evidence_sheets") or [])
        if legacy_focus_sheets
        else [
            {
                "sheet_id": column["sheet_id"],
                "question_numbers": list(column.get("question_numbers") or []),
            }
            for column in diagnostics.get("evidence_columns") or []
        ]
    )
    result["evidence_diagnostics"] = (
        {
            "ownership": str(diagnostics["ownership"]),
            "answer_source_coverage": answer_coverage,
            "incomplete_answer_regions": incomplete,
            "evidence_sheets": evidence_sheets,
        }
        if legacy_focus_sheets
        else {
            "ownership": str(diagnostics["ownership"]),
            "evidence_sheets": evidence_sheets,
        }
        if multi_image
        else {
            "ownership": str(diagnostics["ownership"]),
            "answer_source_coverage": answer_coverage,
            "incomplete_answer_regions": incomplete,
            "evidence_sheets": [],
        }
    )
    result["artifact_manifest"] = manifest
    if multi_image and layout_requires_slot_geometry(job["layout_snapshot"]):
        labels = {}
        for column in diagnostics.get("evidence_columns") or []:
            for placement in column.get("label_placements") or []:
                if "slot_id" not in placement:
                    continue
                slot_id = placement["slot_id"]
                if slot_id in labels:
                    raise ValueError("slot geometry renderer produced duplicate labels")
                labels[slot_id] = placement["region_bbox_1000"]
        result["slot_geometry_receipt"] = {
            "contract_version": CLIENT_EVIDENCE_SLOT_GEOMETRY_CONTRACT_VERSION,
            "layout_snapshot_sha256": layout_sha,
            "labels": labels,
        }
        validate_slot_geometry_receipt(
            result, stage=job["stage"], layout=job["layout_snapshot"],
            layout_snapshot_sha256=layout_sha,
        )
    result["artifact_sha256"] = candidate_artifact_sha256(
        page_type_id=page_type_id,
        source_sha256=source_sha,
        template_sha256=template_sha,
        layout_snapshot_sha256=layout_sha,
        homography=result["homography"],
        artifact_manifest=manifest,
    )
    if local_quality is not None:
        identity = {
            "source_sha256": source_sha,
            "template_sha256": template_sha,
            "layout_snapshot_sha256": layout_sha,
            "artifact_sha256": result["artifact_sha256"],
            "page_type_id": page_type_id,
        }
        report = {
            "version": LOCAL_FULL_COLUMN_CHECK_VERSION,
            **identity,
            "metrics": {
                "coverage": local_quality.coverage,
                "aligned_replay_mae": local_quality.aligned_mae,
                "aligned_replay_p99": local_quality.aligned_p99,
                "template_edge_recall": local_quality.template_edge_recall,
                "gradient_cosine": local_quality.gradient_cosine,
                "gradient_x_cosine": local_quality.gradient_x_cosine,
                "gradient_y_cosine": local_quality.gradient_y_cosine,
                "template_edge_pixels": local_quality.template_edge_pixels,
                "aligned_edge_pixels": local_quality.aligned_edge_pixels,
            },
        }
        result["local_quality_report"] = validate_bound_client_quality_report(
            report, **identity, coverage=coverage,
        )
        del local_quality
    metrics = dict(result.get("worker_metrics") or {})
    metrics.update({
        "artifact_contract_version": manifest["contract_version"],
        "artifact_generation_time_ms": round(
            (time.perf_counter() - started) * 1000.0,
            3,
        ),
        "answer_source_coverage": answer_coverage,
    })
    if multi_image and not legacy_focus_sheets:
        metrics["local_validation_time_ms"] = round(local_validation_ms, 3)
    result["worker_metrics"] = metrics
    _canonical(result)
    return (
        result,
        artifacts.aligned_page_path,
        artifacts.source_mask_path,
        evidence_paths,
    )


def process_candidate(
    source_path: str | Path,
    template_path: str | Path,
    job: dict[str, Any],
    *,
    artifact_output_dir: str | Path,
    gpu_self_test: GpuSelfTestResult | None = None,
    gpu_operation: Callable[[Path, Path, dict[str, Any]], dict[str, Any]] = _gpu_candidate,
    cpu_operation: Callable[[Path, Path, dict[str, Any]], dict[str, Any]] = _cpu_candidate,
    allow_gpu: bool = True,
    allow_cpu_fallback: bool = True,
) -> ProcessingOutcome:
    source = Path(source_path)
    template = Path(template_path)
    artifact_root = Path(artifact_output_dir)
    _validate_identity(job)
    if not isinstance(allow_gpu, bool) or not isinstance(allow_cpu_fallback, bool):
        raise RuntimeError("cpu_fallback_policy_invalid")
    global _GPU_SELF_TEST_CACHE
    if allow_gpu and gpu_self_test is None:
        with _GPU_SELF_TEST_CACHE_LOCK:
            if _GPU_SELF_TEST_CACHE is None:
                _GPU_SELF_TEST_CACHE = run_gpu_self_test()
            self_test = _GPU_SELF_TEST_CACHE
    elif allow_gpu:
        self_test = gpu_self_test
    else:
        self_test = None
    started = time.perf_counter()
    fallback_reason: str | None = None
    if self_test is not None and self_test.enabled:
        try:
            (
                candidate,
                aligned_path,
                mask_path,
                evidence_paths,
            ) = _materialize_candidate(
                source,
                template,
                job,
                gpu_operation(source, template, job),
                artifact_root / "gpu",
            )
            if not gpu_candidate_gate(candidate):
                raise GpuCandidateRejected("gpu_candidate_near_contract_boundary")
            return ProcessingOutcome(
                candidate=candidate,
                backend=GPU_BACKEND,
                fallback_reason=None,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
                aligned_page_path=aligned_path,
                source_mask_path=mask_path,
                evidence_path=evidence_paths[0],
                evidence_paths=evidence_paths,
            )
        except GpuCandidateRejected:
            fallback_reason = "gpu_candidate_near_contract_boundary"
            shutil.rmtree(artifact_root / "gpu", ignore_errors=True)
        except Exception:
            fallback_reason = "gpu_operation_failed"
            shutil.rmtree(artifact_root / "gpu", ignore_errors=True)
    elif self_test is not None:
        fallback_reason = self_test.fallback_reason or "gpu_not_enabled"
    else:
        fallback_reason = "gpu_not_scheduled"
    if not allow_cpu_fallback:
        raise RuntimeError("cpu_fallback_not_qualified")
    (
        candidate,
        aligned_path,
        mask_path,
        evidence_paths,
    ) = _materialize_candidate(
        source,
        template,
        job,
        cpu_operation(source, template, job),
        artifact_root / "cpu",
    )
    metrics = dict(candidate.get("worker_metrics") or {})
    metrics["fallback_reason"] = fallback_reason
    candidate["worker_metrics"] = metrics
    _canonical(candidate)
    return ProcessingOutcome(
        candidate=candidate,
        backend=CPU_BACKEND,
        fallback_reason=fallback_reason,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
        aligned_page_path=aligned_path,
        source_mask_path=mask_path,
        evidence_path=evidence_paths[0],
        evidence_paths=evidence_paths,
    )


def gpu_candidate_gate(candidate: dict[str, Any]) -> bool:
    """Keep the Windows GPU auto-submit set narrower than the CPU baseline."""

    try:
        import cv2
        import numpy as np

        good_matches = int(candidate["good_matches"])
        inliers = int(candidate["inliers"])
        inlier_ratio = float(candidate["inlier_ratio"])
        rmse = float(candidate["reprojection_rmse"])
        coverage = float(candidate["coverage"])
        matrix = np.asarray(candidate["homography"], dtype=np.float64)
        manifest = candidate["artifact_manifest"]
        source_width = int(manifest["source_width"])
        source_height = int(manifest["source_height"])
        if (
            matrix.shape != (3, 3)
            or not np.isfinite(matrix).all()
            or source_width <= 1
            or source_height <= 1
        ):
            return False
        source_corners = np.float32(
            [
                [0, 0],
                [source_width - 1, 0],
                [source_width - 1, source_height - 1],
                [0, source_height - 1],
            ]
        ).reshape(-1, 1, 2)
        projected = cv2.perspectiveTransform(source_corners, matrix)
        if not np.isfinite(projected).all():
            return False
        contour = projected.astype(np.float32)
        if not cv2.isContourConvex(contour) or cv2.contourArea(contour, oriented=True) <= 0:
            return False
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    return bool(
        good_matches >= 100
        and inliers >= 100
        and inlier_ratio >= 0.70
        and rmse <= 4.5
        and coverage >= 0.80
        and candidate.get("answer_source_complete") is True
        and candidate.get("incomplete_answer_regions") == []
    )
