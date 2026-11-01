"""Tests for the independent accelerator aligned-page artifact contract."""
from __future__ import annotations

import hashlib
from pathlib import Path
import struct

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw

from services import accelerator_artifacts
from services.accelerator_artifacts import (
    candidate_artifact_sha256,
    client_full_column_evidence_manifest,
    client_evidence_manifest,
    client_multi_evidence_manifest,
    materialize_global_artifacts,
    replay_and_verify_artifacts,
)
from services.template_evidence import build_evidence_from_aligned


def _page(path: Path, *, student: bool = False) -> None:
    image = Image.new("RGB", (800, 1000), "white")
    draw = ImageDraw.Draw(image)
    for y in range(80, 920, 80):
        draw.line((60, y, 740, y), fill="black", width=3)
    for x in range(80, 760, 80):
        draw.rectangle((x, 40, x + 25, 65), outline="black", width=2)
    if student:
        draw.line((120, 260, 500, 320), fill="black", width=8)
    image.save(path)


def _identity() -> list[list[float]]:
    return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "decoder",
    [
        accelerator_artifacts.decode_raw_source,
        accelerator_artifacts.decode_raw_template,
    ],
)
def test_raw_image_pixel_limit_is_checked_before_opencv_decode(
    tmp_path,
    monkeypatch,
    decoder,
):
    source = tmp_path / "oversized.png"
    source.write_bytes(b"header-only-test")

    class OversizedImage:
        size = (5_001, 5_000)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        accelerator_artifacts.Image,
        "open",
        lambda *_args, **_kwargs: OversizedImage(),
    )
    monkeypatch.setattr(
        accelerator_artifacts.cv2,
        "imread",
        lambda *_args, **_kwargs: pytest.fail("OpenCV must not decode oversized input"),
    )

    with pytest.raises(ValueError, match="dimensions"):
        decoder(source)


def test_chunked_absdiff_metrics_match_numpy_linear_percentile():
    left = np.zeros((10, 10, 3), dtype=np.uint8)
    right = np.zeros_like(left)
    right.reshape(-1)[-3:] = [10, 20, 30]

    difference = cv2.absdiff(left, right)
    expected = (
        float(np.mean(difference)),
        float(np.percentile(difference, 99.0)),
    )

    assert accelerator_artifacts._uint8_absdiff_metrics(left, right) == expected


def test_chunked_absdiff_metrics_match_numpy_across_chunks(monkeypatch):
    generator = np.random.default_rng(20260809)
    left = generator.integers(0, 256, size=(37, 43, 3), dtype=np.uint8)
    right = generator.integers(0, 256, size=left.shape, dtype=np.uint8)
    monkeypatch.setattr(accelerator_artifacts, "_ABS_DIFF_CHUNK_BYTES", 113)

    difference = cv2.absdiff(left, right)
    expected = (
        float(np.mean(difference)),
        float(np.percentile(difference, 99.0)),
    )

    assert accelerator_artifacts._uint8_absdiff_metrics(left, right) == expected


def test_materialized_artifact_replays_and_builds_existing_evidence(tmp_path):
    source = tmp_path / "source.png"
    template = tmp_path / "template.png"
    _page(template)
    _page(source, student=True)
    artifacts = materialize_global_artifacts(
        source,
        template,
        _identity(),
        tmp_path / "worker-output",
    )

    replay = replay_and_verify_artifacts(
        source_path=source,
        template_path=template,
        homography=_identity(),
        artifact_manifest=artifacts.manifest,
        aligned_page_path=artifacts.aligned_page_path,
        source_mask_path=artifacts.source_mask_path,
    )

    assert replay.coverage == 1.0
    assert replay.aligned_mae == 0.0
    assert replay.aligned_p99 == 0.0
    result = build_evidence_from_aligned(
        replay.aligned_rgb,
        replay.source_mask,
        replay.template_rgb,
        {
            "page_type_id": "page_1",
            "answer_regions": [{
                "question_number": "1",
                "question_type": "solve",
                "answer_bbox": [100, 180, 800, 420],
            }],
            "blank_detection_enabled": False,
        },
        tmp_path / "server-output",
        diagnostics={"validation_mode": "submitted_transform_replay_v1"},
    )
    assert Path(result.evidence_path).is_file()
    assert result.question_numbers == ["1"]
    assert result.diagnostics["answer_source_complete"] is True
    assert result.diagnostics["validation_mode"] == "submitted_transform_replay_v1"


def test_source_mask_pixel_tamper_is_rejected(tmp_path):
    source = tmp_path / "source.png"
    template = tmp_path / "template.png"
    _page(template)
    _page(source, student=True)
    artifacts = materialize_global_artifacts(source, template, _identity(), tmp_path / "out")
    mask = cv2.imread(str(artifacts.source_mask_path), cv2.IMREAD_GRAYSCALE)
    mask[10, 10] = 0
    assert cv2.imwrite(str(artifacts.source_mask_path), mask)
    tampered = dict(artifacts.manifest)
    tampered["source_mask"] = dict(tampered["source_mask"])
    tampered["source_mask"]["byte_size"] = artifacts.source_mask_path.stat().st_size
    tampered["source_mask"]["sha256"] = _sha(artifacts.source_mask_path)

    with pytest.raises(ValueError, match="source mask does not replay exactly"):
        replay_and_verify_artifacts(
            source_path=source,
            template_path=template,
            homography=_identity(),
            artifact_manifest=tampered,
            aligned_page_path=artifacts.aligned_page_path,
            source_mask_path=artifacts.source_mask_path,
        )


def test_aligned_page_tamper_is_rejected_even_with_updated_manifest(tmp_path):
    source = tmp_path / "source.png"
    template = tmp_path / "template.png"
    _page(template)
    _page(source, student=True)
    artifacts = materialize_global_artifacts(source, template, _identity(), tmp_path / "out")
    aligned = cv2.imread(str(artifacts.aligned_page_path), cv2.IMREAD_COLOR)
    aligned[100:300, 100:300] = 0
    assert cv2.imwrite(str(artifacts.aligned_page_path), aligned)
    tampered = dict(artifacts.manifest)
    tampered["aligned_page"] = dict(tampered["aligned_page"])
    tampered["aligned_page"]["byte_size"] = artifacts.aligned_page_path.stat().st_size
    tampered["aligned_page"]["sha256"] = _sha(artifacts.aligned_page_path)

    with pytest.raises(ValueError, match="aligned page does not match transform replay"):
        replay_and_verify_artifacts(
            source_path=source,
            template_path=template,
            homography=_identity(),
            artifact_manifest=tampered,
            aligned_page_path=artifacts.aligned_page_path,
            source_mask_path=artifacts.source_mask_path,
        )


def test_reflected_homography_is_rejected_even_with_self_consistent_artifacts(tmp_path):
    source = tmp_path / "source.png"
    template = tmp_path / "template.png"
    _page(template)
    _page(source, student=True)
    reflected = [[-1.0, 0.0, 799.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    artifacts = materialize_global_artifacts(source, template, reflected, tmp_path / "out")

    with pytest.raises(ValueError, match="reflects"):
        replay_and_verify_artifacts(
            source_path=source,
            template_path=template,
            homography=reflected,
            artifact_manifest=artifacts.manifest,
            aligned_page_path=artifacts.aligned_page_path,
            source_mask_path=artifacts.source_mask_path,
        )


def test_candidate_identity_binds_all_immutable_inputs(tmp_path):
    source = tmp_path / "source.png"
    template = tmp_path / "template.png"
    _page(template)
    _page(source, student=True)
    artifacts = materialize_global_artifacts(source, template, _identity(), tmp_path / "out")
    common = {
        "page_type_id": "page_1",
        "source_sha256": _sha(source),
        "template_sha256": _sha(template),
        "layout_snapshot_sha256": "a" * 64,
        "homography": _identity(),
        "artifact_manifest": artifacts.manifest,
    }
    original = candidate_artifact_sha256(**common)
    changed = candidate_artifact_sha256(
        **{**common, "layout_snapshot_sha256": "b" * 64}
    )
    assert original != changed


def test_client_evidence_manifest_replaces_full_resolution_uploads(tmp_path):
    source = tmp_path / "source.png"
    template = tmp_path / "template.png"
    _page(template)
    _page(source, student=True)
    artifacts = materialize_global_artifacts(
        source,
        template,
        _identity(),
        tmp_path / "out",
    )
    evidence = tmp_path / "evidence.png"
    Image.new("RGB", (640, 360), "white").save(evidence)

    manifest = client_evidence_manifest(artifacts.manifest, evidence)

    assert manifest["contract_version"] == "client-evidence-v1"
    assert set(manifest) == {
        "contract_version",
        "coordinate_space",
        "transform_graph",
        "renderer",
        "source_width",
        "source_height",
        "template_width",
        "template_height",
        "evidence",
    }
    assert manifest["evidence"]["sha256"] == _sha(evidence)
    assert manifest["evidence"]["width"] == 640
    assert manifest["evidence"]["height"] == 360


def test_multi_evidence_manifest_preserves_order_and_block_metadata(tmp_path):
    source = tmp_path / "source.png"
    template = tmp_path / "template.png"
    _page(template)
    _page(source, student=True)
    artifacts = materialize_global_artifacts(
        source,
        template,
        _identity(),
        tmp_path / "out",
    )
    images = []
    for index in range(3):
        path = tmp_path / f"crop-{index}.png"
        Image.new("RGB", (320, 240 + index), "white").save(path)
        images.append(path)

    manifest = client_multi_evidence_manifest(
        artifacts.manifest,
        images,
        [
            {
                "sheet_id": "sheet_01",
                "regions": [
                    {"question_number": "1", "bbox_1000": [100, 100, 450, 450]},
                    {"question_number": "2", "bbox_1000": [500, 100, 900, 450]},
                ],
            },
            {
                "sheet_id": "sheet_02",
                "regions": [
                    {"question_number": "3", "bbox_1000": [100, 100, 900, 900]},
                ],
            },
            {
                "sheet_id": "sheet_03",
                "regions": [
                    {"question_number": "4", "bbox_1000": [100, 100, 900, 900]},
                ],
            },
        ],
    )

    assert manifest["contract_version"] == "client-evidence-multi-v3"
    assert [item["kind"] for item in manifest["evidence_images"]] == [
        "evidence_01",
        "evidence_02",
        "evidence_03",
    ]
    assert [item["sheet_id"] for item in manifest["evidence_images"]] == [
        "sheet_01",
        "sheet_02",
        "sheet_03",
    ]

    reversed_manifest = dict(manifest)
    reversed_manifest["evidence_images"] = list(reversed(manifest["evidence_images"]))
    with pytest.raises(ValueError, match="order"):
        accelerator_artifacts.validate_artifact_manifest(reversed_manifest)

    wrong_sheet = dict(manifest)
    wrong_sheet["evidence_images"] = [dict(item) for item in manifest["evidence_images"]]
    wrong_sheet["evidence_images"][1]["sheet_id"] = "sheet_03"
    with pytest.raises(ValueError, match="metadata"):
        accelerator_artifacts.validate_artifact_manifest(wrong_sheet)

    too_many = dict(manifest)
    too_many["evidence_images"] = [
        {
            **manifest["evidence_images"][-1],
            "kind": f"evidence_{index:02d}",
            "sheet_id": f"sheet_{index:02d}",
            "regions": [{
                "question_number": str(index),
                "bbox_1000": [100, 100, 900, 900],
            }],
        }
        for index in range(1, accelerator_artifacts.MAX_MULTI_EVIDENCE_IMAGES + 2)
    ]
    with pytest.raises(ValueError, match="count"):
        accelerator_artifacts.validate_artifact_manifest(too_many)

    legacy = dict(manifest)
    legacy["contract_version"] = "client-evidence-multi-v1"
    with pytest.raises(ValueError):
        accelerator_artifacts.validate_artifact_manifest(legacy)


def test_evidence_upload_validation_does_not_decode_full_pixel_buffer(
    tmp_path,
    monkeypatch,
):
    evidence = tmp_path / "evidence.png"
    Image.new("RGB", (640, 360), "white").save(evidence)
    monkeypatch.setattr(
        accelerator_artifacts.cv2,
        "imdecode",
        lambda *_args, **_kwargs: pytest.fail(
            "trusted evidence validation must not allocate an OpenCV pixel buffer"
        ),
    )
    monkeypatch.setattr(
        accelerator_artifacts.Image,
        "open",
        lambda *_args, **_kwargs: pytest.fail(
            "trusted evidence validation must not invoke a Pillow decoder"
        ),
    )

    metadata = accelerator_artifacts.validate_uploaded_artifact_file(
        evidence,
        "evidence",
    )

    assert metadata["sha256"] == _sha(evidence)
    assert metadata["channels"] == 3


def test_uploaded_artifact_decode_uses_the_hash_bound_bytes(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    template = tmp_path / "template.png"
    _page(template)
    _page(source, student=True)
    artifacts = materialize_global_artifacts(
        source,
        template,
        _identity(),
        tmp_path / "out",
    )
    import services.accelerator_artifacts as artifact_module

    original_decode = artifact_module._decode_uploaded_png_bytes
    replaced = False

    def replace_paths_after_snapshot(raw, *, mask):
        nonlocal replaced
        if not replaced:
            replaced = True
            artifacts.aligned_page_path.write_bytes(b"late-replacement")
            artifacts.source_mask_path.write_bytes(b"late-replacement")
        return original_decode(raw, mask=mask)

    monkeypatch.setattr(
        artifact_module,
        "_decode_uploaded_png_bytes",
        replace_paths_after_snapshot,
    )

    replay = replay_and_verify_artifacts(
        source_path=source,
        template_path=template,
        homography=_identity(),
        artifact_manifest=artifacts.manifest,
        aligned_page_path=artifacts.aligned_page_path,
        source_mask_path=artifacts.source_mask_path,
    )

    assert replaced is True
    assert replay.aligned_mae == 0.0
    assert replay.aligned_p99 == 0.0


@pytest.mark.parametrize(
    ("mask", "color_type"),
    [(False, 2), (True, 0)],
)
def test_oversized_png_ihdr_is_rejected_before_opencv_decode(
    monkeypatch,
    mask,
    color_type,
):
    import services.accelerator_artifacts as artifact_module

    ihdr = struct.pack(">IIBBBBB", 20_000, 20_000, 8, color_type, 0, 0, 0)
    oversized_png_header = (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(ihdr))
        + b"IHDR"
        + ihdr
        + b"\x00\x00\x00\x00"
    )
    decode_called = False

    def forbidden_decode(*_args, **_kwargs):
        nonlocal decode_called
        decode_called = True
        raise AssertionError("OpenCV must not decode an oversized PNG")

    monkeypatch.setattr(artifact_module.cv2, "imdecode", forbidden_decode)

    with pytest.raises(ValueError, match="invalid dimensions"):
        artifact_module._decode_uploaded_png_bytes(
            oversized_png_header,
            mask=mask,
        )

    assert decode_called is False


def test_full_column_manifest_contains_order_only_and_no_answer_boxes(tmp_path):
    source = tmp_path / "source.png"
    template = tmp_path / "template.png"
    _page(template)
    _page(source, student=True)
    artifacts = materialize_global_artifacts(
        source,
        template,
        _identity(),
        tmp_path / "out-full-columns",
    )
    columns = []
    paths = []
    for index, question_numbers in enumerate((["1", "2"], ["3"]), 1):
        path = tmp_path / f"column-{index}.png"
        Image.new("RGB", (400, 1000), "white").save(path)
        paths.append(path)
        columns.append({
            "sheet_id": f"column_{index:02d}",
            "question_numbers": question_numbers,
        })

    manifest = client_full_column_evidence_manifest(
        artifacts.manifest,
        paths,
        columns,
    )

    assert manifest["contract_version"] == "client-evidence-columns-v1"
    assert [item["question_numbers"] for item in manifest["evidence_images"]] == [
        ["1", "2"],
        ["3"],
    ]
    assert all(
        "regions" not in item and "bbox_1000" not in item
        for item in manifest["evidence_images"]
    )


def _bound_quality_report():
    identity = {
        "source_sha256": "a" * 64,
        "template_sha256": "b" * 64,
        "layout_snapshot_sha256": "c" * 64,
        "artifact_sha256": "d" * 64,
        "page_type_id": "page_1",
    }
    report = {
        "version": accelerator_artifacts.LOCAL_FULL_COLUMN_CHECK_VERSION,
        **identity,
        "metrics": {
            "coverage": 0.95,
            "aligned_replay_mae": 0.0,
            "aligned_replay_p99": 0.0,
            "template_edge_recall": 0.95,
            "gradient_cosine": 0.95,
            "gradient_x_cosine": 0.90,
            "gradient_y_cosine": 0.90,
            "template_edge_pixels": 12000,
            "aligned_edge_pixels": 13000,
        },
    }
    return report, identity


def test_bound_quality_report_accepts_finite_bound_measurements_without_pixel_work(monkeypatch):
    report, identity = _bound_quality_report()

    def forbidden(*_args, **_kwargs):
        pytest.fail("quality report parsing must not decode or transform images")

    monkeypatch.setattr(accelerator_artifacts, "decode_raw_source", forbidden)
    monkeypatch.setattr(accelerator_artifacts, "replay_and_verify_artifacts", forbidden)
    normalized = accelerator_artifacts.validate_bound_client_quality_report(
        report, **identity, coverage=0.95,
    )
    assert normalized == report
    assert normalized is not report and normalized["metrics"] is not report["metrics"]


@pytest.mark.parametrize("field,value", [
    ("coverage", 0.60), ("coverage", 0.90),
    ("aligned_replay_mae", 1.01), ("aligned_replay_p99", 4.01),
    ("template_edge_recall", 0.69), ("gradient_cosine", 0.45),
    ("gradient_x_cosine", 0.37), ("gradient_y_cosine", 0.37),
    ("template_edge_pixels", 999), ("aligned_edge_pixels", 0),
    ("template_edge_pixels", 12000.0), ("gradient_cosine", float("nan")),
    ("gradient_cosine", float("inf")), ("gradient_cosine", True),
    ("gradient_cosine", "0.9"),
])
def test_bound_quality_report_rejects_bad_metrics(field, value):
    report, identity = _bound_quality_report()
    report["metrics"][field] = value
    with pytest.raises(ValueError, match="bound client"):
        accelerator_artifacts.validate_bound_client_quality_report(
            report, **identity, coverage=0.95,
        )


@pytest.mark.parametrize("field", [
    "source_sha256", "template_sha256", "layout_snapshot_sha256",
    "artifact_sha256", "page_type_id", "version",
])
def test_bound_quality_report_rejects_identity_changes(field):
    report, identity = _bound_quality_report()
    report[field] = "wrong"
    with pytest.raises(ValueError, match="identity"):
        accelerator_artifacts.validate_bound_client_quality_report(
            report, **identity, coverage=0.95,
        )


@pytest.mark.parametrize("value", [None, True, "0.95", float("nan"), 1.1])
def test_bound_quality_report_rejects_invalid_candidate_coverage(value):
    report, identity = _bound_quality_report()
    with pytest.raises(ValueError, match="coverage"):
        accelerator_artifacts.validate_bound_client_quality_report(
            report, **identity, coverage=value,
        )
