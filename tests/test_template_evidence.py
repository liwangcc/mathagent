import copy
import hashlib
import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")
from PIL import Image

import services.template_evidence as template_evidence
from services.template_anchor_crops import build_anchor_crop_plan
from services.template_evidence import (
    EvidenceResult,
    TemplateEvidenceError,
    TemplateEvidenceMatcher,
    accept_canonical_template_candidate,
    build_focus_evidence_sheet_layout,
    build_multi_evidence_from_aligned,
    build_template_artifact,
    canonicalize_template_image,
    cleanup_evidence,
    materialize_template_difference_artifact,
    normalize_answer_regions,
    verify_template_artifacts,
)
from services import template_evidence_artifacts
from services import template_evidence_sheets
from services import template_evidence_columns


QUESTIONS = [
    {
        "question_number": "1",
        "question_type": "choice",
        "answer_bbox": [650, 130, 930, 260],
    },
    {
        "question_no": "2",
        "question_type": "fill",
        "answer_bbox": [600, 540, 940, 680],
    },
]


def _question_crop_plan(artifact_sha256: str) -> dict:
    return build_anchor_crop_plan(
        {
            "column_layout": {
                "column_count": 2,
                "columns": [
                    {"column_index": 1, "x1_1000": 0, "x2_1000": 500},
                    {"column_index": 2, "x1_1000": 500, "x2_1000": 1000},
                ],
            },
        },
        artifact_sha256=artifact_sha256,
        expected_question_numbers=["1", "2", "3", "4"],
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _synthetic_template(path: Path, seed: int = 42) -> np.ndarray:
    rng = np.random.default_rng(seed)
    image = np.full((700, 1000, 3), 255, dtype=np.uint8)
    for _ in range(180):
        x = int(rng.integers(20, 980))
        y = int(rng.integers(20, 680))
        radius = int(rng.integers(2, 9))
        shade = int(rng.integers(0, 130))
        cv2.circle(image, (x, y), radius, (shade, shade, shade), -1)
    for row, text in enumerate(("PAPER A 137", "QUESTION 2468", "LAYOUT 97531")):
        cv2.putText(
            image,
            text,
            (35, 90 + row * 220),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.6,
            (0, 0, 0),
            3,
            cv2.LINE_AA,
        )
    cv2.rectangle(image, (650, 91), (930, 182), (0, 0, 0), 2)
    cv2.line(image, (600, 430), (940, 430), (0, 0, 0), 2)
    Image.fromarray(image, mode="RGB").save(path)
    return image


def _perspective_student(template: np.ndarray, path: Path) -> np.ndarray:
    marked = template.copy()
    cv2.putText(
        marked,
        "C",
        (730, 165),
        cv2.FONT_HERSHEY_SIMPLEX,
        2.0,
        (20, 20, 20),
        5,
        cv2.LINE_AA,
    )
    cv2.putText(
        marked,
        "-7",
        (700, 455),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.8,
        (230, 20, 20),
        5,
        cv2.LINE_AA,
    )
    source = np.float32([[0, 0], [999, 0], [999, 699], [0, 699]])
    target = np.float32([[70, 45], [1050, 20], [1090, 765], [35, 790]])
    transform = cv2.getPerspectiveTransform(source, target)
    student = cv2.warpPerspective(
        marked,
        transform,
        (1140, 820),
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255),
    )
    Image.fromarray(student, mode="RGB").save(path)
    return transform


def _template_photo(page: np.ndarray, path: Path) -> np.ndarray:
    source = np.float32([
        [0, 0],
        [page.shape[1] - 1, 0],
        [page.shape[1] - 1, page.shape[0] - 1],
        [0, page.shape[0] - 1],
    ])
    page_quad = np.float32([[150, 90], [1040, 145], [980, 870], [75, 805]])
    transform = cv2.getPerspectiveTransform(source, page_quad)
    canvas = np.full((940, 1120, 3), 38, dtype=np.uint8)
    photographed = cv2.warpPerspective(
        page,
        transform,
        (canvas.shape[1], canvas.shape[0]),
        dst=canvas,
        borderMode=cv2.BORDER_TRANSPARENT,
    )
    Image.fromarray(photographed, mode="RGB").save(path)
    return page_quad


def test_normalize_answer_regions_canonicalizes_aliases_and_rejects_bad_data():
    assert normalize_answer_regions(QUESTIONS) == [
        {
            "question_number": "1",
            "question_type": "choice",
            "answer_bbox": [650, 130, 930, 260],
        },
        {
            "question_number": "2",
            "question_type": "fill",
            "answer_bbox": [600, 540, 940, 680],
        },
    ]

    invalid_values = [
        [{"question_number": "1", "question_type": "essay", "answer_bbox": [0, 0, 1, 1]}],
        [{"question_number": "1", "question_type": "fill", "answer_bbox": [0, 0, 0, 1]}],
        [{"question_number": "1", "question_type": "fill", "answer_bbox": [0, 0, 1.0, 1]}],
        [{"question_number": "1", "question_type": "fill", "answer_bbox": [0, 0, True, 1]}],
        [
            {"question_number": "1", "question_type": "fill", "answer_bbox": [0, 0, 10, 10]},
            {"question_no": "1", "question_type": "solve", "answer_bbox": [0, 10, 10, 20]},
        ],
    ]
    for value in invalid_values:
        with pytest.raises(ValueError):
            normalize_answer_regions(value)


def test_normalize_answer_regions_bounds_count_and_normalized_area():
    two_hundred_regions = [
        {
            "question_number": str(row * 20 + column + 1),
            "question_type": "fill",
            "answer_bbox": [
                column * 50,
                row * 100,
                (column + 1) * 50,
                (row + 1) * 100,
            ],
        }
        for row in range(10)
        for column in range(20)
    ]

    assert len(normalize_answer_regions(two_hundred_regions)) == 200
    with pytest.raises(ValueError, match="at most 200"):
        normalize_answer_regions([
            *two_hundred_regions,
            {
                "question_number": "201",
                "question_type": "fill",
                "answer_bbox": [0, 0, 1, 1],
            },
        ])

    assert normalize_answer_regions([{
        "question_number": "1",
        "question_type": "solve",
        "answer_bbox": [0, 0, 1000, 1000],
    }])

    with pytest.raises(ValueError, match="maximum total normalized area"):
        normalize_answer_regions([
            {
                "question_number": str(index),
                "question_type": "solve",
                "answer_bbox": [0, 0, 1000, 600],
            }
            for index in range(1, 4)
        ])


def test_normalize_answer_regions_rejects_overlap_amplification():
    allowed = [
        {
            "question_number": "1",
            "question_type": "fill",
            "answer_bbox": [0, 0, 200, 200],
        },
        {
            "question_number": "2",
            "question_type": "fill",
            "answer_bbox": [150, 0, 350, 200],
        },
        {
            "question_number": "3",
            "question_type": "fill",
            "answer_bbox": [350, 0, 450, 200],
        },
    ]

    assert len(normalize_answer_regions(allowed)) == 3
    rejected = copy.deepcopy(allowed[:2])
    rejected[1]["answer_bbox"] = [149, 0, 349, 200]
    with pytest.raises(ValueError, match="overlap more than 25%"):
        normalize_answer_regions(rejected)


def test_answer_analysis_streams_boxes_without_materializing_all_crops(
    monkeypatch,
):
    def forbid_bulk_crops(*_args, **_kwargs):
        raise AssertionError("high-level evidence paths must stream crop boxes")

    monkeypatch.setattr(template_evidence, "_answer_crops", forbid_bulk_crops)
    image = np.full((300, 400, 3), 255, dtype=np.uint8)
    regions = normalize_answer_regions([
        {
            "question_number": "1",
            "question_type": "fill",
            "answer_bbox": [0, 0, 400, 300],
        },
        {
            "question_number": "2",
            "question_type": "fill",
            "answer_bbox": [600, 700, 1000, 1000],
        },
    ])

    blanks, counts = template_evidence._confident_blank_questions(
        image,
        image.copy(),
        regions,
    )
    evidence = template_evidence._build_evidence_table(image, regions)

    assert blanks == ["1", "2"]
    assert counts == {"1": 0, "2": 0}
    assert evidence.shape == (160, 1800, 3)


def test_build_and_verify_artifact_normalizes_exif_and_bounds_size(tmp_path):
    source = tmp_path / "rotated.jpg"
    image = Image.new("RGB", (2300, 1150), "white")
    exif = Image.Exif()
    exif[274] = 6
    image.save(source, exif=exif)

    template_dir = tmp_path / "template"
    artifact = build_template_artifact(source, template_dir, 2, QUESTIONS)

    assert artifact["version"] == 1
    assert artifact["template_image"] == "artifacts/page_002/template.png"
    output = template_dir / artifact["template_image"]
    assert output.is_file()
    assert (artifact["width"], artifact["height"]) == (1100, 2200)
    assert artifact["sha256"] == _sha256(output)
    assert artifact["blank_detection_enabled"] is True
    assert verify_template_artifacts(template_dir, {"page_3": artifact}) == {
        "page_3": artifact
    }


def test_canonical_template_rectifies_page_and_binds_source_geometry(tmp_path):
    flat_path = tmp_path / "flat.png"
    flat = _synthetic_template(flat_path)
    source = tmp_path / "photo.png"
    _template_photo(flat, source)
    template_dir = tmp_path / "template"

    canonical = canonicalize_template_image(source, template_dir, 0)
    artifact = build_template_artifact(
        canonical.image_path,
        template_dir,
        0,
        [],
        canonicalization=canonical.canonicalization,
    )
    verified = verify_template_artifacts(template_dir, {"page_1": artifact})
    metadata = verified["page_1"]["canonicalization"]

    assert metadata["version"] == "paper-canonical-v1"
    assert metadata["detection_method"] == "detected_quadrilateral"
    assert metadata["confidence"] >= 0.68
    assert metadata["source_sha256"] == _sha256(
        template_dir / metadata["source_image"]
    )
    assert metadata["canonical_sha256"] == artifact["sha256"]
    assert (metadata["canonical_width"], metadata["canonical_height"]) == (
        artifact["width"], artifact["height"]
    )

    matrix = np.asarray(metadata["source_to_canonical"], dtype=np.float64)
    detected_quad = np.asarray(metadata["detected_page_quad"], dtype=np.float64)
    projected = cv2.perspectiveTransform(
        detected_quad.astype(np.float32).reshape(-1, 1, 2),
        matrix,
    ).reshape(-1, 2)
    margin = metadata["page_margin_px"]
    expected = np.float32([
        [margin, margin],
        [artifact["width"] - margin - 1, margin],
        [artifact["width"] - margin - 1, artifact["height"] - margin - 1],
        [margin, artifact["height"] - margin - 1],
    ])
    assert np.max(np.abs(projected - expected)) < 1.0

    repeated = canonicalize_template_image(source, template_dir, 0)
    assert _sha256(Path(repeated.image_path)) == artifact["sha256"]
    assert repeated.canonicalization["source_to_canonical"] == metadata[
        "source_to_canonical"
    ]


def test_canonical_template_accepts_phone_mpo_jpeg_primary_frame(tmp_path):
    source = tmp_path / "phone-template.jpg"
    flat_path = tmp_path / "flat.png"
    primary = Image.fromarray(_synthetic_template(flat_path), mode="RGB")
    secondary = Image.new("RGB", primary.size, "black")
    primary.save(
        source,
        format="MPO",
        save_all=True,
        append_images=[secondary],
    )

    canonical = canonicalize_template_image(source, tmp_path / "template", 0)

    assert Path(canonical.image_path).is_file()
    assert canonical.canonicalization["source_format"] == "jpeg"


def test_server_rejects_canonical_bytes_that_do_not_match_declared_hash(
    tmp_path,
    monkeypatch,
):
    worker_root = Path(__file__).resolve().parents[1] / "desktop" / "worker"
    monkeypatch.syspath_prepend(str(worker_root))
    from mathagent_worker.canonicalization import canonicalize_template

    flat_path = tmp_path / "flat.png"
    flat = _synthetic_template(flat_path)
    workspace = tmp_path / "desktop-workspace"
    workspace.mkdir()
    source = workspace / "source.png"
    _template_photo(flat, source)
    canonical_path = workspace / "canonical.png"
    desktop_result = canonicalize_template(workspace, source, canonical_path)

    with Image.open(canonical_path) as image:
        tampered = np.asarray(image.convert("RGB"), dtype=np.uint8).copy()
    center_y = tampered.shape[0] // 2
    center_x = tampered.shape[1] // 2
    tampered[center_y - 75:center_y + 75, center_x - 75:center_x + 75] = 0
    tampered_path = workspace / "tampered.png"
    Image.fromarray(tampered, mode="RGB").save(tampered_path, format="PNG")
    metadata = dict(desktop_result.canonicalization)

    with pytest.raises(ValueError, match="canonical_sha256 mismatch"):
        accept_canonical_template_candidate(
            source,
            tampered_path,
            metadata,
            tmp_path / "server-template",
            0,
        )


def test_student_alignment_reuses_canonical_answer_coordinates(tmp_path):
    flat_path = tmp_path / "flat.png"
    flat = _synthetic_template(flat_path)
    teacher_photo = tmp_path / "teacher-photo.png"
    teacher_quad = _template_photo(flat, teacher_photo)
    template_dir = tmp_path / "template"
    canonical = canonicalize_template_image(teacher_photo, template_dir, 0)
    metadata = canonical.canonicalization

    page_corners = np.float32([
        [0, 0], [flat.shape[1] - 1, 0],
        [flat.shape[1] - 1, flat.shape[0] - 1], [0, flat.shape[0] - 1],
    ])
    page_to_teacher = cv2.getPerspectiveTransform(page_corners, teacher_quad)
    page_to_canonical = (
        np.asarray(metadata["source_to_canonical"], dtype=np.float64)
        @ np.asarray(page_to_teacher, dtype=np.float64)
    )
    canonical_width = metadata["canonical_width"]
    canonical_height = metadata["canonical_height"]

    def canonical_bbox(number: str, bbox: list[int]) -> dict:
        left, top, right, bottom = bbox
        points = np.float32([
            [left, top], [right, top], [right, bottom], [left, bottom],
        ]).reshape(-1, 1, 2)
        projected = cv2.perspectiveTransform(
            points, page_to_canonical.astype(np.float32)
        ).reshape(-1, 2)
        normalized = [
            int(round(float(projected[:, 0].min()) * 1000 / canonical_width)),
            int(round(float(projected[:, 1].min()) * 1000 / canonical_height)),
            int(round(float(projected[:, 0].max()) * 1000 / canonical_width)),
            int(round(float(projected[:, 1].max()) * 1000 / canonical_height)),
        ]
        return {
            "question_number": number,
            "question_type": "fill",
            "answer_bbox": normalized,
        }

    regions = [
        canonical_bbox("1", [650, 91, 930, 215]),
        canonical_bbox("2", [600, 400, 940, 500]),
    ]
    artifact = build_template_artifact(
        canonical.image_path,
        template_dir,
        0,
        regions,
        blank_detection_enabled=False,
        canonicalization=metadata,
    )
    student_path = tmp_path / "student.png"
    page_to_student = _perspective_student(flat, student_path)
    matcher = TemplateEvidenceMatcher({"page_1": artifact}, template_dir)

    result = matcher.match_and_build(student_path, tmp_path / "evidence")

    actual = np.asarray(
        result.diagnostics["raw_pixel_to_template_pixel"], dtype=np.float64
    )
    expected = page_to_canonical @ np.linalg.inv(page_to_student)
    comparison_points = np.float32([
        [70, 45], [1050, 20], [1090, 765], [35, 790], [560, 410],
        [700, 150], [900, 150], [700, 460], [900, 460],
    ]).reshape(-1, 1, 2)
    actual_projection = cv2.perspectiveTransform(
        comparison_points, actual.astype(np.float32)
    )
    expected_projection = cv2.perspectiveTransform(
        comparison_points, expected.astype(np.float32)
    )
    errors = np.linalg.norm(
        actual_projection.reshape(-1, 2) - expected_projection.reshape(-1, 2),
        axis=1,
    )
    assert float(np.max(errors)) < 8.0
    assert result.diagnostics["answer_source_complete"] is True
    assert result.question_numbers == ["1", "2"]


def test_canonical_template_rejects_blank_but_accepts_content_without_visible_corners(tmp_path):
    blank = tmp_path / "blank.png"
    Image.new("RGB", (800, 1000), "white").save(blank)
    with pytest.raises(ValueError, match="confidence"):
        canonicalize_template_image(blank, tmp_path / "blank-template", 0)

    blank_photo = tmp_path / "blank-photo.png"
    _template_photo(np.full((700, 1000, 3), 255, dtype=np.uint8), blank_photo)
    with pytest.raises(ValueError, match="confidence"):
        canonicalize_template_image(blank_photo, tmp_path / "blank-photo-template", 0)

    page_path = tmp_path / "page.png"
    page = _synthetic_template(page_path)
    source = np.float32([
        [0, 0], [page.shape[1] - 1, 0],
        [page.shape[1] - 1, page.shape[0] - 1], [0, page.shape[0] - 1],
    ])
    clipped_quad = np.float32([[-120, 80], [850, 25], [880, 730], [-80, 760]])
    transform = cv2.getPerspectiveTransform(source, clipped_quad)
    canvas = np.full((800, 900, 3), 35, dtype=np.uint8)
    clipped = cv2.warpPerspective(
        page,
        transform,
        (900, 800),
        dst=canvas,
        borderMode=cv2.BORDER_TRANSPARENT,
    )
    clipped_path = tmp_path / "clipped.png"
    Image.fromarray(clipped, mode="RGB").save(clipped_path)
    canonical = canonicalize_template_image(
        clipped_path,
        tmp_path / "clipped-template",
        0,
    )
    assert Path(canonical.image_path).is_file()
    assert canonical.canonicalization["detection_method"] == "full_frame_scan"
    assert canonical.canonicalization["detected_page_quad"] == [
        [0.0, 0.0],
        [899.0, 0.0],
        [899.0, 799.0],
        [0.0, 799.0],
    ]


def test_source_pixel_limit_is_checked_before_image_decode(tmp_path, monkeypatch):
    source_path = tmp_path / "oversized.png"
    source_path.write_bytes(b"bounded-test-placeholder")

    class OversizedImage:
        format = "PNG"
        mode = "RGB"
        n_frames = 1
        size = (10_000, 9_000)
        loaded = False
        verified = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def load(self):
            self.loaded = True
            raise AssertionError("oversized image must be rejected before decode")

        def verify(self):
            self.verified = True
            raise AssertionError("oversized image must be rejected before verify")

    oversized = OversizedImage()
    monkeypatch.setattr(template_evidence.Image, "open", lambda *_args, **_kwargs: oversized)

    with pytest.raises(ValueError, match="invalid"):
        canonicalize_template_image(source_path, tmp_path / "template", 0)
    with pytest.raises(ValueError, match="invalid"):
        template_evidence._load_rgb_image(source_path)
    assert oversized.loaded is False
    assert oversized.verified is False


def test_verify_canonicalization_rejects_non_finite_transform(tmp_path):
    flat_path = tmp_path / "flat.png"
    flat = _synthetic_template(flat_path)
    source = tmp_path / "photo.png"
    _template_photo(flat, source)
    template_dir = tmp_path / "template"
    canonical = canonicalize_template_image(source, template_dir, 0)
    artifact = build_template_artifact(
        canonical.image_path,
        template_dir,
        0,
        QUESTIONS,
        canonicalization=canonical.canonicalization,
    )
    tampered = copy.deepcopy(artifact)
    tampered["canonicalization"]["source_to_canonical"][0][0] = float("nan")

    with pytest.raises(ValueError, match="source_to_canonical"):
        verify_template_artifacts(template_dir, {"page_1": tampered})


def test_verify_artifact_rejects_path_escape_hash_and_dimension_tampering(tmp_path):
    source = tmp_path / "template.png"
    _synthetic_template(source)
    template_dir = tmp_path / "stored"
    artifact = build_template_artifact(source, template_dir, 0, QUESTIONS)

    escaped = copy.deepcopy(artifact)
    escaped["template_image"] = "../template.png"
    with pytest.raises(ValueError, match="inside template_dir"):
        verify_template_artifacts(template_dir, {"page_1": escaped})

    bad_hash = copy.deepcopy(artifact)
    bad_hash["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="sha256 mismatch"):
        verify_template_artifacts(template_dir, {"page_1": bad_hash})

    bad_size = copy.deepcopy(artifact)
    bad_size["width"] += 1
    with pytest.raises(ValueError, match="dimensions mismatch"):
        verify_template_artifacts(template_dir, {"page_1": bad_size})

    bad_blank_policy = copy.deepcopy(artifact)
    bad_blank_policy["blank_detection_enabled"] = "false"
    with pytest.raises(ValueError, match="blank_detection_enabled"):
        verify_template_artifacts(template_dir, {"page_1": bad_blank_policy})


def test_template_artifact_materializes_whole_box_whiteout_without_changing_original(
    tmp_path,
):
    source = tmp_path / "template.png"
    image = np.full((100, 160, 3), 240, dtype=np.uint8)
    image[20:40, 30:70] = (25, 35, 45)
    Image.fromarray(image, mode="RGB").save(source)
    template_dir = tmp_path / "stored"
    regions = [{
        "question_number": "1",
        "regions": [[150, 200, 500, 500]],
    }]

    artifact = build_template_artifact(
        source,
        template_dir,
        0,
        [{"question_number": "1", "question_type": "fill"}],
        template_handwriting_regions=regions,
    )
    assert artifact["template_handwriting_regions"] == regions
    assert artifact["difference_template_image"] == (
        "artifacts/page_000/difference-template.png"
    )
    difference_path = template_dir / artifact["difference_template_image"]
    assert difference_path.is_file()
    assert hashlib.sha256(difference_path.read_bytes()).hexdigest() == (
        artifact["difference_template_sha256"]
    )

    stored_path = template_dir / artifact["template_image"]
    with Image.open(source) as source_file:
        assert np.array_equal(np.asarray(source_file.convert("RGB")), image)
    with Image.open(stored_path) as stored_file:
        stored = np.asarray(stored_file.convert("RGB"))
    with Image.open(difference_path) as difference_file:
        difference = np.asarray(difference_file.convert("RGB"))

    assert np.array_equal(stored, image)
    # [150, 200, 500, 500] in normalized space maps to this complete pixel
    # rectangle. Every channel is white; no pixel/content heuristic is applied.
    assert np.all(difference[20:50, 24:80] == 255)
    assert np.array_equal(difference[:20], stored[:20])
    assert verify_template_artifacts(template_dir, {"page_1": artifact})[
        "page_1"
    ] == artifact

    matcher = TemplateEvidenceMatcher({"page_1": artifact}, template_dir)
    assert np.array_equal(matcher._load_verified_template_rgb("page_1"), stored)
    assert np.array_equal(
        matcher._load_verified_difference_template_rgb("page_1"),
        difference,
    )

    tampered = dict(artifact)
    tampered["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="template artifact sha256 mismatch"):
        materialize_template_difference_artifact(template_dir, tampered)


def test_verify_artifact_accepts_only_a_hash_bound_continuous_crop_plan(tmp_path):
    source = tmp_path / "template.png"
    _synthetic_template(source)
    template_dir = tmp_path / "stored"
    artifact = build_template_artifact(source, template_dir, 0, QUESTIONS)
    artifact["question_crop_plan"] = _question_crop_plan(artifact["sha256"])

    verified = verify_template_artifacts(template_dir, {"page_1": artifact})

    assert verified["page_1"]["question_crop_plan"]["provider_bottom_ignored"] is True
    assert verified["page_1"]["question_crop_plan"]["coverage"]["vertical_gaps"] == 0

    tampered = copy.deepcopy(artifact)
    tampered["question_crop_plan"]["source_artifact_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="source hash mismatch"):
        verify_template_artifacts(template_dir, {"page_1": tampered})


def test_matcher_aligns_perspective_and_emits_answer_only_evidence(
    tmp_path,
    monkeypatch,
):
    # Force the synthetic page through the reduced SIFT path.  The resulting
    # keypoint coordinates must still map back to the full artifact space.
    monkeypatch.setattr(template_evidence, "FEATURE_MAX_EDGE", 640)
    source = tmp_path / "template.png"
    template = _synthetic_template(source)
    student = tmp_path / "student.png"
    _perspective_student(template, student)
    template_dir = tmp_path / "stored"
    artifact = build_template_artifact(source, template_dir, 0, QUESTIONS)
    matcher = TemplateEvidenceMatcher({"page_1": artifact}, template_dir)

    result = matcher.match_and_build(student, tmp_path / "evidence")

    assert isinstance(result, EvidenceResult)
    assert result.page_type_id == "page_1"
    assert result.question_numbers == ["1", "2"]
    assert result.blank_question_numbers == []
    evidence_path = Path(result.evidence_path)
    assert evidence_path.is_file()
    assert evidence_path.stem.startswith("evidence-")
    assert len(evidence_path.stem.removeprefix("evidence-")) == 32
    assert all(
        char in "0123456789abcdef"
        for char in evidence_path.stem.removeprefix("evidence-")
    )
    with Image.open(evidence_path) as evidence_image:
        evidence = np.asarray(evidence_image.convert("RGB"))
    assert evidence.shape == (700, 1000, 3)
    assert evidence.std() > 5
    red_pixels = (evidence[:, :, 0] > 170) & (evidence[:, :, 1] < 100)
    assert int(red_pixels.sum()) > 20
    assert result.diagnostics["candidates"][0]["good_matches"] >= 25
    assert result.diagnostics["candidates"][0]["inliers"] >= 15
    assert result.diagnostics["candidates"][0]["coverage"] >= 0.78
    assert result.diagnostics["candidates"][0]["reprojection_rmse"] <= 6.0
    assert result.diagnostics["feature_max_edge"] == 640
    assert len(result.diagnostics["local_alignment"]) == 2
    assert result.diagnostics["answer_source_complete"] is True
    assert result.diagnostics["ownership"] == "full_column_difference"

    cleanup_evidence(evidence_path)
    assert not evidence_path.exists()
    cleanup_evidence(evidence_path)


def test_full_column_production_path_uses_v2_difference(tmp_path, monkeypatch):
    calls = []

    def v2(aligned_rgb, template_rgb, source_mask, **kwargs):
        calls.append(kwargs)
        return np.full_like(aligned_rgb, 255)

    monkeypatch.setattr(template_evidence_sheets, "_answer_only_pixels_v2", v2)
    monkeypatch.setattr(template_evidence_columns, "_answer_only_pixels_v2", v2)
    image = np.full((80, 120, 3), 255, dtype=np.uint8)
    source_mask = np.full((80, 120), 255, dtype=np.uint8)

    result = template_evidence.build_full_column_difference_evidence_from_aligned(
        image,
        source_mask,
        image.copy(),
        {
            "page_type_id": "page_1",
            "question_numbers": ["1"],
            "question_anchors": [],
            "template_handwriting_regions": [],
        },
        tmp_path,
    )

    assert calls == [{
        "answer_regions": None,
        "template_handwriting_regions": [],
    }]
    assert result.diagnostics["difference_algorithm"] == "difference-v2-cleaner"
    for path in result.evidence_paths:
        cleanup_evidence(path)


def test_full_column_labels_use_the_same_handwriting_region_as_whiteout(
    tmp_path, monkeypatch,
):
    def v2(aligned_rgb, template_rgb, source_mask, **kwargs):
        return np.full_like(aligned_rgb, 255)

    monkeypatch.setattr(template_evidence_sheets, "_answer_only_pixels_v2", v2)
    monkeypatch.setattr(template_evidence_columns, "_answer_only_pixels_v2", v2)
    image = np.full((500, 1000, 3), 255, dtype=np.uint8)
    source_mask = np.full((500, 1000), 255, dtype=np.uint8)

    result = template_evidence.build_full_column_difference_evidence_from_aligned(
        image,
        source_mask,
        image.copy(),
        {
            "page_type_id": "page_1",
            "question_numbers": ["22"],
            "question_anchors": [{
                "question_number": "22",
                "question_number_bbox": [10, 100, 30, 140],
            }],
            "template_handwriting_regions": [{
                "question_number": "22",
                "regions": [[600, 300, 700, 400]],
            }],
        },
        tmp_path,
    )

    placement = result.diagnostics["evidence_columns"][0]["label_placements"][0]
    assert placement["source"] == "template_handwriting_region"
    assert placement["region_bbox_1000"] == [600, 300, 700, 400]
    assert placement["label_bbox"][0] > 0
    assert placement["label_bbox"][2] < 600
    for path in result.evidence_paths:
        cleanup_evidence(path)


def test_marked_full_column_labels_each_answer_region_under_same_question(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        template_evidence_sheets,
        "_answer_only_pixels_v2",
        lambda aligned_rgb, template_rgb, source_mask, **kwargs: np.full_like(
            aligned_rgb, 255
        ),
    )
    monkeypatch.setattr(
        template_evidence_columns,
        "_answer_only_pixels_v2",
        lambda aligned_rgb, template_rgb, source_mask, **kwargs: np.full_like(
            aligned_rgb, 255
        ),
    )
    image = np.full((600, 1200, 3), 255, dtype=np.uint8)
    source_mask = np.full((600, 1200), 255, dtype=np.uint8)
    regions = [[100, 100, 180, 140], [500, 300, 590, 340], [850, 450, 940, 490]]
    result = template_evidence.build_full_column_difference_evidence_from_aligned(
        image,
        source_mask,
        image.copy(),
        {
            "page_type_id": "page_1",
            "template_mode": "marked",
            "question_numbers": ["22"],
            "question_label_policy": template_evidence.PRINTED_SOLVE_LABEL_POLICY,
            "question_anchors": [],
            "template_answer_regions": [{
                "question_number": "22",
                "regions": regions,
            }],
            "template_handwriting_regions": [{
                "question_number": "22",
                "regions": regions,
            }],
        },
        tmp_path,
    )
    placements = result.diagnostics["evidence_columns"][0]["label_placements"]
    assert len(placements) == len(regions)
    assert [item["question_number"] for item in placements] == ["22"] * len(regions)
    assert [item["region_index"] for item in placements] == [0, 1, 2]
    assert [item["region_bbox_1000"] for item in placements] == regions
    assert all(item["source"] == "template_answer_region" for item in placements)
    assert all(len(item["connector_line"]) == 2 for item in placements)
    for path in result.evidence_paths:
        cleanup_evidence(path)


@pytest.mark.parametrize("template_mode", ["blank", "marked"])
def test_fixed_solve_label_keeps_exemptions_and_source_pixels_unchanged(
    tmp_path, monkeypatch, template_mode,
):
    calls = []
    difference = np.full((700, 1000, 3), 255, np.uint8)
    difference[110:125, 70:110] = 20
    handwriting = [{"question_number": "26", "regions": [[600, 400, 800, 600]]}]
    supplied_handwriting = handwriting if template_mode == "marked" else []
    saved = copy.deepcopy(supplied_handwriting)

    def v2(aligned_rgb, template_rgb, source_mask, **kwargs):
        calls.append(copy.deepcopy(kwargs))
        return difference.copy()

    monkeypatch.setattr(template_evidence_sheets, "_answer_only_pixels_v2", v2)
    monkeypatch.setattr(template_evidence_columns, "_answer_only_pixels_v2", v2)
    result = template_evidence.build_full_column_difference_evidence_from_aligned(
        difference.copy(), np.full((700, 1000), 255, np.uint8), np.full_like(difference, 255),
        {"page_type_id": "page_1", "template_mode": template_mode,
         "question_label_policy": template_evidence.PRINTED_SOLVE_LABEL_POLICY,
         "question_numbers": ["26"],
         "question_schema": [{"question_number": "26", "question_type": "solve"}],
         "question_anchors": [{"question_number": "26", "question_number_bbox": [60, 140, 110, 180]}],
         "template_answer_regions": [{"question_number": "26", "regions": []}],
         "template_handwriting_regions": supplied_handwriting},
        tmp_path,
    )
    assert calls == [{"answer_regions": None, "template_handwriting_regions": saved}]
    assert supplied_handwriting == saved
    assert np.all(difference[110:125, 70:110] == 20)
    placement = result.diagnostics["evidence_columns"][0]["label_placements"][0]
    assert placement["source"] == "printed_question_number"
    assert placement["placement_mode"] == "external_rail"
    assert placement["question_number_bbox"] == [60, 140, 110, 180]
    from services.drawing_grading import reconstruct_canvas
    replay = reconstruct_canvas(result.evidence_paths, result.diagnostics)
    assert np.array_equal(replay[110:125, 70:110], difference[110:125, 70:110])
    assert result.diagnostics["template_handwriting_exemptions"] == len(supplied_handwriting)
    assert result.blank_question_numbers == []


@pytest.mark.parametrize("template_mode", ["blank", "marked"])
def test_matcher_passes_solve_schema_and_renders_printed_number_after_alignment(tmp_path, template_mode):
    source = tmp_path / "template.png"
    template = _synthetic_template(source)
    # An actual printed number on a generated, non-student fixture. The bbox
    # is annotated from this known drawing, not inferred from a model result.
    cv2.putText(template, "26.", (100, 220), cv2.FONT_HERSHEY_SIMPLEX,
                .8, (0, 0, 0), 2, cv2.LINE_AA)
    Image.fromarray(template).save(source)
    photo = tmp_path / "synthetic-answer-photo.png"
    _perspective_student(template, photo)
    source_hash, photo_hash = _sha256(source), _sha256(photo)
    number_bbox = [100, 285, 147, 327]
    template_dir = tmp_path / "stored"
    artifact = build_template_artifact(
        source, template_dir, 0,
        [{"question_number": "26", "question_type": "solve"}],
        template_mode=template_mode,
        question_anchors=[{"question_number": "26", "question_number_bbox": number_bbox}],
        template_answer_regions=[{"question_number": "26", "regions": []}],
        template_handwriting_regions=(
            [{"question_number": "26", "regions": []}] if template_mode == "marked" else []
        ),
    )
    # Both artifact construction and reloading permit a solve with a printed
    # number even though it has no answer-region/handwriting anchor to reuse.
    matcher = TemplateEvidenceMatcher({"page_1": artifact}, template_dir)
    result = matcher.match_and_build(photo, tmp_path / "evidence")
    placements = [p for column in result.diagnostics["evidence_columns"]
                  for p in column["label_placements"]]
    assert len(placements) == 1
    placement = placements[0]
    assert placement["source"] == "printed_question_number"
    assert placement["label_bbox"][2] == 147
    assert abs((placement["label_bbox"][1] + placement["label_bbox"][3]) / 2 - 214.2) <= .5
    assert placement["placement_offset_px"] == [0, 0]
    assert result.question_numbers == ["26"]
    assert result.blank_question_numbers == []
    assert result.diagnostics["answer_source_complete"] is True
    assert result.diagnostics["ownership"] == "full_column_difference"
    assert _sha256(source) == source_hash
    assert _sha256(photo) == photo_hash
    with Image.open(result.evidence_path) as rendered:
        assert rendered.size == (1000, 700)


def test_blank_template_does_not_label_from_legacy_question_anchor_position(
    tmp_path, monkeypatch,
):
    def v2(aligned_rgb, template_rgb, source_mask, **kwargs):
        return np.full_like(aligned_rgb, 255)

    monkeypatch.setattr(template_evidence_sheets, "_answer_only_pixels_v2", v2)
    monkeypatch.setattr(template_evidence_columns, "_answer_only_pixels_v2", v2)
    image = np.full((500, 1000, 3), 255, dtype=np.uint8)
    source_mask = np.full((500, 1000), 255, dtype=np.uint8)

    # Missing blank ownership geometry must not silently send unlabelled OCR.
    with pytest.raises(template_evidence.TemplateEvidenceError, match="教师"):
        template_evidence.build_full_column_difference_evidence_from_aligned(
            image, source_mask, image.copy(),
            {"page_type_id": "page_1", "template_mode": "blank",
             "question_numbers": ["22"],
             "question_anchors": [{"question_number": "22", "question_number_bbox": [10, 100, 30, 140]}],
             "template_handwriting_regions": [{"question_number": "22", "regions": [[600, 300, 700, 400]]}]},
            tmp_path,
        )


def test_blank_template_labels_reuse_answer_region_position(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        template_evidence_sheets,
        "_answer_only_pixels_v2",
        lambda aligned_rgb, template_rgb, source_mask, **kwargs: np.full_like(
            aligned_rgb, 255
        ),
    )
    monkeypatch.setattr(
        template_evidence_columns,
        "_answer_only_pixels_v2",
        lambda aligned_rgb, template_rgb, source_mask, **kwargs: np.full_like(
            aligned_rgb, 255
        ),
    )
    image = np.full((500, 1000, 3), 255, dtype=np.uint8)
    source_mask = np.full((500, 1000), 255, dtype=np.uint8)

    result = template_evidence.build_full_column_difference_evidence_from_aligned(
        image,
        source_mask,
        image.copy(),
        {
            "page_type_id": "page_1",
            "template_mode": "blank",
            "question_numbers": ["22"],
            "question_anchors": [{"question_number": "22"}],
            "template_answer_regions": [{
                "question_number": "22",
                "regions": [[600, 300, 700, 400]],
            }],
            "template_handwriting_regions": [],
        },
        tmp_path,
    )

    placement = result.diagnostics["evidence_columns"][0]["label_placements"][0]
    assert placement["source"] == "template_answer_region"
    assert placement["region_bbox_1000"] == [600, 300, 700, 400]
    for path in result.evidence_paths:
        cleanup_evidence(path)


@pytest.mark.parametrize("template_mode", ["blank", "marked"])
def test_qwen37_labels_are_complete_and_left_of_regions(
    tmp_path, monkeypatch, template_mode,
):
    monkeypatch.setattr(
        template_evidence_sheets,
        "_answer_only_pixels_v2",
        lambda aligned_rgb, template_rgb, source_mask, **kwargs: np.full_like(
            aligned_rgb, 255
        ),
    )
    monkeypatch.setattr(
        template_evidence_columns,
        "_answer_only_pixels_v2",
        lambda aligned_rgb, template_rgb, source_mask, **kwargs: np.full_like(
            aligned_rgb, 255
        ),
    )
    # Put the region at the bottom of the page so the regression catches the
    # old renderer's lower-half clipping.  The label still has to remain in
    # the left gutter and fully inside the exact Qwen input image.
    image = np.full((120, 1000, 3), 255, dtype=np.uint8)
    source_mask = np.full((120, 1000), 255, dtype=np.uint8)
    region_key = (
        "template_answer_regions"
        if template_mode == "blank"
        else "template_handwriting_regions"
    )
    layout = {
        "page_type_id": "page_1",
        "template_mode": template_mode,
        "question_numbers": ["25"],
        "question_anchors": [{"question_number": "25"}],
        "template_answer_regions": [],
        "template_handwriting_regions": [],
    }
    layout[region_key] = [{
        "question_number": "25",
        "regions": [[400, 900, 600, 999]],
    }]

    result = template_evidence.build_full_column_difference_evidence_from_aligned(
        image,
        source_mask,
        image.copy(),
        layout,
        tmp_path,
    )

    placement = result.diagnostics["evidence_columns"][0]["label_placements"][0]
    label_left, label_top, label_right, label_bottom = placement["label_bbox"]
    assert placement["label_fully_visible"] is True
    assert placement["label_left_of_region"] is True
    assert 0 <= label_left <= label_right < image.shape[1]
    assert 0 <= label_top <= label_bottom < image.shape[0]
    assert label_right < 400
    assert result.diagnostics["qwen37_label_contract"]["question_number_boxes"] is False
    assert result.diagnostics["qwen37_label_contract"]["answer_region_overlays"] is False
    evidence = np.asarray(Image.open(result.evidence_paths[0]).convert("RGB"))
    red_pixels = (
        (evidence[:, :, 0] > 170)
        & (evidence[:, :, 1] < 100)
        & (evidence[:, :, 2] < 100)
    )
    assert int(red_pixels.sum()) == 0
    for path in result.evidence_paths:
        cleanup_evidence(path)


def test_matcher_can_skip_unused_evidence_artifact(tmp_path):
    source = tmp_path / "template.png"
    template = _synthetic_template(source)
    student = tmp_path / "student.png"
    _perspective_student(template, student)
    template_dir = tmp_path / "stored"
    artifact = build_template_artifact(source, template_dir, 0, QUESTIONS)
    matcher = TemplateEvidenceMatcher({"page_1": artifact}, template_dir)

    output_dir = tmp_path / "evidence"
    result = matcher.match_and_build(
        student,
        output_dir,
        include_evidence=False,
    )

    assert result.evidence_path == ""
    assert Path(result.aligned_path).is_file()
    assert result.diagnostics["evidence_width"] == 0
    assert result.diagnostics["evidence_height"] == 0
    assert list(output_dir.glob("evidence-*.png")) == []


def test_matcher_uses_confirmed_answer_regions_when_anchor_overview_exists(tmp_path):
    source = tmp_path / "template.png"
    _synthetic_template(source)
    template_dir = tmp_path / "stored"
    artifact = build_template_artifact(source, template_dir, 0, QUESTIONS)
    artifact["question_crop_plan"] = _question_crop_plan(artifact["sha256"])
    matcher = TemplateEvidenceMatcher({"page_1": artifact}, template_dir)

    result = matcher.match_and_build(source, tmp_path / "evidence")

    assert result.diagnostics["ownership"] == "full_column_difference"
    assert result.diagnostics["question_crop_plan"] == {
        "strategy": "full_height_columns",
        "block_count": 2,
        "anchor_count": 0,
        "vertical_gaps": 0,
        "provider_bottom_ignored": True,
        "requires_question_number_gate": True,
        "missing_expected_question_numbers": [],
    }
    with Image.open(result.evidence_path) as evidence_image:
        evidence = np.asarray(evidence_image.convert("RGB"))
    assert len(result.evidence_paths) == 2
    assert evidence.shape == (700, 500, 3)
    assert evidence.std() >= 0
    for path in result.evidence_paths:
        cleanup_evidence(path)


def test_multi_evidence_materializes_native_pixel_focus_rows_in_question_order(tmp_path):
    template = np.full((700, 1000, 3), 255, dtype=np.uint8)
    template[:, :500] = (200, 205, 210)
    image = template.copy()
    image[235:260, 180:260] = (20, 20, 20)
    source_mask = np.full((700, 1000), 255, dtype=np.uint8)
    plan = _question_crop_plan("a" * 64)
    layout = {
        "page_type_id": "page_1",
        "answer_regions": [
            {
                "question_number": "1",
                "question_type": "fill",
                "answer_bbox": [100, 200, 400, 300],
            },
            {
                "question_number": "2",
                "question_type": "solve",
                "answer_bbox": [600, 300, 900, 500],
            },
        ],
        "blank_detection_enabled": False,
        "question_crop_plan": plan,
    }

    result = build_multi_evidence_from_aligned(
        image,
        source_mask,
        template,
        layout,
        tmp_path,
    )

    assert result.question_numbers == ["1", "2"]
    sheets = build_focus_evidence_sheet_layout(
        layout["answer_regions"],
        1000,
        700,
        layout["question_crop_plan"],
    )
    assert len(result.evidence_paths) == len(sheets) == 2
    assert result.diagnostics["evidence_sheets"] == [
        {"sheet_id": sheet["sheet_id"], "regions": sheet["regions"]}
        for sheet in sheets
    ]
    for path, sheet in zip(result.evidence_paths, sheets, strict=True):
        row = sheet["rows"][0]
        with Image.open(path) as evidence_image:
            evidence_pixels = np.asarray(evidence_image.convert("RGB"))
            assert evidence_image.size == (sheet["width"], sheet["height"])
            source_left, source_top, source_right, source_bottom = row["source_bbox"]
            paste_left, paste_top, paste_right, paste_bottom = row["paste_bbox"]
            assert np.array_equal(
                evidence_pixels[paste_top:paste_bottom, paste_left:paste_right],
                np.where(
                    np.all(
                        image[source_top:source_bottom, source_left:source_right]
                        == (20, 20, 20),
                        axis=2,
                    )[..., None],
                    (20, 20, 20),
                    255,
                ),
            )
    for path in result.evidence_paths:
        cleanup_evidence(path)


def test_choice_answer_pixels_keeps_dominant_mark_and_drops_small_residue():
    crop = np.full((80, 240, 3), 255, dtype=np.uint8)
    crop[12:15, 18:21] = 20
    crop[20:65, 150:172] = 20

    filtered = template_evidence._choice_answer_pixels(crop)

    assert np.all(filtered[12:15, 18:21] == 255)
    assert np.all(filtered[20:65, 150:172] == 20)


def test_choice_answer_pixels_keeps_nearby_detached_letter_stroke():
    crop = np.full((90, 240, 3), 255, dtype=np.uint8)
    crop[20:70, 150:154] = 20
    crop[22:68, 160:178] = 20
    crop[8:11, 20:23] = 20

    filtered = template_evidence._choice_answer_pixels(crop)

    assert np.all(filtered[20:70, 150:154] == 20)
    assert np.all(filtered[22:68, 160:178] == 20)
    assert np.all(filtered[8:11, 20:23] == 255)


def _template_handwriting_page():
    """Template with printed text, a printed rule, and one handwritten trace."""
    template = np.full((240, 320, 3), 240, dtype=np.uint8)
    printed = np.zeros((240, 320), dtype=bool)
    cv2.putText(
        template, "QUESTION", (18, 40), cv2.FONT_HERSHEY_SIMPLEX,
        0.9, (28, 28, 28), 2, cv2.LINE_AA,
    )
    cv2.line(template, (18, 150), (300, 150), (35, 35, 35), 2)
    printed |= np.any(template < 200, axis=2)

    trace = np.zeros((240, 320), dtype=np.uint8)
    for step in range(70):
        cv2.circle(trace, (90 + step * 2, 95 + int(14 * math.sin(step / 9.0))), 2, 1, -1)
    handwriting = trace.astype(bool)
    template[handwriting] = 60

    student = template.copy()
    answer = np.zeros((240, 320), dtype=np.uint8)
    for step in range(90):
        cv2.circle(answer, (120 + int(30 * math.sin(step / 20.0)), 70 + step), 2, 1, -1)
    answer = answer.astype(bool)
    student[answer] = 45
    return template, student, printed, handwriting, answer


def _kept_ratio(answer_only, target):
    kept = np.any(answer_only < 250, axis=2)
    return np.count_nonzero(kept & target) / max(1, np.count_nonzero(target))


def test_v2_directly_wipes_every_approved_exemption_box():
    template = np.full((240, 420, 3), 240, dtype=np.uint8)
    cv2.putText(
        template, "PRINTED", (16, 42), cv2.FONT_HERSHEY_SIMPLEX,
        0.8, (35, 35, 35), 2, cv2.LINE_AA,
    )
    printed = np.any(template < 200, axis=2)

    long_trace = np.zeros((240, 420), dtype=np.uint8)
    points = np.asarray([[80, 110], [110, 82], [145, 120], [185, 86]])
    cv2.polylines(long_trace, [points], False, 1, 5, cv2.LINE_AA)
    long_trace = long_trace.astype(bool)
    template[long_trace] = 55

    compact_before = template.copy()
    cv2.putText(
        template, "3", (320, 126), cv2.FONT_HERSHEY_SIMPLEX,
        0.55, (55, 55, 55), 1, cv2.LINE_AA,
    )
    compact = np.any(template != compact_before, axis=2)
    assert 0 < np.count_nonzero(compact) < template_evidence.TEMPLATE_HANDWRITING_MIN_AREA

    def normalized_box(mask):
        ys, xs = np.nonzero(mask)
        return [
            max(0, int(math.floor((xs.min() - 1) * 1000 / 420))),
            max(0, int(math.floor((ys.min() - 1) * 1000 / 240))),
            min(1000, int(math.ceil((xs.max() + 2) * 1000 / 420))),
            min(1000, int(math.ceil((ys.max() + 2) * 1000 / 240))),
        ]

    regions = [
        {"question_number": "1", "regions": [normalized_box(long_trace)]},
        {"question_number": "2", "regions": [normalized_box(compact)]},
    ]
    gray = cv2.cvtColor(template, cv2.COLOR_RGB2GRAY)
    cleaned = template_evidence._erase_template_handwriting(gray, regions)

    # Once the model approves a rectangle, its complete baseline is blanked.
    # The production path deliberately does not inspect component size, shape,
    # or whether a dark pixel looks printed.
    assert np.count_nonzero(cleaned[long_trace] >= 250) / np.count_nonzero(long_trace) > 0.95
    assert np.count_nonzero(cleaned[compact] >= 250) / np.count_nonzero(compact) > 0.95

    evidence = template_evidence._answer_only_pixels_v2(
        template,
        template,
        np.full((240, 420), 255, dtype=np.uint8),
        None,
        regions,
    )
    # An identical student copy now exposes both approved boxes as evidence;
    # this is the intentional consequence of making the model box authoritative.
    assert _kept_ratio(evidence, long_trace) > 0.90
    assert _kept_ratio(evidence, compact) > 0.70


def test_v2_evaluates_compact_component_after_a_long_component_in_same_box():
    """A snapped stroke must not suppress a separately qualified compact stroke."""
    height, width = 240, 420
    gray = np.full((height, width), 240, dtype=np.uint8)
    long_trace = np.zeros_like(gray)
    cv2.polylines(
        long_trace,
        [np.asarray([[60, 110], [90, 80], [125, 115], [160, 85]])],
        False,
        55,
        5,
        cv2.LINE_AA,
    )
    compact_trace = np.zeros_like(gray)
    # This is intentionally compact in area (< 200), but its sparse, elongated
    # shape supplies the second signal required by the guarded fallback.
    cv2.polylines(
        compact_trace,
        [np.asarray([[210, 70], [220, 82], [210, 94], [220, 106], [210, 118]])],
        False,
        55,
        1,
        cv2.LINE_AA,
    )
    template = np.repeat(gray[:, :, None], 3, axis=2)
    template[long_trace > 0] = 55
    template[compact_trace > 0] = 55
    long_mask = template_evidence._local_ink_mask(
        cv2.cvtColor(template, cv2.COLOR_RGB2GRAY),
        contrast=template_evidence.TEMPLATE_INK_LOCAL_CONTRAST,
        max_gray=template_evidence.TEMPLATE_INK_MAX_GRAY,
    )
    compact_mask = long_mask.copy()
    compact_mask[:, :190] = False
    compact_mask[:, 240:] = False
    assert 0 < np.count_nonzero(compact_mask) < template_evidence.TEMPLATE_HANDWRITING_MIN_AREA
    combined = long_mask
    ys, xs = np.nonzero(combined)
    regions = [{
        "question_number": "1",
        "regions": [[
            int((xs.min() - 2) * 1000 / width),
            int((ys.min() - 2) * 1000 / height),
            int((xs.max() + 3) * 1000 / width),
            int((ys.max() + 3) * 1000 / height),
        ]],
    }]

    cleaned = template_evidence._erase_template_handwriting(
        cv2.cvtColor(template, cv2.COLOR_RGB2GRAY),
        regions,
    )

    assert np.count_nonzero(cleaned[long_mask] >= 250) / np.count_nonzero(long_mask) > 0.95
    assert np.count_nonzero(cleaned[compact_mask] >= 250) / np.count_nonzero(compact_mask) > 0.90


def test_v2_overshoot_box_is_authoritative_after_registration_shift():
    """An approved box is blanked wholesale even when registration shifts."""
    template, _student, printed, handwriting, answer = _template_handwriting_page()
    height, width = template.shape[:2]
    shifted_student = cv2.warpAffine(
        template,
        np.asarray([[1.0, 0.0, 1.0], [0.0, 1.0, 0.0]], dtype=np.float32),
        (width, height),
        borderValue=(240, 240, 240),
    )
    shifted_handwriting = cv2.warpAffine(
        handwriting.astype(np.uint8),
        np.asarray([[1.0, 0.0, 1.0], [0.0, 1.0, 0.0]], dtype=np.float32),
        (width, height),
        flags=cv2.INTER_NEAREST,
    ).astype(bool)
    answer_mask = np.zeros((height, width), dtype=np.uint8)
    for step in range(90):
        cv2.circle(
            answer_mask,
            (120 + int(30 * math.sin(step / 20.0)), 70 + step),
            2,
            1,
            -1,
        )
    answer_mask = answer_mask.astype(bool)
    shifted_student[answer_mask] = 45
    ys, xs = np.nonzero(handwriting)
    regions = [{
        "question_number": "1",
        "regions": [[
            int(xs.min() * 1000 / width),
            0,
            int((xs.max() + 1) * 1000 / width),
            int((ys.max() + 1) * 1000 / height),
        ]],
    }]
    covered_print = printed.copy()
    covered_print[:, :xs.min()] = False
    covered_print[:, xs.max() + 1:] = False
    covered_print[ys.max() + 1:, :] = False

    evidence = template_evidence._answer_only_pixels_v2(
        shifted_student,
        template,
        np.full((height, width), 255, dtype=np.uint8),
        None,
        regions,
    )

    # The caller is responsible for giving the model a tight box.  If it
    # overshoots printed content, that content is intentionally exposed too;
    # no pixel heuristic is allowed to undo the approved rectangle.
    assert _kept_ratio(evidence, covered_print) > 0.50
    assert _kept_ratio(evidence, shifted_handwriting) > 0.90
    assert _kept_ratio(evidence, answer_mask) > 0.90


def test_v2_erases_a_print_only_box_when_model_approves_it():
    template = np.full((180, 420, 3), 240, dtype=np.uint8)
    before = template.copy()
    cv2.putText(
        template, "a=(1,2)", (90, 105), cv2.FONT_HERSHEY_SIMPLEX,
        0.8, (40, 40, 40), 2, cv2.LINE_AA,
    )
    printed = np.any(template != before, axis=2)
    ys, xs = np.nonzero(printed)
    regions = [{
        "question_number": "1",
        "regions": [[
            int((xs.min() - 2) * 1000 / 420),
            int((ys.min() - 2) * 1000 / 180),
            int((xs.max() + 3) * 1000 / 420),
            int((ys.max() + 3) * 1000 / 180),
        ]],
    }]

    gray = cv2.cvtColor(template, cv2.COLOR_RGB2GRAY)
    cleaned = template_evidence._erase_template_handwriting(gray, regions)

    assert np.count_nonzero(cleaned[printed] >= 250) / np.count_nonzero(printed) > 0.95
    evidence = template_evidence._answer_only_pixels_v2(
        template,
        template,
        np.full((180, 420), 255, dtype=np.uint8),
        None,
        regions,
    )
    assert _kept_ratio(evidence, printed) > 0.70


def test_v2_erases_a_sparse_stroke_once_the_box_is_approved():
    """The model's approval, rather than stroke shape, controls erasure."""
    height, width = 220, 420
    template = np.full((height, width, 3), 240, dtype=np.uint8)
    cv2.line(
        template,
        (200, 50),
        (220, 120),
        (35, 35, 35),
        2,
        cv2.LINE_AA,
    )
    gray = cv2.cvtColor(template, cv2.COLOR_RGB2GRAY)
    printed = template_evidence._local_ink_mask(
        gray,
        contrast=template_evidence.TEMPLATE_INK_LOCAL_CONTRAST,
        max_gray=template_evidence.TEMPLATE_INK_MAX_GRAY,
    )
    ys, xs = np.nonzero(printed)
    regions = [{
        "question_number": "1",
        "regions": [[
            int((xs.min() - 2) * 1000 / width),
            int((ys.min() - 2) * 1000 / height),
            int((xs.max() + 3) * 1000 / width),
            int((ys.max() + 3) * 1000 / height),
        ]],
    }]

    cleaned = template_evidence._erase_template_handwriting(
        gray,
        regions,
    )
    evidence = template_evidence._answer_only_pixels_v2(
        template,
        template,
        np.full((height, width), 255, dtype=np.uint8),
        None,
        regions,
    )

    assert np.count_nonzero(cleaned[printed] >= 250) / np.count_nonzero(printed) > 0.95
    assert _kept_ratio(evidence, printed) > 0.90


def test_v2_erases_a_printed_glyph_connected_to_an_approved_stroke_box():
    """A box is atomic even when printed and handwritten ink is connected."""
    height, width = 200, 500
    template = np.full((height, width, 3), 240, dtype=np.uint8)
    printed = np.zeros((height, width), dtype=np.uint8)
    cv2.putText(
        printed,
        "A",
        (100, 100),
        cv2.FONT_HERSHEY_SIMPLEX,
        2,
        1,
        5,
        cv2.LINE_AA,
    )
    cv2.putText(
        template,
        "A",
        (100, 100),
        cv2.FONT_HERSHEY_SIMPLEX,
        2,
        (30, 30, 30),
        5,
        cv2.LINE_AA,
    )
    stroke = np.zeros((height, width), dtype=np.uint8)
    cv2.line(stroke, (130, 80), (320, 130), 1, 8, cv2.LINE_AA)
    template[stroke.astype(bool)] = 50
    gray = cv2.cvtColor(template, cv2.COLOR_RGB2GRAY)
    stroke_pixels = stroke.astype(bool)
    ys, xs = np.nonzero(stroke_pixels)
    regions = [{
        "question_number": "1",
        "regions": [[
            int((xs.min() - 2) * 1000 / width),
            int((ys.min() - 2) * 1000 / height),
            int((xs.max() + 3) * 1000 / width),
            int((ys.max() + 3) * 1000 / height),
        ]],
    }]
    printed_pixels = printed.astype(bool)

    cleaned = template_evidence._erase_template_handwriting(gray, regions)
    evidence = template_evidence._answer_only_pixels_v2(
        template,
        template,
        np.full((height, width), 255, dtype=np.uint8),
        None,
        regions,
    )

    approved = template_evidence._template_handwriting_region_mask(
        regions, width, height,
    )
    printed_inside = printed_pixels & approved
    stroke_inside = stroke_pixels & approved
    assert np.count_nonzero((cleaned >= 250) & printed_inside) / np.count_nonzero(printed_inside) > 0.95
    assert _kept_ratio(evidence, printed_inside) > 0.70
    assert _kept_ratio(evidence, stroke_inside) > 0.90


def test_v2_approved_box_does_not_split_connected_student_and_template_ink():
    """An approved box is handled atomically, even across a connected stroke."""
    height, width = 220, 500
    template = np.full((height, width, 3), 240, dtype=np.uint8)
    printed = np.zeros((height, width), dtype=np.uint8)
    cv2.putText(
        printed,
        "A",
        (90, 120),
        cv2.FONT_HERSHEY_SIMPLEX,
        2,
        1,
        5,
        cv2.LINE_AA,
    )
    template[printed.astype(bool)] = 30

    # The registered handwriting is separate from the glyph. The student's new
    # stroke bridges the small gap, but the model-approved box remains the sole
    # authority for what is blanked.
    frozen = np.zeros((height, width), dtype=np.uint8)
    cv2.polylines(
        frozen,
        [np.asarray([[130, 95], [145, 85], [160, 100], [175, 88], [190, 98]])],
        False,
        1,
        3,
        cv2.LINE_AA,
    )
    template[frozen.astype(bool)] = 50

    student = template.copy()
    new_stroke = np.zeros((height, width), dtype=np.uint8)
    cv2.line(new_stroke, (120, 96), (200, 120), 1, 3, cv2.LINE_AA)
    student[new_stroke.astype(bool)] = 40
    printed_pixels = printed.astype(bool)
    frozen_pixels = frozen.astype(bool)
    new_pixels = new_stroke.astype(bool)
    regions = [{
        "question_number": "1",
        "regions": [[
            int(125 * 1000 / width),
            int(78 * 1000 / height),
            int(198 * 1000 / width),
            int(108 * 1000 / height),
        ]],
    }]

    evidence = template_evidence._answer_only_pixels_v2(
        student,
        template,
        np.full((height, width), 255, dtype=np.uint8),
        None,
        regions,
    )
    kept = np.any(evidence < 250, axis=2)

    # Both the frozen trace and the genuinely new stroke survive the atomic box
    # treatment. Printed pixels inside an approved box are intentionally not
    # rescued by a second connected-component veto.
    assert _kept_ratio(evidence, frozen_pixels) > 0.90
    assert _kept_ratio(evidence, new_pixels) > 0.90


def test_approved_template_handwriting_box_exempts_the_confirmed_trace():
    """The model box is expected to cover the trace without code-side snapping."""
    template, student, _printed, handwriting, answer = _template_handwriting_page()
    mask = np.full((240, 320), 255, dtype=np.uint8)
    ys, xs = np.nonzero(handwriting)

    def exemption(shift):
        return [{
            "question_number": "1",
            "regions": [[
                int((xs.min() + shift) * 1000 / 320),
                int((ys.min() + shift) * 1000 / 240),
                int((xs.max() + 1 + shift) * 1000 / 320),
                int((ys.max() + 1 + shift) * 1000 / 240),
            ]],
        }]

    overlap = answer & handwriting
    assert np.count_nonzero(overlap) > 0

    without = template_evidence._answer_only_pixels_v2(student, template, mask)
    assert _kept_ratio(without, overlap) == 0.0

    # The production contract assumes a tight model box and applies it directly.
    exact = template_evidence._answer_only_pixels_v2(
        student, template, mask, None, exemption(0),
    )
    assert _kept_ratio(exact, overlap) == 1.0
    assert _kept_ratio(exact, answer) > 0.9


def test_exemption_box_overshooting_printed_text_is_not_pixel_filtered():
    """A box that includes print is intentionally treated as one blank region."""
    template, _student, printed, handwriting, _answer = _template_handwriting_page()
    mask = np.full((240, 320), 255, dtype=np.uint8)
    ys, xs = np.nonzero(handwriting)
    # A box that starts at the trace and runs up over the printed heading.
    regions = [{
        "question_number": "1",
        "regions": [[
            int(xs.min() * 1000 / 320),
            0,
            int((xs.max() + 1) * 1000 / 320),
            int((ys.max() + 1) * 1000 / 240),
        ]],
    }]
    covered_print = printed.copy()
    covered_print[:, :xs.min()] = False
    covered_print[:, xs.max() + 1:] = False
    covered_print[ys.max() + 1:, :] = False
    assert np.count_nonzero(covered_print) > 50

    # A blank page is the strongest form of this contract: the student and
    # template are identical, so any pixel reaching the evidence image came
    # through the model-approved box.
    answer_only = template_evidence._answer_only_pixels_v2(
        template.copy(), template, mask, None, regions,
    )

    assert _kept_ratio(answer_only, covered_print) > 0.90
    assert _kept_ratio(answer_only, handwriting) > 0.9


def test_exemption_box_over_answer_rule_keeps_student_overlap():
    """A fill answer written across the printed rule must survive an exemption.

    Students routinely write through the answer rule, and the downstream
    fill-line handling owns that overlap.  Covering a rule with an exemption box
    must therefore retain no less of the student stroke than using no box.
    """
    template = np.full((200, 400, 3), 240, dtype=np.uint8)
    cv2.line(template, (20, 120), (380, 120), (32, 32, 32), 2)
    rule = np.any(template < 200, axis=2)
    trace = np.zeros((200, 400), dtype=np.uint8)
    for step in range(50):
        cv2.circle(trace, (60 + step * 2, 70 + int(8 * math.sin(step / 8.0))), 2, 1, -1)
    template[trace.astype(bool)] = 58

    student = template.copy()
    answer = np.zeros((200, 400), dtype=np.uint8)
    for step in range(90):
        cv2.circle(answer, (200 + int(12 * math.sin(step / 20.0)), 60 + step), 3, 1, -1)
    answer = answer.astype(bool)
    student[answer] = 42
    on_rule = answer & rule
    assert np.count_nonzero(on_rule) > 0

    mask = np.full((200, 400), 255, dtype=np.uint8)
    regions = [{"question_number": "1", "regions": [[100, 250, 600, 650]]}]

    without = template_evidence._answer_only_pixels_v2(student, template, mask)
    covering = template_evidence._answer_only_pixels_v2(
        student, template, mask, None, regions,
    )

    assert _kept_ratio(covering, on_rule) >= _kept_ratio(without, on_rule)


def test_full_column_stroke_keeps_its_overlap_with_printed_question_text():
    """A handwritten answer crossing printed text must keep the shared pixels.

    The full-column contract passes no answer regions, and the residue cleanup
    used to drop every retained pixel within the dilated template neighborhood.
    That cut a band the width of the dilation out of any answer written over
    printed question text, with no step downstream to restore it.
    """
    template = np.full((200, 320, 3), 245, dtype=np.uint8)
    cv2.putText(
        template, "PRINTED", (40, 100), cv2.FONT_HERSHEY_SIMPLEX,
        1.0, (170, 170, 170), 2, cv2.LINE_AA,
    )
    printed = np.any(template < 225, axis=2)
    student = template.copy()
    cv2.line(student, (150, 40), (150, 170), (30, 30, 30), 5)
    stroke = np.any(student != template, axis=2)
    on_print = stroke & printed
    assert np.count_nonzero(on_print) > 10

    source_mask = np.full((200, 320), 255, dtype=np.uint8)
    answer_only = template_evidence._answer_only_pixels_v2(
        student, template, source_mask,
    )

    # The stroke survives where it crosses the printed row, while the printed
    # row itself stays out of the evidence image.
    assert _kept_ratio(answer_only, on_print) > 0.5
    assert _kept_ratio(answer_only, printed & ~stroke) < 0.02


def test_v2_residue_filter_keeps_a_supported_stroke_whole():
    template_gray = np.full((140, 220), 245, dtype=np.uint8)
    template_gray[68:72, 20:200] = 55
    student_gray = template_gray.copy()
    student_gray[20:120, 108:112] = 30
    candidate = np.zeros_like(template_gray, dtype=bool)
    candidate[20:120, 108:112] = True
    candidate[67:73, 40:44] = True
    real_stroke = np.zeros_like(candidate)
    real_stroke[20:120, 108:112] = True
    residue = np.zeros_like(candidate)
    residue[67:73, 40:44] = True

    cleaned = template_evidence._suppress_registered_template_residue_v2(
        candidate,
        student_gray,
        template_gray,
    )

    assert np.all(cleaned[real_stroke])
    assert not np.any(cleaned[residue])


def test_v2_normalizes_broad_photo_exposure_without_erasing_new_stroke():
    """A page-wide exposure gradient must not become template evidence."""
    height, width = 400, 600
    template = np.full((height, width, 3), 255, dtype=np.uint8)
    template[20:380, 20:580] = 235
    cv2.putText(
        template,
        "PRINTED QUESTION",
        (45, 75),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.1,
        (35, 35, 35),
        2,
        cv2.LINE_AA,
    )
    cv2.rectangle(template, (35, 95), (320, 175), (40, 40, 40), 2)

    y_grid, x_grid = np.indices((height, width))
    exposure = 45 * np.exp(-((x_grid / 250) ** 2 + (y_grid / 220) ** 2))
    student = np.clip(
        template.astype(np.float32) - exposure[:, :, None],
        0,
        255,
    ).astype(np.uint8)
    student[:20] = 255
    student[:, :20] = 255
    cv2.putText(
        student,
        "7",
        (250, 320),
        cv2.FONT_HERSHEY_SIMPLEX,
        2.2,
        (10, 10, 10),
        5,
        cv2.LINE_AA,
    )

    source_mask = np.full((height, width), 255, dtype=np.uint8)
    source_mask[:20] = 0
    source_mask[:, :20] = 0
    evidence = template_evidence._answer_only_pixels_v2(
        student,
        template,
        source_mask,
    )
    evidence_ink = np.any(evidence < 250, axis=2)

    printed_area = evidence_ink[20:200, 20:580]
    answer_area = evidence_ink[240:380, 200:340]
    assert int(np.count_nonzero(printed_area)) < 650
    assert int(np.count_nonzero(answer_area)) > 500


def test_fill_line_overlap_recovery_runs_without_answer_regions():
    """The full-column contract has no answer boxes but the same crossing case.

    Recovery used to be reachable only through answer regions, so a stroke
    written across a printed rule lost its overlap on every page rendered by the
    full-column contract. The two-sided support test, not the answer geometry,
    is what keeps a blank rule out of the evidence.
    """
    template = np.full((180, 320, 3), 245, dtype=np.uint8)
    cv2.line(template, (70, 92), (290, 92), (50, 50, 50), 2)
    rule = np.any(template < 225, axis=2)
    written = template.copy()
    # The center of the stroke is deliberately identical to the printed rule, so
    # only the recovery step can bring those shared pixels back.  That also means
    # the shared pixels cannot be found by comparing the two images: take them
    # from the geometry instead, where the stroke's columns meet the rule.
    cv2.line(written, (170, 82), (170, 104), (50, 50, 50), 2)
    stroke_columns = np.zeros(rule.shape, dtype=bool)
    stroke_columns[:, 169:172] = True
    crossing = rule & stroke_columns
    assert np.count_nonzero(crossing) > 0

    source_mask = np.full((180, 320), 255, dtype=np.uint8)
    blank_only = template_evidence._answer_only_pixels_v2(
        template.copy(), template, source_mask,
    )
    written_only = template_evidence._answer_only_pixels_v2(
        written, template, source_mask,
    )

    assert _kept_ratio(written_only, crossing) > 0.5
    # A rule with nothing written across it stays white.
    assert _kept_ratio(blank_only, rule) < 0.02


def test_multi_evidence_ignores_legacy_column_boundary(tmp_path):
    plan = build_anchor_crop_plan(
        {
            "column_layout": {
                "column_count": 2,
                "columns": [
                    {"column_index": 1, "x1_1000": 0, "x2_1000": 500},
                    {"column_index": 2, "x1_1000": 500, "x2_1000": 1000},
                ],
            },
        },
        artifact_sha256="b" * 64,
        expected_question_numbers=["1"],
    )
    layout = {
        "page_type_id": "page_1",
        "answer_regions": [{
            "question_number": "1",
            "question_type": "solve",
            "answer_bbox": [450, 300, 550, 500],
        }],
        "blank_detection_enabled": False,
        "question_crop_plan": plan,
    }
    image = np.full((700, 1000, 3), 255, dtype=np.uint8)
    source_mask = np.full((700, 1000), 255, dtype=np.uint8)

    result = build_multi_evidence_from_aligned(
        image,
        source_mask,
        image.copy(),
        layout,
        tmp_path,
    )

    assert result.question_numbers == ["1"]
    assert len(result.evidence_paths) == 1
    assert result.diagnostics["ownership"] == "confirmed_bbox_focus_sheets"
    cleanup_evidence(result.evidence_paths[0])


def test_fill_focus_capture_reaches_frozen_column_edge_without_crossing_column():
    plan = build_anchor_crop_plan(
        {
            "column_layout": {
                "column_count": 2,
                "columns": [
                    {"column_index": 1, "x1_1000": 0, "x2_1000": 500},
                    {"column_index": 2, "x1_1000": 500, "x2_1000": 1000},
                ],
            },
        },
        artifact_sha256="c" * 64,
        expected_question_numbers=["16", "17"],
    )
    regions = [
        {
            "question_number": "17",
            "question_type": "fill",
            "answer_bbox": [250, 500, 320, 560],
        },
        {
            "question_number": "16",
            "question_type": "fill",
            "answer_bbox": [600, 400, 670, 460],
        },
    ]

    sheets = build_focus_evidence_sheet_layout(
        regions,
        1000,
        1000,
        plan,
    )
    rows = {
        row["question_number"]: row
        for sheet in sheets
        for row in sheet["rows"]
    }

    assert rows["16"]["source_bbox"][0] == 500
    assert rows["16"]["source_bbox"][2] == 1000
    assert rows["17"]["source_bbox"][0] == 0
    assert rows["17"]["source_bbox"][2] == 500


def test_choice_focus_capture_stays_near_confirmed_bracket():
    plan = build_anchor_crop_plan(
        {
            "column_layout": {
                "column_count": 1,
                "columns": [
                    {"column_index": 1, "x1_1000": 0, "x2_1000": 1000},
                ],
            },
        },
        artifact_sha256="d" * 64,
        expected_question_numbers=["1"],
    )
    regions = [{
        "question_number": "1",
        "question_type": "choice",
        "answer_bbox": [600, 400, 670, 460],
    }]

    sheets = build_focus_evidence_sheet_layout(
        regions,
        1000,
        1000,
        plan,
    )

    assert sheets[0]["rows"][0]["source_bbox"][2] < 900


def test_legacy_evidence_entry_uses_frozen_fill_column_boundary(tmp_path):
    plan = build_anchor_crop_plan(
        {
            "column_layout": {
                "column_count": 1,
                "columns": [
                    {"column_index": 1, "x1_1000": 0, "x2_1000": 1000},
                ],
            },
        },
        artifact_sha256="e" * 64,
        expected_question_numbers=["16"],
    )
    image = np.full((1000, 1000, 3), 255, dtype=np.uint8)
    source_mask = np.full((1000, 1000), 255, dtype=np.uint8)
    layout = {
        "page_type_id": "page_1",
        "answer_regions": [{
            "question_number": "16",
            "question_type": "fill",
            "answer_bbox": [600, 400, 670, 460],
        }],
        "blank_detection_enabled": False,
        "question_crop_plan": plan,
    }

    result = template_evidence.build_evidence_from_aligned(
        image,
        source_mask,
        image.copy(),
        layout,
        tmp_path,
    )

    assert result.diagnostics["answer_source_coverage"][0]["crop_box"] == [
        0,
        385,
        1000,
        475,
    ]
    cleanup_evidence(result.evidence_path)


@pytest.mark.parametrize("quarter_turns", [1, 2, 3])
def test_matcher_normalizes_pixel_rotation_without_exif(
    tmp_path,
    quarter_turns,
):
    source = tmp_path / "template.png"
    template = _synthetic_template(source)
    photographed_path = tmp_path / "photographed.png"
    _perspective_student(template, photographed_path)
    with Image.open(photographed_path) as photographed:
        rotated = np.ascontiguousarray(np.rot90(
            np.asarray(photographed.convert("RGB")),
            k=quarter_turns,
        ))
    student = tmp_path / f"student-rotated-{quarter_turns}.png"
    Image.fromarray(rotated, mode="RGB").save(student)
    with Image.open(student) as stored:
        assert stored.getexif().get(274) is None

    template_dir = tmp_path / "stored"
    artifact = build_template_artifact(source, template_dir, 0, QUESTIONS)
    matcher = TemplateEvidenceMatcher({"page_1": artifact}, template_dir)

    result = matcher.match_and_build(student, tmp_path / "evidence")

    assert result.page_type_id == "page_1"
    candidate = result.diagnostics["candidates"][0]
    assert candidate["qualified"] is True
    assert candidate["coverage"] >= 0.78
    assert candidate["reprojection_rmse"] <= 6.0
    cleanup_evidence(result.evidence_path)


def test_matcher_rejects_partial_page_coverage(tmp_path):
    source = tmp_path / "template.png"
    template = _synthetic_template(source)
    partial = tmp_path / "partial.png"
    Image.fromarray(template[80:620, 180:820], mode="RGB").save(partial)
    template_dir = tmp_path / "stored"
    artifact = build_template_artifact(source, template_dir, 0, QUESTIONS)
    matcher = TemplateEvidenceMatcher({"page_1": artifact}, template_dir)

    with pytest.raises(TemplateEvidenceError, match="did not match") as error:
        matcher.match_and_build(partial, tmp_path / "evidence")

    candidate = error.value.diagnostics["candidates"][0]
    assert candidate["qualified"] is False
    assert candidate["coverage"] < 0.78


def test_answer_region_source_coverage_rejects_perspective_fill_pixels():
    mask = np.full((100, 100), 255, dtype=np.uint8)
    mask[:, :20] = 0
    coverage, incomplete = template_evidence._answer_region_source_coverage(
        mask,
        [
            {"question_number": "1", "answer_bbox": [0, 0, 300, 1000]},
            {"question_number": "2", "answer_bbox": [300, 0, 1000, 1000]},
        ],
    )

    assert incomplete == ["1"]
    assert coverage[0]["complete"] is False
    assert coverage[0]["coverage"] < 1.0
    assert coverage[1]["complete"] is True
    assert coverage[1]["coverage"] == 1.0


def test_source_mask_requires_every_positive_weight_tile_to_be_valid():
    # The overlap around x=50 blends both tiles.  The first transform shifts
    # real pixels away while the second remains valid; union/max would wrongly
    # mark the overlap complete.
    global_mask = np.full((40, 100), 255, dtype=np.uint8)
    corrections = [
        np.asarray([[1.0, 0.0, 60.0], [0.0, 1.0, 0.0]], dtype=np.float32),
        np.eye(2, 3, dtype=np.float32),
    ]

    refined = template_evidence._refine_source_mask(global_mask, corrections)

    assert np.all(refined[:, 52] == 0)
    assert np.all(refined[:, 70] == 255)


def test_answer_source_gate_ignores_context_padding_outside_raw_bbox():
    mask = np.full((1000, 1000), 255, dtype=np.uint8)
    # Raw bbox starts at x=600, but a fill crop expands right by 35 pixels.
    mask[:, 905:] = 0
    regions = [{
        "question_number": "25",
        "question_type": "fill",
        "answer_bbox": [600, 700, 900, 760],
    }]

    raw_coverage, raw_incomplete = template_evidence._answer_region_source_coverage(
        mask, regions
    )
    crop_coverage, crop_incomplete = template_evidence._answer_crop_source_coverage(
        mask, regions
    )

    assert raw_incomplete == []
    assert raw_coverage[0]["complete"] is True
    assert crop_incomplete == []
    assert crop_coverage[0]["complete"] is True
    assert crop_coverage[0]["coverage"] == 1.0
    assert crop_coverage[0]["crop_box"][2] > 900


def test_answer_source_gate_still_rejects_missing_raw_answer_pixels():
    mask = np.full((1000, 1000), 255, dtype=np.uint8)
    mask[:, 800:] = 0
    regions = [{
        "question_number": "25",
        "question_type": "fill",
        "answer_bbox": [600, 700, 900, 760],
    }]

    coverage, incomplete = template_evidence._answer_crop_source_coverage(
        mask, regions
    )

    assert incomplete == ["25"]
    assert coverage[0]["complete"] is False
    assert coverage[0]["coverage"] < 1.0


def test_local_refinement_rejects_extreme_affine(monkeypatch):
    image = np.full((700, 1000, 3), 255, dtype=np.uint8)
    points = np.asarray(
        [
            (x, y)
            for x in (80, 160, 240, 460, 540, 620, 780, 900)
            for y in (120, 320, 520)
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

    refined, diagnostics = template_evidence._refine_local_alignment(
        image,
        np.eye(3, dtype=np.float64),
        points,
        points,
    )

    assert np.array_equal(refined, image)
    assert all(item["applied"] is False for item in diagnostics)
    assert all(
        item["rejection_reason"] == "affine_translation_out_of_bounds"
        for item in diagnostics
    )


def test_final_answer_crop_does_not_extend_to_page_bottom():
    image = np.full((1000, 1000, 3), 255, dtype=np.uint8)
    regions = [{
        "question_number": "25",
        "question_type": "fill",
        "answer_bbox": [600, 700, 900, 760],
    }]

    crop = template_evidence._answer_crops(image, regions)[0]

    assert crop.shape[0] < 100


def test_neighbor_clamping_preserves_current_box_without_crossing_next_box():
    image = np.full((1000, 1000, 3), 255, dtype=np.uint8)
    image[760:766, 650:850] = 0
    regions = [
        {
            "question_number": "24",
            "question_type": "fill",
            "answer_bbox": [600, 700, 900, 760],
        },
        {
            "question_number": "25",
            "question_type": "fill",
            "answer_bbox": [600, 760, 900, 820],
        },
    ]

    question_24, question_25 = template_evidence._answer_crops(image, regions)

    assert int(question_24.min()) == 255
    assert int(question_25.min()) == 0


def test_choice_crop_keeps_small_left_overflow():
    image = np.full((1000, 1000, 3), 255, dtype=np.uint8)
    image[400:460, 585:599] = 0
    regions = [{
        "question_number": "13",
        "question_type": "choice",
        "answer_bbox": [600, 400, 700, 460],
    }]

    crop = template_evidence._answer_crops(image, regions)[0]

    assert int(crop.min()) == 0


def test_matcher_programmatically_confirms_truly_blank_regions(tmp_path):
    source = tmp_path / "template.png"
    _synthetic_template(source)
    template_dir = tmp_path / "stored"
    artifact = build_template_artifact(source, template_dir, 0, QUESTIONS)
    matcher = TemplateEvidenceMatcher({"page_1": artifact}, template_dir)

    result = matcher.match_and_build(source, tmp_path / "evidence")

    assert result.blank_question_numbers == []
    assert result.diagnostics["ownership"] == "full_column_difference"
    cleanup_evidence(result.evidence_path)


def test_answered_template_disables_program_blank_detection(tmp_path):
    source = tmp_path / "answered-template.png"
    _synthetic_template(source)
    template_dir = tmp_path / "stored"
    artifact = build_template_artifact(
        source,
        template_dir,
        0,
        QUESTIONS,
        blank_detection_enabled=False,
    )
    matcher = TemplateEvidenceMatcher({"page_1": artifact}, template_dir)

    result = matcher.match_and_build(source, tmp_path / "evidence")

    assert result.blank_question_numbers == []
    assert result.diagnostics["blank_detection_enabled"] is False
    assert result.diagnostics["program_blank_count"] == 0
    assert result.diagnostics["new_ink_pixels"] == {}
    cleanup_evidence(result.evidence_path)


def test_matcher_rejects_unmatched_and_ambiguous_pages(tmp_path):
    source = tmp_path / "template.png"
    template = _synthetic_template(source)
    student = tmp_path / "student.png"
    _perspective_student(template, student)
    template_dir = tmp_path / "stored"
    artifact = build_template_artifact(source, template_dir, 0, QUESTIONS)

    ambiguous = TemplateEvidenceMatcher(
        {"page_1": artifact, "page_2": copy.deepcopy(artifact)},
        template_dir,
    )
    with pytest.raises(TemplateEvidenceError, match="multiple templates"):
        ambiguous.match_and_build(student, tmp_path / "ambiguous")
    assert not (tmp_path / "ambiguous").exists()

    blank = tmp_path / "blank.png"
    Image.new("RGB", (1000, 700), "white").save(blank)
    matcher = TemplateEvidenceMatcher({"page_1": artifact}, template_dir)
    with pytest.raises(TemplateEvidenceError, match="did not match"):
        matcher.match_and_build(blank, tmp_path / "unmatched")
    assert not (tmp_path / "unmatched").exists()


def test_template_features_are_cached_lazily_across_threads(tmp_path):
    source = tmp_path / "template.png"
    _synthetic_template(source)
    template_dir = tmp_path / "stored"
    artifact = build_template_artifact(source, template_dir, 0, QUESTIONS)
    matcher = TemplateEvidenceMatcher({"page_1": artifact}, template_dir)
    assert matcher._feature_cache == {}

    with ThreadPoolExecutor(max_workers=6) as executor:
        features = list(executor.map(
            lambda _index: matcher._template_features("page_1"), range(12)
        ))

    assert len({id(value) for value in features}) == 1
    assert list(matcher._feature_cache) == ["page_1"]
    assert not hasattr(features[0], "rgb")


def test_twenty_template_features_remain_cached_for_second_match(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "template.png"
    _synthetic_template(source)
    template_dir = tmp_path / "stored"
    artifact = build_template_artifact(source, template_dir, 0, QUESTIONS)
    artifacts = {
        f"page_{index}": copy.deepcopy(artifact)
        for index in range(1, 21)
    }
    matcher = TemplateEvidenceMatcher(artifacts, template_dir)
    original_load = template_evidence._load_rgb_image
    loaded_paths = []

    def counted_load(path):
        loaded_paths.append(Path(path))
        return original_load(path)

    monkeypatch.setattr(template_evidence_artifacts, "_load_rgb_image", counted_load)
    monkeypatch.setattr(template_evidence, "_load_rgb_image", counted_load)

    with pytest.raises(TemplateEvidenceError, match="multiple templates"):
        matcher.match_and_build(source, tmp_path / "first")
    first_features = dict(matcher._feature_cache)

    with pytest.raises(TemplateEvidenceError, match="multiple templates"):
        matcher.match_and_build(source, tmp_path / "second")

    # Each invocation decodes its student input. Template pages are decoded and
    # feature-extracted only during the first pass, despite exceeding the old
    # four-entry LRU capacity.
    assert len(loaded_paths) == 22
    assert len(matcher._feature_cache) == 20
    assert all(
        matcher._feature_cache[page_type_id] is features
        for page_type_id, features in first_features.items()
    )
    assert all(not hasattr(value, "rgb") for value in matcher._feature_cache.values())


def test_cleanup_refuses_arbitrary_paths(tmp_path):
    arbitrary = tmp_path / "template.png"
    arbitrary.write_bytes(b"keep")
    with pytest.raises(ValueError, match="non-evidence"):
        cleanup_evidence(arbitrary)
    assert arbitrary.read_bytes() == b"keep"
