from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path
from typing import Sequence

from .core import (
    AlignmentError,
    LabConfig,
    TemplateMatcher,
    benchmark_artifact,
    compile_blank_template,
    compile_consensus_template,
    load_regions,
    load_verified_manifest,
    render_region_sheets,
    summarize_regions,
)
from .evaluation import evaluate_quality_gate


def _reject_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _config_for_preset(name: str) -> LabConfig:
    if name == "balanced":
        return LabConfig()
    if name == "fast":
        return LabConfig(max_edge=1600, feature_edge=800, max_features=3000)
    if name == "quality":
        return LabConfig(max_edge=2200, feature_edge=1280, max_features=5000)
    raise ValueError("unknown preprocessing preset")


def _expand_images(raw_values: Sequence[str]) -> list[Path]:
    """Expand explicit image paths and shell-independent glob patterns."""
    paths: list[Path] = []
    unmatched: list[str] = []

    for raw_value in raw_values:
        explicit = Path(raw_value).expanduser()
        if explicit.is_file():
            matches = [explicit]
        else:
            matches = [
                Path(match)
                for match in glob.glob(str(explicit), recursive=True)
                if Path(match).is_file()
            ]
        if not matches:
            unmatched.append(raw_value)
            continue
        paths.extend(matches)

    if unmatched:
        joined = ", ".join(unmatched)
        raise ValueError(f"image paths or globs matched no files: {joined}")

    unique: dict[str, Path] = {}
    for path in paths:
        resolved = path.resolve()
        unique.setdefault(str(resolved).casefold(), resolved)
    if not unique:
        raise ValueError("no image files were provided")
    return sorted(unique.values(), key=lambda path: (path.name.casefold(), str(path)))


def _write_json(path: str | Path, payload: dict) -> Path:
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return destination.resolve()


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _run_compile(args: argparse.Namespace) -> dict:
    images = _expand_images(args.images)
    output_dir = Path(args.output).expanduser().resolve()
    manifest = compile_consensus_template(
        images,
        output_dir,
        config=_config_for_preset(args.preset),
        rotate_clockwise_quadrants=args.rotate_clockwise,
    )
    return {
        "command": "compile",
        "manifest": str(output_dir / "manifest.json"),
        "source_count": manifest["source_count"],
        "canonical_size": manifest["canonical_size"],
        "preset": args.preset,
        "quality": manifest["quality"],
    }


def _run_compile_blank(args: argparse.Namespace) -> dict:
    output_dir = Path(args.output).expanduser().resolve()
    manifest = compile_blank_template(
        Path(args.image).expanduser().resolve(),
        output_dir,
        config=_config_for_preset(args.preset),
        rotate_clockwise_quadrants=args.rotate_clockwise,
    )
    return {
        "command": "compile-blank",
        "manifest": str(output_dir / "manifest.json"),
        "source_mode": manifest["source_mode"],
        "answer_preservation_safe": manifest["answer_preservation_safe"],
        "canonical_size": manifest["canonical_size"],
        "preset": args.preset,
        "quality": manifest["quality"],
    }


def _run_evidence(args: argparse.Namespace) -> dict:
    if bool(args.regions) != bool(args.sheets_output):
        raise ValueError("--regions and --sheets-output must be supplied together")

    image_path = Path(args.image).expanduser().resolve()
    manifest_path = Path(args.manifest).expanduser().resolve()
    output_dir = Path(args.output).expanduser().resolve()
    matcher = TemplateMatcher(
        manifest_path,
        allow_unsafe_consensus=args.allow_unsafe_consensus,
    )
    regions = load_regions(Path(args.regions).expanduser().resolve()) if args.regions else None
    if regions is not None:
        regions.require_artifact(matcher.manifest["registration_sha256"])
    evidence = matcher.extract_evidence(
        image_path,
        output_dir=output_dir,
        sample_id=args.sample_id,
    )

    safe_id = evidence.stats["sample_id"]
    summary = {
        "command": "evidence",
        "sample_id": safe_id,
        "source_mode": evidence.stats["source_mode"],
        "answer_preservation_safe": evidence.stats["answer_preservation_safe"],
        "artifact_registration_sha256": evidence.stats[
            "artifact_registration_sha256"
        ],
        "output_dir": str(output_dir),
        "aligned_image": str(output_dir / f"{safe_id}-aligned.png"),
        "ink_mask": str(output_dir / f"{safe_id}-ink.png"),
        "ambiguous_mask": str(output_dir / f"{safe_id}-ambiguous.png"),
        "evidence_image": str(output_dir / f"{safe_id}-evidence.png"),
        "diagnostics": str(output_dir / f"{safe_id}-diagnostics.json"),
        "alignment": {
            "inliers": evidence.stats.get("inliers"),
            "inlier_ratio": evidence.stats.get("inlier_ratio"),
            "reprojection_rmse": evidence.stats.get("reprojection_rmse"),
            "coverage": evidence.stats.get("coverage"),
            "template_edge_recall": evidence.stats.get("template_edge_recall"),
        },
        "ink_pixels": evidence.stats["ink_pixels"],
        "ambiguous_pixels": evidence.stats["ambiguous_pixels"],
        "component_count": evidence.stats["component_count"],
    }

    if regions is not None:
        sheet_paths = render_region_sheets(
            evidence,
            regions,
            Path(args.sheets_output).expanduser().resolve(),
        )
        summary["region_count"] = len(regions)
        summary["region_contract_sha256"] = regions.contract_sha256
        summary["question_evidence"] = summarize_regions(evidence, regions)
        summary["region_sheets"] = [str(path.resolve()) for path in sheet_paths]
    return summary


def _run_benchmark(args: argparse.Namespace) -> dict:
    images = _expand_images(args.images)
    report = benchmark_artifact(
        images,
        Path(args.manifest).expanduser().resolve(),
    )
    if args.output:
        report_path = Path(args.output).expanduser().resolve()
        report["report_path"] = str(report_path)
        _write_json(report_path, report)
    return report


def _run_quality_gate(args: argparse.Namespace) -> dict:
    source = Path(args.results).expanduser().resolve()
    try:
        payload = json.loads(
            source.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except json.JSONDecodeError as exc:
        raise ValueError("quality results must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("quality results must be a JSON object")
    manifest = load_verified_manifest(
        Path(args.manifest).expanduser().resolve()
    )
    regions = load_regions(Path(args.regions).expanduser().resolve())
    regions.require_artifact(manifest["registration_sha256"])
    actual_artifact = {
        "source_mode": manifest["source_mode"],
        "answer_preservation_safe": manifest["answer_preservation_safe"],
        "artifact_registration_sha256": manifest["registration_sha256"],
        "region_contract_sha256": regions.contract_sha256,
    }
    if payload.get("artifact") != actual_artifact:
        raise ValueError(
            "quality result artifact metadata does not match verified inputs"
        )
    result = evaluate_quality_gate(
        payload.get("samples"),
        artifact=actual_artifact,
        alignment=payload.get("alignment"),
        minimum_samples=args.minimum_samples,
        maximum_score_downward=args.maximum_score_downward,
    )
    if args.output:
        _write_json(args.output, result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="template-ocr-lab",
        description=(
            "Compile and evaluate template-guided student-handwriting evidence "
            "without importing production services."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    compile_parser = subparsers.add_parser(
        "compile",
        help="compile a consensus template from at least three same-layout papers",
    )
    compile_parser.add_argument(
        "--images",
        nargs="+",
        required=True,
        help="image paths or quoted glob patterns",
    )
    compile_parser.add_argument(
        "--output",
        required=True,
        help="directory for manifest.json, template.png, and print_mask.png",
    )
    compile_parser.add_argument(
        "--preset",
        choices=("balanced", "fast", "quality"),
        default="balanced",
        help="image/feature resolution preset (default: balanced)",
    )
    compile_parser.add_argument(
        "--rotate-clockwise",
        type=int,
        choices=range(4),
        default=0,
        metavar="{0,1,2,3}",
        help="clockwise quarter-turns applied to every input image (default: 0)",
    )
    compile_parser.set_defaults(handler=_run_compile)

    blank_parser = subparsers.add_parser(
        "compile-blank",
        help="compile an answer-preserving artifact from one verified blank paper",
    )
    blank_parser.add_argument("--image", required=True, help="electronic or photographed blank page")
    blank_parser.add_argument(
        "--output",
        required=True,
        help="directory for the compiled artifact",
    )
    blank_parser.add_argument(
        "--preset",
        choices=("balanced", "fast", "quality"),
        default="balanced",
        help="image/feature resolution preset (default: balanced)",
    )
    blank_parser.add_argument(
        "--rotate-clockwise",
        type=int,
        choices=range(4),
        default=0,
        metavar="{0,1,2,3}",
        help="clockwise quarter-turns applied to the blank image (default: 0)",
    )
    blank_parser.set_defaults(handler=_run_compile_blank)

    evidence_parser = subparsers.add_parser(
        "evidence",
        help="align one paper and extract conservative handwriting evidence",
    )
    evidence_parser.add_argument("--manifest", required=True, help="compiled manifest.json")
    evidence_parser.add_argument("--image", required=True, help="student-paper image")
    evidence_parser.add_argument("--output", required=True, help="diagnostic output directory")
    evidence_parser.add_argument("--sample-id", required=True, help="anonymous output identifier")
    evidence_parser.add_argument(
        "--allow-unsafe-consensus",
        action="store_true",
        help="research only: allow a student_consensus artifact",
    )
    evidence_parser.add_argument(
        "--regions",
        help="optional question-region JSON; requires --sheets-output",
    )
    evidence_parser.add_argument(
        "--sheets-output",
        help="optional region-sheet directory; requires --regions",
    )
    evidence_parser.set_defaults(handler=_run_evidence)

    benchmark_parser = subparsers.add_parser(
        "benchmark",
        help="measure alignment quality for a compiled artifact",
    )
    benchmark_parser.add_argument("--manifest", required=True, help="compiled manifest.json")
    benchmark_parser.add_argument(
        "--images",
        nargs="+",
        required=True,
        help="image paths or quoted glob patterns",
    )
    benchmark_parser.add_argument(
        "--output",
        help="optional JSON report path; the report is always printed",
    )
    benchmark_parser.set_defaults(handler=_run_benchmark)

    gate_parser = subparsers.add_parser(
        "quality-gate",
        help="evaluate isolated OCR results against production integration gates",
    )
    gate_parser.add_argument(
        "--results",
        required=True,
        help="JSON containing artifact, alignment, and anonymous sample metrics",
    )
    gate_parser.add_argument(
        "--manifest",
        required=True,
        help="verified template artifact manifest.json",
    )
    gate_parser.add_argument(
        "--regions",
        required=True,
        help="artifact-bound question-region JSON",
    )
    gate_parser.add_argument(
        "--minimum-samples",
        type=int,
        default=10,
        help="minimum anonymous papers required for this gate (default: 10)",
    )
    gate_parser.add_argument(
        "--maximum-score-downward",
        type=int,
        default=4,
        help="largest allowed per-paper score drop (default: 4)",
    )
    gate_parser.add_argument("--output", help="optional JSON report path")
    gate_parser.set_defaults(handler=_run_quality_gate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
    except (AlignmentError, FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")
    _print_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
