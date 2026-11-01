from __future__ import annotations

import json
import hashlib

import pytest
from PIL import Image, ImageDraw

from mathagent_worker.gpu_backend import (
    CPU_BACKEND,
    GPU_BACKEND,
    GPU_BACKEND_VERSION,
    GpuSelfTestResult,
)
from mathagent_worker.preprocessing import (
    ALGORITHM_VERSION,
    ProcessingOutcome,
    process_candidate,
)
import mathagent_worker.preprocessing as preprocessing_module


def _job() -> dict:
    return {
        "stage": "template_registration",
        "algorithm_version": ALGORITHM_VERSION,
        "source_sha256": "c" * 64,
        "template_sha256": "a" * 64,
        "layout_snapshot_sha256": "b" * 64,
        "layout_snapshot": {
            "page_type_id": "page-1",
            "answer_regions": [{
                "question_number": "1",
                "question_type": "solve",
                "answer_bbox": [0, 0, 10, 10],
            }],
        },
    }


def _self_test(enabled: bool = True, reason: str | None = None) -> GpuSelfTestResult:
    return GpuSelfTestResult(
        available=enabled,
        enabled=enabled,
        backend=GPU_BACKEND if enabled else CPU_BACKEND,
        backend_version=GPU_BACKEND_VERSION,
        device_name="NVIDIA Test GPU" if enabled else None,
        driver_version="1.0" if enabled else None,
        gpu_time_ms=1.0 if enabled else None,
        cpu_time_ms=None,
        speedup=None,
        projection_max_px=5.0 if enabled else None,
        fallback_reason=reason,
    )


def _candidate(backend: str) -> dict:
    homography = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    return {
        "status": "succeeded",
        "page_type_id": "page-1",
        "homography": homography,
        "good_matches": 120,
        "inliers": 110,
        "inlier_ratio": 110 / 120,
        "reprojection_rmse": 0.1,
        "coverage": 1.0,
        "worker_metrics": {"backend": backend},
    }


def _images(tmp_path):
    source = tmp_path / "source.png"
    template = tmp_path / "template.png"
    Image.new("RGB", (160, 200), "white").save(source)
    Image.new("RGB", (160, 200), "white").save(template)
    return source, template


def test_oversized_image_is_rejected_before_opencv_decode(tmp_path, monkeypatch) -> None:
    source = tmp_path / "oversized.png"
    source.write_bytes(b"header-only-test")

    class OversizedImage:
        size = (5_001, 5_000)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        preprocessing_module.Image,
        "open",
        lambda *_args, **_kwargs: OversizedImage(),
    )

    import cv2

    monkeypatch.setattr(
        cv2,
        "imread",
        lambda *_args, **_kwargs: pytest.fail("OpenCV must not decode oversized input"),
    )

    with pytest.raises(RuntimeError, match="image_dimensions_invalid"):
        preprocessing_module._safe_decode(source)


def test_gpu_candidate_is_selected_when_self_test_is_ready(tmp_path) -> None:
    calls = []
    source, template = _images(tmp_path)

    def gpu(source, template, job):
        calls.append((source.name, template.name, job["stage"]))
        return _candidate(GPU_BACKEND)

    outcome = process_candidate(
        source,
        template,
        _job(),
        artifact_output_dir=tmp_path / "artifacts",
        gpu_self_test=_self_test(),
        gpu_operation=gpu,
        cpu_operation=lambda *_args: pytest.fail("CPU must not run"),
    )

    assert outcome.backend == GPU_BACKEND
    assert outcome.fallback_reason is None
    assert calls == [("source.png", "template.png", "template_registration")]
    assert outcome.aligned_page_path.is_file()
    assert outcome.source_mask_path.is_file()
    assert outcome.evidence_path is not None
    assert outcome.evidence_path.is_file()
    assert outcome.candidate["answer_source_complete"] is True
    assert outcome.candidate["question_numbers"] == ["1"]
    assert outcome.candidate["blank_question_numbers"] == []
    assert outcome.candidate["evidence_diagnostics"]["ownership"] == (
        "confirmed_bbox_neighbor_clamped"
    )
    assert outcome.candidate["evidence_diagnostics"][
        "incomplete_answer_regions"
    ] == []
    manifest = outcome.candidate["artifact_manifest"]
    assert manifest["contract_version"] == "client-evidence-v1"
    assert "local_quality_report" not in outcome.candidate
    assert "evidence" in manifest
    assert "aligned_page" not in manifest
    assert "source_mask" not in manifest
    json.dumps(outcome.candidate, allow_nan=False)


def test_gpu_exception_runs_real_cpu_candidate_and_records_bounded_reason(tmp_path) -> None:
    calls = []
    source, template = _images(tmp_path)

    def gpu(*_args):
        calls.append("gpu")
        raise RuntimeError("driver detail must not leak")

    def cpu(*_args):
        calls.append("cpu")
        return _candidate(CPU_BACKEND)

    outcome = process_candidate(
        source,
        template,
        _job(),
        artifact_output_dir=tmp_path / "artifacts",
        gpu_self_test=_self_test(),
        gpu_operation=gpu,
        cpu_operation=cpu,
    )

    assert calls == ["gpu", "cpu"]
    assert outcome.backend == CPU_BACKEND
    assert outcome.fallback_reason == "gpu_operation_failed"
    assert outcome.candidate["worker_metrics"]["fallback_reason"] == "gpu_operation_failed"
    assert "driver detail" not in json.dumps(outcome.candidate)


def test_client_evidence_preserves_local_blank_detection(tmp_path) -> None:
    source, template = _images(tmp_path)
    job = _job()
    job["layout_snapshot"]["blank_detection_enabled"] = True

    outcome = process_candidate(
        source,
        template,
        job,
        artifact_output_dir=tmp_path / "artifacts",
        gpu_self_test=_self_test(),
        gpu_operation=lambda *_args: _candidate(GPU_BACKEND),
        cpu_operation=lambda *_args: pytest.fail("CPU must not run"),
    )

    assert outcome.candidate["question_numbers"] == ["1"]
    assert outcome.candidate["blank_question_numbers"] == ["1"]


def test_gpu_failure_never_runs_unqualified_cpu_fallback(tmp_path) -> None:
    calls = []
    source, template = _images(tmp_path)

    def broken_gpu(*_args):
        calls.append("gpu")
        raise RuntimeError("gpu failed")

    with pytest.raises(RuntimeError, match="cpu_fallback_not_qualified"):
        process_candidate(
            source,
            template,
            _job(),
            artifact_output_dir=tmp_path / "artifacts",
            gpu_self_test=_self_test(),
            gpu_operation=broken_gpu,
            cpu_operation=lambda *_args: calls.append("cpu") or _candidate(CPU_BACKEND),
            allow_cpu_fallback=False,
        )

    assert calls == ["gpu"]


def test_disabled_gpu_never_invokes_gpu_operation(tmp_path) -> None:
    calls = []
    source, template = _images(tmp_path)
    outcome = process_candidate(
        source,
        template,
        _job(),
        artifact_output_dir=tmp_path / "artifacts",
        gpu_self_test=_self_test(False, "gpu_runtime_missing"),
        gpu_operation=lambda *_args: calls.append("gpu"),
        cpu_operation=lambda *_args: calls.append("cpu") or _candidate(CPU_BACKEND),
    )

    assert calls == ["cpu"]
    assert outcome.backend == CPU_BACKEND
    assert outcome.fallback_reason == "gpu_runtime_missing"


def test_cpu_route_policy_never_probes_or_invokes_gpu(tmp_path, monkeypatch) -> None:
    calls = []
    source, template = _images(tmp_path)
    monkeypatch.setattr(
        preprocessing_module,
        "run_gpu_self_test",
        lambda: pytest.fail("GPU self-test must not run on the CPU route"),
    )

    outcome = process_candidate(
        source,
        template,
        _job(),
        artifact_output_dir=tmp_path / "artifacts",
        allow_gpu=False,
        gpu_operation=lambda *_args: calls.append("gpu"),
        cpu_operation=lambda *_args: calls.append("cpu") or _candidate(CPU_BACKEND),
    )

    assert calls == ["cpu"]
    assert outcome.backend == CPU_BACKEND
    assert outcome.fallback_reason == "gpu_not_scheduled"


def test_no_qualified_backend_fails_before_cpu_processing(tmp_path) -> None:
    calls = []
    source, template = _images(tmp_path)

    with pytest.raises(RuntimeError, match="cpu_fallback_not_qualified"):
        process_candidate(
            source,
            template,
            _job(),
            artifact_output_dir=tmp_path / "artifacts",
            gpu_self_test=_self_test(False, "gpu_runtime_missing"),
            gpu_operation=lambda *_args: calls.append("gpu"),
            cpu_operation=lambda *_args: calls.append("cpu") or _candidate(CPU_BACKEND),
            allow_cpu_fallback=False,
        )

    assert calls == []


def test_near_boundary_gpu_candidate_runs_cpu_before_submission(tmp_path) -> None:
    calls = []
    source, template = _images(tmp_path)
    unsafe = _candidate(GPU_BACKEND)
    unsafe["inlier_ratio"] = 0.6
    unsafe["reprojection_rmse"] = 1.8

    outcome = process_candidate(
        source,
        template,
        _job(),
        artifact_output_dir=tmp_path / "artifacts",
        gpu_self_test=_self_test(),
        gpu_operation=lambda *_args: calls.append("gpu") or unsafe,
        cpu_operation=lambda *_args: calls.append("cpu") or _candidate(CPU_BACKEND),
    )

    assert calls == ["gpu", "cpu"]
    assert outcome.backend == CPU_BACKEND
    assert outcome.fallback_reason == "gpu_candidate_near_contract_boundary"
    assert outcome.candidate["worker_metrics"]["fallback_reason"] == "gpu_candidate_near_contract_boundary"


def test_reflected_gpu_candidate_runs_cpu_before_submission(tmp_path) -> None:
    calls = []
    source, template = _images(tmp_path)
    reflected = _candidate(GPU_BACKEND)
    reflected["homography"] = [
        [-1.0, 0.0, 159.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
    ]

    outcome = process_candidate(
        source,
        template,
        _job(),
        artifact_output_dir=tmp_path / "artifacts",
        gpu_self_test=_self_test(),
        gpu_operation=lambda *_args: calls.append("gpu") or reflected,
        cpu_operation=lambda *_args: calls.append("cpu") or _candidate(CPU_BACKEND),
    )

    assert calls == ["gpu", "cpu"]
    assert outcome.backend == CPU_BACKEND
    assert outcome.fallback_reason == "gpu_candidate_near_contract_boundary"


def test_gpu_template_features_are_cached_by_algorithm_and_template_hash(
    tmp_path,
    monkeypatch,
) -> None:
    calls = []
    source, template = _images(tmp_path)
    preprocessing_module._GPU_TEMPLATE_CACHE.clear()

    def extract(path, _root):
        calls.append(path.name)
        return (200, 160), object(), object(), 12.0, "resident"

    def fit(*_args, **_kwargs):
        return {"worker_metrics": {}}

    monkeypatch.setattr(preprocessing_module, "_extract_gpu", extract)
    monkeypatch.setattr(preprocessing_module, "_fit_candidate", fit)

    first = preprocessing_module._gpu_candidate(source, template, _job())
    assert set(preprocessing_module._GPU_TEMPLATE_CACHE) == {
        (ALGORITHM_VERSION, "a" * 64)
    }
    second = preprocessing_module._gpu_candidate(source, template, _job())

    assert calls == ["source.png", "template.png", "source.png"]
    assert first["worker_metrics"]["template_cache_hit"] is False
    assert second["worker_metrics"]["template_cache_hit"] is True
    assert second["worker_metrics"]["template_feature_time_ms"] == 0.0


@pytest.mark.parametrize(
    "field,value",
    [
        ("stage", "grading"),
        ("algorithm_version", "other-version"),
        ("layout_snapshot_sha256", "not-a-hash"),
    ],
)
def test_invalid_job_contract_fails_before_any_processing(tmp_path, field, value) -> None:
    job = _job()
    job[field] = value
    with pytest.raises(RuntimeError):
        process_candidate(
            tmp_path / "source.png",
            tmp_path / "template.png",
            job,
            artifact_output_dir=tmp_path / "artifacts",
            gpu_self_test=_self_test(),
            gpu_operation=lambda *_args: pytest.fail("GPU must not run"),
            cpu_operation=lambda *_args: pytest.fail("CPU must not run"),
        )


def _full_column_job(tmp_path, *, printed=True):
    from services.template_anchor_crops import build_anchor_crop_plan

    source, template = tmp_path / "source.png", tmp_path / "template.png"
    page = Image.new("RGB", (800, 1000), "white")
    draw = ImageDraw.Draw(page)
    if printed:
        for y in range(70, 950, 90):
            for x in range(70, 730, 70):
                draw.rectangle((x, y, x + 30, y + 20), outline="black", width=2)
                draw.line((x, y + 20, x + 30, y), fill="black", width=2)
        draw.text((60, 200), "1. Solve", fill="black")
    page.save(template)
    draw.line((150, 360, 440, 410), fill="black", width=7)
    page.save(source)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    template_sha = hashlib.sha256(template.read_bytes()).hexdigest()
    layout = {
        "page_type_id": "page-1",
        "question_numbers": ["1"],
        "question_schema": [{"question_number": "1", "question_type": "solve"}],
        "question_anchors": [{
            "question_number": "1", "question_type": "solve",
            "question_number_bbox": [60, 200, 100, 230],
        }],
        "blank_detection_enabled": False,
        "question_crop_plan": build_anchor_crop_plan({"column_layout": {
            "column_count": 1,
            "columns": [{"column_index": 1, "x1_1000": 0, "x2_1000": 1000}],
        }}, artifact_sha256=template_sha),
    }
    return source, template, {
        "stage": "student_multi_evidence",
        "algorithm_version": ALGORITHM_VERSION,
        "source_sha256": source_sha,
        "template_sha256": template_sha,
        "layout_snapshot_sha256": hashlib.sha256(
            preprocessing_module._canonical(layout)
        ).hexdigest(),
        "layout_snapshot": layout,
    }


@pytest.mark.parametrize("use_gpu", [False, True])
def test_full_column_performs_actual_local_quality_check_and_binds_report(tmp_path, use_gpu):
    source, template, job = _full_column_job(tmp_path)
    # Only feature matching is injected. Transform, QC and difference rendering
    # use production implementations, exercising both candidate branch paths.
    outcome = process_candidate(
        source, template, job,
        artifact_output_dir=tmp_path / "artifacts",
        allow_gpu=use_gpu,
        gpu_self_test=_self_test(),
        gpu_operation=lambda *_args: _candidate(GPU_BACKEND),
        cpu_operation=lambda *_args: _candidate(CPU_BACKEND),
    )
    assert outcome.backend == (GPU_BACKEND if use_gpu else CPU_BACKEND)
    report = outcome.candidate["local_quality_report"]
    assert report["version"] == "local-full-column-check-v2"
    for field in ("source_sha256", "template_sha256", "layout_snapshot_sha256"):
        assert report[field] == job[field]
    assert report["artifact_sha256"] == outcome.candidate["artifact_sha256"]
    assert report["page_type_id"] == "page-1"
    assert report["metrics"]["aligned_replay_mae"] == 0.0
    assert report["metrics"]["template_edge_pixels"] >= 1000
    assert outcome.candidate["worker_metrics"]["local_validation_time_ms"] > 0
    assert outcome.candidate["question_numbers"] == ["1"]
    assert len(outcome.evidence_paths) == 1 and outcome.evidence_paths[0].is_file()


def test_full_column_cannot_silently_skip_local_quality_check(tmp_path):
    source, template, job = _full_column_job(tmp_path, printed=False)
    with pytest.raises(ValueError, match="insufficient structure"):
        process_candidate(
            source, template, job,
            artifact_output_dir=tmp_path / "artifacts",
            allow_gpu=False,
            cpu_operation=lambda *_args: _candidate(CPU_BACKEND),
        )
