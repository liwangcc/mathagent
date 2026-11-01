from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image, ImageChops

from mathagent_worker import template_ocr_evidence as evidence


def _canonical(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _legacy_plan(source_sha256: str) -> dict:
    anchor_hash = hashlib.sha256(b"anchor").hexdigest()
    return {
        "version": 1,
        "strategy": "top_anchor_continuous_half_columns",
        "coordinate_space": "template_1000",
        "source_artifact_sha256": source_sha256,
        "provider_bottom_ignored": True,
        "requires_question_number_gate": True,
        "parameters": {
            "split_x_1000": 500,
            "column_overlap_1000": 12,
            "vertical_overlap_1000": 10,
            "anchors_per_block": 3,
            "anchor_merge_tolerance_1000": 8,
        },
        "expected_question_numbers": ["1", "2"],
        "anchors": [
            {
                "anchor_id": "left-1",
                "column": "left",
                "top_1000": 100,
                "question_number_hints": ["1"],
                "source_candidate_sha256": [anchor_hash],
                "source_candidate_count": 1,
            },
            {
                "anchor_id": "right-1",
                "column": "right",
                "top_1000": 120,
                "question_number_hints": ["2"],
                "source_candidate_sha256": [anchor_hash],
                "source_candidate_count": 1,
            },
        ],
        "blocks": [
            {
                "block_id": "left-1",
                "column": "left",
                "bbox_1000": [0, 0, 512, 1000],
                "source_anchor_ids": ["left-1"],
                "question_number_hints": ["1"],
                "fallback_full_column": False,
            },
            {
                "block_id": "right-1",
                "column": "right",
                "bbox_1000": [488, 0, 1000, 1000],
                "source_anchor_ids": ["right-1"],
                "question_number_hints": ["2"],
                "fallback_full_column": False,
            },
        ],
        "warnings": [],
        "source_summary": {
            "trusted_candidate_count": 2,
            "rejected_candidate_count": 0,
        },
    }


def _plan(source_sha256: str) -> dict:
    columns = [
        {"column_index": 1, "x1_1000": 0, "x2_1000": 330},
        {"column_index": 2, "x1_1000": 330, "x2_1000": 665},
        {"column_index": 3, "x1_1000": 665, "x2_1000": 1000},
    ]
    blocks = [
        {
            "block_id": f"col_{column['column_index']}",
            "column": f"col_{column['column_index']}",
            "bbox_1000": [column["x1_1000"], 0, column["x2_1000"], 1000],
            "source_anchor_ids": [],
            "question_number_hints": [],
            "fallback_full_column": True,
        }
        for column in columns
    ]
    return {
        "version": 2,
        "strategy": "full_height_columns",
        "coordinate_space": "template_1000",
        "source_artifact_sha256": source_sha256,
        "provider_bottom_ignored": True,
        "requires_question_number_gate": True,
        "parameters": {"column_count": 3, "columns": columns},
        "expected_question_numbers": ["1", "2", "3"],
        "anchors": [],
        "blocks": blocks,
        "warnings": [],
        "hint_coverage": {
            "observed_question_number_hints": [],
            "missing_expected_question_numbers": [],
            "unexpected_question_number_hints": [],
        },
        "source_summary": {
            "trusted_candidate_count": 3,
            "rejected_candidate_count": 0,
        },
        "coverage": {
            "columns": ["col_1", "col_2", "col_3"],
            "page_top_to_bottom": True,
            "vertical_gaps": 0,
            "block_count": 3,
            "anchor_count": 0,
        },
    }


def _server_frozen_plan(source_sha256: str, *, width: int, height: int) -> dict:
    """Build the v2 shape emitted after server-side crop-plan validation."""
    from services.template_anchor_crops import validate_anchor_crop_plan

    raw = _plan(source_sha256)
    raw["layout_context"] = {
        "layout_revision": 7,
        "coordinate_space": "normalized_1000_v1",
        "crop_endpoint_version": "rounded_source_pixels_v1",
        "source_width": width,
        "source_height": height,
        "source_dimensions": {"width": width, "height": height},
        "image_sha256": source_sha256,
        "contract_version": "template-column-layout-v2",
    }
    for index, column in enumerate(raw["parameters"]["columns"], 1):
        left = column["x1_1000"]
        right = column["x2_1000"]
        crop_left = round(left * width / 1000)
        crop_right = round(right * width / 1000)
        metadata = {
            "column_id": f"col-{index}",
            "column_order": index,
            "layout_revision": 7,
            "crop_left_px": crop_left,
            "crop_right_px": crop_right,
            "crop_box_px": [crop_left, 0, crop_right, height],
            "actual_pixel_endpoints": {
                "left": crop_left,
                "right": crop_right,
            },
            "crop_endpoint_version": "rounded_source_pixels_v1",
            "crop_coordinate_space": "source_pixels",
            "source_width": width,
            "source_height": height,
            "source_dimensions": {"width": width, "height": height},
            "image_sha256": source_sha256,
        }
        column.update(metadata)
        raw["blocks"][index - 1].update(metadata)
    return validate_anchor_crop_plan(raw, artifact_sha256=source_sha256)


def _job(source_sha256: str, layout_snapshot: dict) -> dict:
    return {
        "stage": "template_ocr_evidence",
        "algorithm_version": "template-registration-v2",
        "source_sha256": source_sha256,
        "layout_snapshot": layout_snapshot,
        "layout_snapshot_sha256": _canonical(layout_snapshot),
    }


def test_renderer_emits_bound_rgb_png_montage(tmp_path: Path) -> None:
    source = tmp_path / "canonical.png"
    image = Image.new("RGB", (800, 1000), "white")
    try:
        for y in range(0, 1000, 100):
            for x in range(0, 800, 100):
                image.putpixel((x, y), (x % 255, y % 255, (x + y) % 255))
        image.save(source, format="PNG")
    finally:
        image.close()
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    layout_snapshot = {"question_crop_plan": _plan(source_sha256)}
    job = _job(source_sha256, layout_snapshot)
    output = tmp_path / "evidence.png"

    result = evidence.render_template_ocr_evidence(source, job, output)

    assert result.artifact_path == output
    manifest = result.candidate["artifact_manifest"]
    assert manifest["contract_version"] == "template-ocr-evidence-v2"
    artifact = manifest["template_ocr_evidence"]
    assert artifact["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert artifact["byte_size"] == output.stat().st_size
    assert artifact["channels"] == 3
    assert artifact["format"] == "png"
    assert result.candidate["input_identity"] == {
        "canonical_sha256": source_sha256,
        "layout_snapshot_sha256": job["layout_snapshot_sha256"],
    }
    with Image.open(output) as rendered:
        assert rendered.mode == "RGB"
        assert rendered.size == (800, 1000)
        assert rendered.size == (artifact["width"], artifact["height"])


def test_renderer_accepts_plan_as_direct_layout_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "canonical.png"
    image = Image.new("RGB", (640, 800), "white")
    try:
        image.save(source, format="PNG")
    finally:
        image.close()
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    plan = _plan(source_sha256)

    result = evidence.render_template_ocr_evidence(
        source,
        _job(source_sha256, plan),
        tmp_path / "evidence.png",
    )

    assert result.candidate["status"] == "succeeded"


def test_renderer_strictly_migrates_legacy_plan_to_two_full_height_columns(
    tmp_path: Path,
) -> None:
    source = tmp_path / "canonical.png"
    image = Image.new("RGB", (800, 600), "white")
    try:
        image.paste("red", (0, 0, 400, 600))
        image.paste("blue", (400, 0, 800, 600))
        image.save(source, format="PNG")
    finally:
        image.close()
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    output = tmp_path / "evidence.png"

    evidence.render_template_ocr_evidence(
        source,
        _job(
            source_sha256,
            {"question_crop_plan": _legacy_plan(source_sha256)},
        ),
        output,
    )

    with Image.open(output) as rendered:
        assert rendered.size == (800, 600)
        assert rendered.getpixel((100, 300)) == (255, 0, 0)
        assert rendered.getpixel((700, 300)) == (0, 0, 255)


def test_three_non_equal_columns_reassemble_without_duplicate_boundary_pixels(
    tmp_path: Path,
) -> None:
    source = tmp_path / "canonical.png"
    image = Image.new("RGB", (101, 80), "white")
    try:
        image.paste("red", (0, 0, 33, 80))
        image.paste("green", (33, 0, 67, 80))
        image.paste("blue", (67, 0, 101, 80))
        image.save(source, format="PNG")
    finally:
        image.close()
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    output = tmp_path / "evidence.png"

    evidence.render_template_ocr_evidence(
        source,
        _job(
            source_sha256,
            {"question_crop_plan": _plan(source_sha256)},
        ),
        output,
    )

    with Image.open(source) as original, Image.open(output) as rendered:
        assert rendered.size == (101, 80)
        assert ImageChops.difference(original, rendered).getbbox() is None


def test_renderer_accepts_server_frozen_layout_metadata(tmp_path: Path) -> None:
    source = tmp_path / "canonical.png"
    image = Image.new("RGB", (1001, 80), "white")
    try:
        image.paste("red", (0, 0, 333, 80))
        image.paste("green", (333, 0, 667, 80))
        image.paste("blue", (667, 0, 1001, 80))
        image.save(source, format="PNG")
    finally:
        image.close()
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    plan = _server_frozen_plan(source_sha256, width=1001, height=80)
    layout_snapshot = {"question_crop_plan": plan}
    output = tmp_path / "evidence.png"

    result = evidence.render_template_ocr_evidence(
        source,
        _job(source_sha256, layout_snapshot),
        output,
    )

    assert result.candidate["status"] == "succeeded"
    with Image.open(source) as original, Image.open(output) as rendered:
        assert ImageChops.difference(original, rendered).getbbox() is None


def test_renderer_rejects_frozen_metadata_for_different_source_dimensions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "canonical.png"
    image = Image.new("RGB", (1001, 80), "white")
    try:
        image.save(source, format="PNG")
    finally:
        image.close()
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    # The plan is internally consistent for a 1000x80 source, but must not be
    # rescaled when it is rendered against the actual 1001x80 artifact.
    plan = _server_frozen_plan(source_sha256, width=1000, height=80)

    with pytest.raises(RuntimeError, match="question_crop_plan_invalid"):
        evidence.render_template_ocr_evidence(
            source,
            _job(source_sha256, {"question_crop_plan": plan}),
            tmp_path / "evidence.png",
        )


@pytest.mark.parametrize("conflict", ["column_hash", "column_revision", "context_hash"])
def test_renderer_rejects_frozen_identity_conflicts(
    tmp_path: Path,
    conflict: str,
) -> None:
    source = tmp_path / "canonical.png"
    image = Image.new("RGB", (1001, 80), "white")
    try:
        image.save(source, format="PNG")
    finally:
        image.close()
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    plan = _server_frozen_plan(source_sha256, width=1001, height=80)
    if conflict == "column_hash":
        for column, block in zip(
            plan["parameters"]["columns"], plan["blocks"]
        ):
            column["image_sha256"] = "b" * 64
            block["image_sha256"] = "b" * 64
    elif conflict == "column_revision":
        for column, block in zip(
            plan["parameters"]["columns"], plan["blocks"]
        ):
            column["layout_revision"] = 8
            block["layout_revision"] = 8
    else:
        plan["layout_context"]["image_sha256"] = "b" * 64
    # Keep the snapshot hash valid so the identity conflict is the exercised
    # failure, rather than the outer snapshot-integrity guard.
    layout_snapshot = {"question_crop_plan": plan}

    with pytest.raises(RuntimeError) as raised:
        evidence.render_template_ocr_evidence(
            source,
            _job(source_sha256, layout_snapshot),
            tmp_path / "evidence.png",
        )
    assert str(raised.value) in {
        "question_crop_plan_invalid",
        "question_crop_plan_source_mismatch",
    }


def test_renderer_rejects_tampered_layout_before_image_decode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "canonical.png"
    source.write_bytes(b"not-an-image")
    source_sha256 = "a" * 64
    layout_snapshot = {"question_crop_plan": _plan(source_sha256)}
    job = _job(source_sha256, layout_snapshot)
    job["layout_snapshot"]["question_crop_plan"]["blocks"][0][
        "bbox_1000"
    ] = [0, 1, 512, 1000]
    monkeypatch.setattr(
        evidence.Image,
        "open",
        lambda *_args, **_kwargs: pytest.fail("image decode must not start"),
    )

    with pytest.raises(RuntimeError, match="layout_snapshot_hash_mismatch"):
        evidence.render_template_ocr_evidence(
            source,
            job,
            tmp_path / "evidence.png",
        )


def test_renderer_rejects_pixel_limit_before_full_decode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "canonical.png"
    image = Image.new("RGB", (64, 64), "white")
    try:
        image.save(source, format="PNG")
    finally:
        image.close()
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    layout_snapshot = {"question_crop_plan": _plan(source_sha256)}
    original_load = Image.Image.load
    load_calls = 0

    def tracked_load(self, *args, **kwargs):
        nonlocal load_calls
        load_calls += 1
        return original_load(self, *args, **kwargs)

    monkeypatch.setattr(evidence, "MAX_DECODE_PIXELS", 1)
    monkeypatch.setattr(Image.Image, "load", tracked_load)

    with pytest.raises(RuntimeError, match="canonical_image_invalid"):
        evidence.render_template_ocr_evidence(
            source,
            _job(source_sha256, layout_snapshot),
            tmp_path / "evidence.png",
        )

    assert load_calls == 0


def test_renderer_rejects_source_hash_mismatch_before_decode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "canonical.png"
    source.write_bytes(b"not-the-leased-canonical")
    expected_sha256 = "a" * 64
    layout_snapshot = {"question_crop_plan": _plan(expected_sha256)}
    monkeypatch.setattr(
        evidence.Image,
        "open",
        lambda *_args, **_kwargs: pytest.fail("image decode must not start"),
    )

    with pytest.raises(RuntimeError, match="canonical_image_hash_mismatch"):
        evidence.render_template_ocr_evidence(
            source,
            _job(expected_sha256, layout_snapshot),
            tmp_path / "evidence.png",
        )


def test_renderer_rejects_plan_that_leaves_horizontal_gap(tmp_path: Path) -> None:
    source_sha256 = "a" * 64
    plan = _plan(source_sha256)
    plan["parameters"]["columns"][1]["x1_1000"] = 331
    layout_snapshot = {"question_crop_plan": plan}

    with pytest.raises(RuntimeError, match="question_crop_plan_coverage_invalid"):
        evidence.render_template_ocr_evidence(
            tmp_path / "missing.png",
            _job(source_sha256, layout_snapshot),
            tmp_path / "evidence.png",
        )


def test_renderer_rejects_mismatched_column_count_before_decode(tmp_path: Path) -> None:
    source_sha256 = "a" * 64
    plan = _plan(source_sha256)
    plan["parameters"]["column_count"] = 2

    with pytest.raises(RuntimeError, match="question_crop_plan_invalid"):
        evidence.render_template_ocr_evidence(
            tmp_path / "missing.png",
            _job(source_sha256, {"question_crop_plan": plan}),
            tmp_path / "evidence.png",
        )


def test_renderer_rejects_out_of_order_columns_before_decode(tmp_path: Path) -> None:
    source_sha256 = "a" * 64
    plan = _plan(source_sha256)
    plan["parameters"]["columns"][0]["column_index"] = 2

    with pytest.raises(RuntimeError, match="question_crop_plan_coverage_invalid"):
        evidence.render_template_ocr_evidence(
            tmp_path / "missing.png",
            _job(source_sha256, {"question_crop_plan": plan}),
            tmp_path / "evidence.png",
        )


def test_renderer_rejects_damaged_legacy_plan_before_decode(tmp_path: Path) -> None:
    source_sha256 = "a" * 64
    plan = _legacy_plan(source_sha256)
    plan["blocks"][0]["bbox_1000"] = [0, 1, 512, 1000]

    with pytest.raises(RuntimeError, match="question_crop_plan_coverage_invalid"):
        evidence.render_template_ocr_evidence(
            tmp_path / "missing.png",
            _job(source_sha256, {"question_crop_plan": plan}),
            tmp_path / "evidence.png",
        )
