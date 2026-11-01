from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from experiments.template_ocr_lab import cli
from experiments.template_ocr_lab import core
from experiments.template_ocr_lab.cli import _config_for_preset, build_parser


@pytest.mark.parametrize(
    ("preset", "max_edge", "feature_edge", "max_features"),
    [
        ("balanced", 1800, 960, 3500),
        ("fast", 1600, 800, 3000),
        ("quality", 2200, 1280, 5000),
    ],
)
def test_preprocessing_presets_are_stable(
    preset: str,
    max_edge: int,
    feature_edge: int,
    max_features: int,
) -> None:
    config = _config_for_preset(preset)

    assert config.max_edge == max_edge
    assert config.feature_edge == feature_edge
    assert config.max_features == max_features
    config.validate()


@pytest.mark.parametrize(
    "command",
    [
        ["compile", "--images", "sample.png", "--output", "artifact"],
        ["compile-blank", "--image", "blank.png", "--output", "artifact"],
    ],
)
def test_template_compilers_default_to_balanced(command: list[str]) -> None:
    args = build_parser().parse_args(command)

    assert args.preset == "balanced"


@pytest.mark.parametrize(
    ("command", "compiler_name"),
    [
        (
            ["compile", "--images", "{source}", "--output", "{output}"],
            "compile_consensus_template",
        ),
        (
            ["compile-blank", "--image", "{source}", "--output", "{output}"],
            "compile_blank_template",
        ),
    ],
)
def test_selected_preset_reaches_template_compiler(
    command: list[str],
    compiler_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "sample.png"
    source.write_bytes(b"only checked for existence")
    output = tmp_path / "artifact"
    captured = {}

    def fake_compiler(*_args, config, rotate_clockwise_quadrants):
        captured["config"] = config
        captured["rotation"] = rotate_clockwise_quadrants
        if compiler_name == "compile_consensus_template":
            return {
                "source_count": 1,
                "canonical_size": [100, 100],
                "quality": {},
            }
        return {
            "source_mode": "verified_blank",
            "answer_preservation_safe": True,
            "canonical_size": [100, 100],
            "quality": {},
        }

    monkeypatch.setattr(cli, compiler_name, fake_compiler)
    formatted = [
        value.format(source=source, output=output)
        for value in command
    ]
    args = build_parser().parse_args([*formatted, "--preset", "fast"])

    result = args.handler(args)

    assert captured["config"] == _config_for_preset("fast")
    assert captured["rotation"] == 0
    assert result["preset"] == "fast"


def test_quality_gate_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    results = tmp_path / "duplicate.json"
    results.write_text(
        '{"artifact":{},"artifact":{},"alignment":{},"samples":[]}',
        encoding="utf-8",
    )
    args = build_parser().parse_args([
        "quality-gate",
        "--results",
        str(results),
        "--manifest",
        str(tmp_path / "manifest.json"),
        "--regions",
        str(tmp_path / "regions.json"),
    ])

    with pytest.raises(ValueError, match="duplicate JSON key"):
        args.handler(args)


def test_quality_gate_binds_verified_artifact_and_regions(
    tmp_path: Path,
) -> None:
    blank_path = tmp_path / "blank.png"
    Image.new("RGB", (800, 1000), "white").save(blank_path)
    artifact_dir = tmp_path / "artifact"
    manifest = core.compile_blank_template(blank_path, artifact_dir)

    regions_path = tmp_path / "regions.json"
    regions_path.write_text(
        json.dumps({
            "artifact_registration_sha256": manifest["registration_sha256"],
            "questions": [
                {
                    "question_number": "1",
                    "question_type": "fill",
                    "ownership_bbox": [100, 100, 900, 900],
                }
            ],
        }),
        encoding="utf-8",
    )
    regions = core.load_regions(regions_path)
    results_path = tmp_path / "results.json"
    results_path.write_text(
        json.dumps({
            "artifact": {
                "source_mode": "verified_blank",
                "answer_preservation_safe": True,
                "artifact_registration_sha256": manifest[
                    "registration_sha256"
                ],
                "region_contract_sha256": regions.contract_sha256,
            },
            "alignment": {
                "artifact_registration_sha256": manifest[
                    "registration_sha256"
                ],
                "alignment_pass_rate": 1.0,
                "min_template_edge_recall": 0.90,
            },
            "samples": [
                {
                    "sample_id": "S01",
                    "run_id": "run-01",
                    "artifact_registration_sha256": manifest[
                        "registration_sha256"
                    ],
                    "region_contract_sha256": regions.contract_sha256,
                    "expected_score": 0,
                    "predicted_score": 0,
                    "question_count": 1,
                    "review_count": 0,
                    "transcription_evaluated_count": 1,
                    "transcription_correct_count": 1,
                    "question_number_shift_count": 0,
                    "silent_uplift_count": 0,
                    "ocr_contract_failure_count": 0,
                }
            ],
        }),
        encoding="utf-8",
    )
    args = build_parser().parse_args([
        "quality-gate",
        "--results",
        str(results_path),
        "--manifest",
        str(artifact_dir / "manifest.json"),
        "--regions",
        str(regions_path),
        "--minimum-samples",
        "1",
    ])

    result = args.handler(args)

    assert result["gate_pass"] is True
    assert result["metrics"]["sample_count"] == 1
