from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from experiments.template_ocr_lab import core


PAGE_HEIGHT = 900
PAGE_WIDTH = 700
ARTIFACT_HASH = "a" * 64


def _base_page() -> np.ndarray:
    page = np.full((PAGE_HEIGHT, PAGE_WIDTH, 3), 255, dtype=np.uint8)
    ink = (24, 24, 24)

    cv2.rectangle(page, (24, 24), (PAGE_WIDTH - 25, PAGE_HEIGHT - 25), ink, 3)
    cv2.putText(
        page,
        "FORM A7 2026",
        (72, 78),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.05,
        ink,
        2,
        cv2.LINE_AA,
    )
    cv2.rectangle(page, (45, 42), (62, 82), ink, -1)
    cv2.circle(page, (640, 63), 19, ink, 3)
    cv2.line(page, (627, 50), (653, 76), ink, 2, cv2.LINE_AA)
    cv2.line(page, (653, 50), (627, 76), ink, 2, cv2.LINE_AA)

    for index in range(8):
        top = 120 + index * 90
        coefficient = 3 * index + 2
        label = f"{index + 1}.  {coefficient}x + {index + 5} ="
        cv2.putText(
            page,
            label,
            (55, top + 38),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.62,
            ink,
            2,
            cv2.LINE_AA,
        )
        cv2.rectangle(page, (340, top), (640, top + 57), ink, 2)
        marker_x = 308 + (index % 3) * 5
        cv2.circle(page, (marker_x, top + 28), 3 + (index % 3), ink, -1)

    triangle = np.asarray([[48, 840], [72, 810], [96, 840]], dtype=np.int32)
    cv2.polylines(page, [triangle], True, ink, 3, cv2.LINE_AA)
    cv2.putText(
        page,
        "R9",
        (600, 846),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.75,
        ink,
        2,
        cv2.LINE_AA,
    )
    return page


def _answer_box(index: int) -> tuple[int, int, int, int]:
    top = 120 + index * 90
    return 360, top + 6, 625, top + 51


def _draw_black_answer(page: np.ndarray, index: int, text: str) -> None:
    _left, top, _right, bottom = _answer_box(index)
    cv2.putText(
        page,
        text,
        (375, bottom - 7),
        cv2.FONT_HERSHEY_SCRIPT_SIMPLEX,
        0.82,
        (8, 8, 8),
        3,
        cv2.LINE_8,
    )


def _save_rgb(path: Path, rgb: np.ndarray) -> None:
    Image.fromarray(rgb, mode="RGB").save(path, format="PNG")


def _edge_recall(reference: np.ndarray, candidate: np.ndarray) -> float:
    reference_edges = cv2.Canny(cv2.cvtColor(reference, cv2.COLOR_RGB2GRAY), 60, 150)
    candidate_edges = cv2.Canny(cv2.cvtColor(candidate, cv2.COLOR_RGB2GRAY), 60, 150)
    candidate_edges = cv2.dilate(candidate_edges, np.ones((5, 5), dtype=np.uint8))
    expected = reference_edges > 0
    return float(np.count_nonzero(expected & (candidate_edges > 0))) / float(
        np.count_nonzero(expected)
    )


def _lab_config() -> core.LabConfig:
    return core.LabConfig(
        max_edge=1000,
        feature_edge=800,
        detector="sift",
        max_features=4000,
        ratio_test=0.78,
        ransac_threshold=3.0,
        min_good_matches=18,
        min_inliers=12,
        min_inlier_ratio=0.30,
        min_coverage=0.72,
        local_tiles_x=1,
        consensus_percentile=70,
        print_vote_ratio=0.60,
        component_min_area=4,
    )


@dataclass(frozen=True)
class CompiledArtifact:
    directory: Path
    manifest_path: Path
    base_page: np.ndarray


@pytest.fixture(scope="module")
def compiled_artifact(tmp_path_factory: pytest.TempPathFactory) -> CompiledArtifact:
    root = tmp_path_factory.mktemp("template-ocr-lab")
    source_dir = root / "sources"
    source_dir.mkdir()
    source_paths: list[Path] = []

    base = _base_page()
    answers = ["-3", "1/2", "x=7", "(2,5)", "-11"]
    for index, answer in enumerate(answers):
        paper = base.copy()
        _draw_black_answer(paper, index, answer)
        path = source_dir / f"sample-{index + 1}.png"
        _save_rgb(path, paper)
        source_paths.append(path)

    artifact_dir = root / "artifact"
    core.compile_consensus_template(
        source_paths,
        artifact_dir,
        config=_lab_config(),
    )
    return CompiledArtifact(artifact_dir, artifact_dir / "manifest.json", base)


def test_aligns_a_rotated_perspective_page() -> None:
    reference = _base_page()
    height, width = reference.shape[:2]
    source_corners = np.float32(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]]
    )
    photographed_corners = np.float32(
        [[24, 19], [width - 34, 8], [width - 12, height - 28], [31, height - 10]]
    )
    camera_transform = cv2.getPerspectiveTransform(source_corners, photographed_corners)
    photographed = cv2.warpPerspective(
        reference,
        camera_transform,
        (width, height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255),
    )
    moving = np.ascontiguousarray(np.rot90(photographed, k=1))

    result = core.align_rgb(
        moving,
        reference,
        _lab_config(),
        try_rotations=(1,),
    )

    assert result.stats["rotation_clockwise_quadrants"] == 1
    assert result.stats["inliers"] >= 30
    assert result.stats["coverage"] >= 0.90
    assert _edge_recall(reference, result.rgb) >= 0.90


def test_local_refinement_rejects_extreme_affine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = _base_page()
    points = np.asarray(
        [
            (x, y)
            for x in (80, 160, 240, 460, 540, 620)
            for y in (120, 320, 520, 720)
        ],
        dtype=np.float32,
    )

    def extreme_affine(*_args, **_kwargs):
        matrix = np.asarray(
            [[1.0, 0.0, 300.0], [0.0, 1.0, 0.0]],
            dtype=np.float64,
        )
        mask = np.ones((12, 1), dtype=np.uint8)
        return matrix, mask

    monkeypatch.setattr(cv2, "estimateAffine2D", extreme_affine)
    config = _lab_config()
    config = core.replace(config, local_tiles_x=2)

    refined, diagnostics = core._tile_refine(
        image,
        np.eye(3, dtype=np.float64),
        points,
        points,
        config,
    )

    difference = np.abs(refined.astype(np.int16) - image.astype(np.int16))
    assert int(np.max(difference)) <= 1
    assert all(item["applied"] is False for item in diagnostics)
    assert all(
        item["rejection_reason"] == "affine_translation_out_of_bounds"
        for item in diagnostics
    )


def test_consensus_template_removes_one_off_handwriting(
    compiled_artifact: CompiledArtifact,
) -> None:
    manifest = core._verified_artifact(compiled_artifact.manifest_path)[0]
    assert manifest["source_mode"] == "student_consensus"
    assert manifest["answer_preservation_safe"] is False

    template = np.asarray(
        Image.open(compiled_artifact.directory / "template.png").convert("L")
    )
    print_mask = np.asarray(
        Image.open(compiled_artifact.directory / "print_mask.png").convert("L")
    )

    for index in range(5):
        left, top, right, bottom = _answer_box(index)
        interior = template[top + 3 : bottom - 3, left + 8 : right - 8]
        interior_mask = print_mask[top + 3 : bottom - 3, left + 8 : right - 8]
        assert float(np.mean(interior)) >= 248.0
        assert np.count_nonzero(interior < 220) <= 4
        assert np.count_nonzero(interior_mask) <= 4

    header_mask = print_mask[35:90, 40:665]
    assert np.count_nonzero(header_mask) >= 500


def test_consensus_compilation_has_a_bounded_research_sample_count(
    tmp_path: Path,
) -> None:
    paths = [
        tmp_path / f"sample-{index:02d}.png"
        for index in range(core.MAX_CONSENSUS_SOURCES + 1)
    ]

    with pytest.raises(ValueError, match="accepts at most"):
        core.compile_consensus_template(paths, tmp_path / "artifact")


def test_evidence_excludes_red_marks_and_keeps_black_additions(
    compiled_artifact: CompiledArtifact,
    tmp_path: Path,
) -> None:
    paper = compiled_artifact.base_page.copy()
    _draw_black_answer(paper, 5, "-7/3")
    red_left, red_top, _red_right, red_bottom = _answer_box(6)
    red = (225, 20, 20)
    cv2.line(
        paper,
        (red_left + 25, red_top + 20),
        (red_left + 55, red_bottom - 5),
        red,
        9,
        cv2.LINE_8,
    )
    cv2.line(
        paper,
        (red_left + 55, red_bottom - 5),
        (red_left + 130, red_top + 2),
        red,
        9,
        cv2.LINE_8,
    )
    sample_path = tmp_path / "evidence-source.png"
    _save_rgb(sample_path, paper)

    result = core.extract_evidence(
        sample_path,
        compiled_artifact.manifest_path,
        allow_unsafe_consensus=True,
    )

    black_left, black_top, black_right, black_bottom = _answer_box(5)
    black_crop = result.ink_mask[black_top:black_bottom, black_left:black_right]
    red_crop = result.ink_mask[red_top:red_bottom, red_left : red_left + 155]
    assert np.count_nonzero(black_crop) >= 120
    assert np.count_nonzero(red_crop) == 0
    assert result.stats["component_count"] >= 1


def test_blue_evidence_requires_new_chroma_relative_to_blank() -> None:
    reference = np.full((180, 260, 3), 255, dtype=np.uint8)
    blue = (20, 65, 175)
    black = (20, 20, 20)
    cv2.line(reference, (20, 45), (240, 45), blue, 6, cv2.LINE_8)
    cv2.line(reference, (20, 105), (240, 105), black, 4, cv2.LINE_8)
    template_gray = core._clean_gray_for_consensus(reference)
    print_mask = (template_gray <= 210).astype(np.uint8) * 255
    config = _lab_config()

    identical = core._ink_evidence(
        reference,
        template_gray,
        print_mask,
        reference,
        config,
    )[1]
    assert np.count_nonzero(identical) == 0

    student = reference.copy()
    cv2.line(student, (130, 75), (130, 140), blue, 6, cv2.LINE_8)
    added = core._ink_evidence(
        student,
        template_gray,
        print_mask,
        reference,
        config,
    )[1]
    assert np.count_nonzero(added[75:141, 125:136]) >= 250
    assert np.count_nonzero(added[40:51, 20:241]) == 0


@pytest.mark.parametrize("shift_x", [-2, -1, 1, 2])
def test_shifted_template_blue_is_not_new_student_ink(
    shift_x: int,
) -> None:
    reference = np.full((180, 260, 3), 255, dtype=np.uint8)
    blue = (20, 65, 175)
    cv2.line(reference, (20, 80), (240, 80), blue, 6, cv2.LINE_8)
    template_gray = core._clean_gray_for_consensus(reference)
    print_mask = (template_gray <= 210).astype(np.uint8) * 255
    shifted = cv2.warpAffine(
        reference,
        np.asarray([[1.0, 0.0, shift_x], [0.0, 1.0, 0.0]], dtype=np.float32),
        (260, 180),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255),
    )

    automatic = core._ink_evidence(
        shifted,
        template_gray,
        print_mask,
        reference,
        _lab_config(),
    )[1]

    assert np.count_nonzero(automatic) <= 10


@pytest.mark.parametrize("shift_x", [-2, -1, 1, 2])
def test_near_print_registration_residual_is_not_automatic_ink(
    shift_x: int,
) -> None:
    reference = _base_page()
    template_gray = core._clean_gray_for_consensus(reference)
    print_mask = (template_gray <= 210).astype(np.uint8) * 255
    shifted = cv2.warpAffine(
        reference,
        np.asarray([[1.0, 0.0, shift_x], [0.0, 1.0, 0.0]], dtype=np.float32),
        (PAGE_WIDTH, PAGE_HEIGHT),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255),
    )

    result = core._ink_evidence(
        shifted,
        template_gray,
        print_mask,
        reference,
        _lab_config(),
    )

    assert np.count_nonzero(result[1]) <= 20
    assert np.count_nonzero(result[2]) >= 100


def test_region_summary_distinguishes_visible_and_blank_answers(
    compiled_artifact: CompiledArtifact,
    tmp_path: Path,
) -> None:
    paper = compiled_artifact.base_page.copy()
    _draw_black_answer(paper, 5, "-7/3")
    sample_path = tmp_path / "region-source.png"
    _save_rgb(sample_path, paper)
    evidence = core.extract_evidence(
        sample_path,
        compiled_artifact.manifest_path,
        allow_unsafe_consensus=True,
    )

    def region(number: str, index: int) -> core.QuestionRegion:
        left, top, right, bottom = _answer_box(index)
        return core.QuestionRegion(
            number,
            "fill",
            (
                round(left * 1000 / PAGE_WIDTH),
                round(top * 1000 / PAGE_HEIGHT),
                round(right * 1000 / PAGE_WIDTH),
                round(bottom * 1000 / PAGE_HEIGHT),
            ),
        )

    summaries = core.summarize_regions(
        evidence,
        core.RegionSet(
            evidence.stats["artifact_registration_sha256"],
            (region("6", 5), region("7", 6)),
        ),
    )

    assert summaries[0]["state"] == "visible"
    assert summaries[0]["ink_pixels"] >= 120
    assert summaries[1]["state"] == "blank"
    assert summaries[1]["ink_pixels"] <= _lab_config().blank_pixel_limit


@pytest.mark.parametrize("long_edge", [1600, 1800, 2200])
def test_ink_state_thresholds_scale_with_preset_area(long_edge: int) -> None:
    config = core.LabConfig()
    scale = (long_edge / core.REFERENCE_MAX_EDGE) ** 2

    visible_pixels = int(round(30 * scale))
    uncertain_pixels = int(round(10 * scale))

    assert (
        core._region_state(
            visible_pixels,
            config,
            (long_edge, round(long_edge * 0.75)),
        )
        == "visible"
    )
    assert (
        core._region_state(
            uncertain_pixels,
            config,
            (long_edge, round(long_edge * 0.75)),
        )
        == "uncertain"
    )


def test_region_summary_rejects_missing_preprocessing_config() -> None:
    evidence = core.EvidenceResult(
        aligned_rgb=np.full((20, 20, 3), 255, dtype=np.uint8),
        cleaned_gray=np.full((20, 20), 255, dtype=np.uint8),
        ink_mask=np.zeros((20, 20), dtype=np.uint8),
        ambiguous_mask=np.zeros((20, 20), dtype=np.uint8),
        evidence_rgb=np.full((20, 20, 3), 255, dtype=np.uint8),
        stats={"artifact_registration_sha256": ARTIFACT_HASH},
    )
    region = core.QuestionRegion("1", "fill", (0, 0, 1000, 1000))
    regions = core.RegionSet(ARTIFACT_HASH, (region,))

    with pytest.raises(ValueError, match="missing its preprocessing config"):
        core.summarize_regions(evidence, regions)


def test_ambiguous_near_print_evidence_is_never_called_blank() -> None:
    ambiguous = np.zeros((40, 40), dtype=np.uint8)
    ambiguous[15:20, 15:20] = 255
    evidence = core.EvidenceResult(
        aligned_rgb=np.full((40, 40, 3), 255, dtype=np.uint8),
        cleaned_gray=np.full((40, 40), 255, dtype=np.uint8),
        ink_mask=np.zeros((40, 40), dtype=np.uint8),
        ambiguous_mask=ambiguous,
        evidence_rgb=np.full((40, 40, 3), 255, dtype=np.uint8),
        stats={
            "artifact_registration_sha256": ARTIFACT_HASH,
            "config": asdict(core.LabConfig()),
        },
    )
    region = core.QuestionRegion("1", "fill", (0, 0, 1000, 1000))
    regions = core.RegionSet(ARTIFACT_HASH, (region,))

    summary = core.summarize_regions(evidence, regions)[0]

    assert summary["ink_pixels"] == 0
    assert summary["ambiguous_pixels"] == 25
    assert summary["state"] == "uncertain"


def test_region_loader_rejects_overlapping_ownership(
    tmp_path: Path,
) -> None:
    region_path = tmp_path / "overlap.json"
    region_path.write_text(
        json.dumps({
            "artifact_registration_sha256": ARTIFACT_HASH,
            "questions": [
                {
                    "question_number": "1",
                    "question_type": "choice",
                    "ownership_bbox": [100, 100, 500, 300],
                },
                {
                    "question_number": "2",
                    "question_type": "fill",
                    "ownership_bbox": [450, 200, 800, 400],
                },
            ]
        }),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="ownership regions overlap"):
        core.load_regions(region_path)


@pytest.mark.parametrize(
    "questions",
    [
        [],
        ["not an object"],
        [
            {
                "question_number": "1",
                "question_type": "fill",
                "ownership_bbox": [100.5, 100, 500, 300],
            }
        ],
    ],
)
def test_region_loader_rejects_ambiguous_schema(
    questions: list,
    tmp_path: Path,
) -> None:
    region_path = tmp_path / "invalid.json"
    region_path.write_text(
        json.dumps({
            "artifact_registration_sha256": ARTIFACT_HASH,
            "questions": questions,
        }),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        core.load_regions(region_path)


def test_region_loader_requires_explicit_type_and_one_bbox(
    tmp_path: Path,
) -> None:
    region_path = tmp_path / "ambiguous-fields.json"
    region_path.write_text(
        json.dumps({
            "artifact_registration_sha256": ARTIFACT_HASH,
            "questions": [
                {
                    "question_number": "1",
                    "ownership_bbox": [100, 100, 500, 300],
                    "bbox": [500, 300, 800, 500],
                }
            ],
        }),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing question_type"):
        core.load_regions(region_path)

    payload = json.loads(region_path.read_text(encoding="utf-8"))
    payload["questions"][0]["question_type"] = "fill"
    region_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="exactly one bbox field"):
        core.load_regions(region_path)


@pytest.mark.parametrize(
    "number_fields",
    [
        {"question_number": 1},
        {"question_number": "1", "question_no": "2"},
        {"question_number": " 1 "},
    ],
)
def test_region_loader_rejects_ambiguous_question_numbers(
    number_fields: dict,
    tmp_path: Path,
) -> None:
    region_path = tmp_path / "invalid-number.json"
    region_path.write_text(
        json.dumps({
            "artifact_registration_sha256": ARTIFACT_HASH,
            "questions": [
                {
                    **number_fields,
                    "question_type": "fill",
                    "ownership_bbox": [100, 100, 500, 300],
                }
            ],
        }),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        core.load_regions(region_path)


def test_touching_region_boundaries_map_to_the_same_pixel() -> None:
    left = core.QuestionRegion("1", "fill", (0, 0, 500, 1000))
    right = core.QuestionRegion("2", "fill", (500, 0, 1000, 1000))

    left_pixels = core._pixel_bbox(left, 701, 503, 0.0)
    right_pixels = core._pixel_bbox(right, 701, 503, 0.0)

    assert left_pixels[2] == right_pixels[0]
    assert left_pixels == (0, 0, 351, 503)
    assert right_pixels == (351, 0, 701, 503)


def test_region_sheet_padding_never_copies_adjacent_ink(
    tmp_path: Path,
) -> None:
    ink_mask = np.zeros((80, 160), dtype=np.uint8)
    ink_mask[25:45, 100:125] = 255
    evidence = core.EvidenceResult(
        aligned_rgb=np.full((80, 160, 3), 255, dtype=np.uint8),
        cleaned_gray=np.full((80, 160), 255, dtype=np.uint8),
        ink_mask=ink_mask,
        ambiguous_mask=np.zeros_like(ink_mask),
        evidence_rgb=np.full((80, 160, 3), 255, dtype=np.uint8),
        stats={
            "artifact_registration_sha256": ARTIFACT_HASH,
            "config": asdict(core.LabConfig()),
        },
    )
    regions = core.RegionSet(
        ARTIFACT_HASH,
        (
            core.QuestionRegion("1", "fill", (0, 0, 500, 1000)),
            core.QuestionRegion("2", "fill", (500, 0, 1000, 1000)),
        ),
    )

    sheet_path = core.render_region_sheets(
        evidence,
        regions,
        tmp_path,
        group_size=2,
        pad_ratio=0.25,
    )[0]
    sheet = np.asarray(Image.open(sheet_path).convert("L"))

    assert np.count_nonzero(sheet[:240, 170:] < 100) == 0
    assert np.count_nonzero(sheet[240:, 170:] < 100) > 0


def test_verified_blank_artifact_preserves_new_black_ink(
    tmp_path: Path,
) -> None:
    blank_path = tmp_path / "blank.png"
    _save_rgb(blank_path, _base_page())
    artifact_dir = tmp_path / "blank-artifact"
    manifest = core.compile_blank_template(
        blank_path,
        artifact_dir,
        config=_lab_config(),
    )

    paper = _base_page()
    _draw_black_answer(paper, 4, "4")
    red_left, red_top, _red_right, red_bottom = _answer_box(5)
    cv2.line(
        paper,
        (red_left + 20, red_bottom - 5),
        (red_left + 110, red_top + 4),
        (225, 20, 20),
        8,
        cv2.LINE_8,
    )
    paper_path = tmp_path / "filled.png"
    _save_rgb(paper_path, paper)
    result = core.extract_evidence(paper_path, artifact_dir / "manifest.json")

    black_left, black_top, black_right, black_bottom = _answer_box(4)
    black_crop = result.ink_mask[
        black_top:black_bottom,
        black_left:black_right,
    ]
    red_crop = result.ink_mask[
        red_top:red_bottom,
        red_left : red_left + 140,
    ]
    assert manifest["source_mode"] == "verified_blank"
    assert manifest["answer_preservation_safe"] is True
    assert manifest["config"] == asdict(_lab_config())
    loaded_config = core._verified_artifact(artifact_dir / "manifest.json")[4]
    assert loaded_config == _lab_config()
    assert np.count_nonzero(black_crop) >= 40
    assert np.count_nonzero(red_crop) == 0


def test_batch_matcher_reuses_registration_features(
    compiled_artifact: CompiledArtifact,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "batch-source.png"
    _save_rgb(source_path, compiled_artifact.base_page)
    original = core._extract_features
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(core, "_extract_features", counted)
    matcher = core.TemplateMatcher(
        compiled_artifact.manifest_path,
        allow_unsafe_consensus=True,
    )
    matcher.extract_evidence(source_path)
    matcher.extract_evidence(source_path)

    assert calls == 3


@pytest.mark.parametrize("sample_id", ["A/B", "AB ", "陈", "", "a" * 65])
def test_sample_id_is_rejected_instead_of_lossily_filtered(sample_id: str) -> None:
    with pytest.raises(ValueError, match="sample_id must be"):
        core.validate_sample_id(sample_id)


def test_evidence_rejects_low_template_edge_recall(
    compiled_artifact: CompiledArtifact,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "low-edge-recall.png"
    _save_rgb(source_path, compiled_artifact.base_page)
    monkeypatch.setattr(core, "_template_edge_recall", lambda *_args: 0.10)
    matcher = core.TemplateMatcher(
        compiled_artifact.manifest_path,
        allow_unsafe_consensus=True,
    )

    with pytest.raises(core.AlignmentError, match="edge recall is only"):
        matcher.extract_evidence(source_path)


def test_consensus_artifact_requires_explicit_research_opt_in(
    compiled_artifact: CompiledArtifact,
) -> None:
    with pytest.raises(ValueError, match="explicit research opt-in"):
        core.TemplateMatcher(compiled_artifact.manifest_path)


def test_rejects_a_tampered_template_artifact(
    compiled_artifact: CompiledArtifact,
    tmp_path: Path,
) -> None:
    tampered_dir = tmp_path / "tampered-artifact"
    shutil.copytree(compiled_artifact.directory, tampered_dir)
    template_path = tampered_dir / "template.png"
    template_path.write_bytes(template_path.read_bytes() + b"tampered")

    sample_path = tmp_path / "sample.png"
    _save_rgb(sample_path, compiled_artifact.base_page)
    with pytest.raises(ValueError, match="template image hash mismatch"):
        core.extract_evidence(sample_path, tampered_dir / "manifest.json")


def test_rejects_an_artifact_with_incomplete_config(
    compiled_artifact: CompiledArtifact,
    tmp_path: Path,
) -> None:
    incomplete_dir = tmp_path / "incomplete-config-artifact"
    shutil.copytree(compiled_artifact.directory, incomplete_dir)
    manifest_path = incomplete_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["config"].pop("feature_edge")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="config fields do not match"):
        core._verified_artifact(manifest_path)
