from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest
from PIL import Image

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

import mathagent_worker.canonicalization as canonicalization_module
from mathagent_worker.canonicalization import (
    CANONICALIZATION_ALGORITHM,
    CANONICALIZATION_VERSION,
    CANONICAL_COORDINATE_SYSTEM,
    CANONICAL_PAGE_MARGIN,
    CANONICAL_SOURCE_COORDINATE_SYSTEM,
    TemplateCanonicalizationError,
    canonicalize_template,
)
from mathagent_worker.protocol import serve


CANONICAL_CANDIDATE_KEYS = {
    "version",
    "algorithm",
    "source_sha256",
    "canonical_sha256",
    "source_format",
    "exif_orientation",
    "encoded_width",
    "encoded_height",
    "source_width",
    "source_height",
    "canonical_width",
    "canonical_height",
    "source_coordinate_space",
    "canonical_coordinate_space",
    "source_to_canonical",
    "detected_page_quad",
    "detection_method",
    "confidence",
    "page_margin_px",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _synthetic_page(seed: int = 42) -> np.ndarray:
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
    return image


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
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(photographed, mode="RGB").save(path)
    return page_quad


def _workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "rust-workspace"
    source = root / "incoming" / "teacher.png"
    output = root / "canonical" / "template.png"
    _template_photo(_synthetic_page(), source)
    return root, source, output


def test_canonicalize_template_rectifies_and_binds_complete_metadata(tmp_path) -> None:
    root, source, output = _workspace(tmp_path)
    raw_before = source.read_bytes()

    result = canonicalize_template(root, source, output)
    metadata = result.canonicalization

    assert result.canonical_path == output.resolve()
    assert output.is_file()
    assert source.read_bytes() == raw_before
    assert set(metadata) == CANONICAL_CANDIDATE_KEYS
    assert metadata["version"] == CANONICALIZATION_VERSION
    assert metadata["algorithm"] == CANONICALIZATION_ALGORITHM
    assert "source_image" not in metadata
    assert metadata["source_sha256"] == hashlib.sha256(raw_before).hexdigest()
    assert metadata["source_coordinate_space"] == CANONICAL_SOURCE_COORDINATE_SYSTEM
    assert metadata["canonical_coordinate_space"] == CANONICAL_COORDINATE_SYSTEM
    assert metadata["canonical_sha256"] == _sha256(output)
    assert metadata["detection_method"] == "detected_quadrilateral"
    assert metadata["confidence"] >= 0.68
    assert metadata["canonical_width"] <= 2000 + 2 * CANONICAL_PAGE_MARGIN
    assert metadata["canonical_height"] <= 2000 + 2 * CANONICAL_PAGE_MARGIN
    with Image.open(output) as canonical:
        assert canonical.format == "PNG"
        assert canonical.size == (
            metadata["canonical_width"],
            metadata["canonical_height"],
        )

    matrix = np.asarray(metadata["source_to_canonical"], dtype=np.float64)
    quad = np.asarray(metadata["detected_page_quad"], dtype=np.float32)
    assert matrix.shape == (3, 3)
    assert np.isfinite(matrix).all()
    assert abs(float(np.linalg.det(matrix))) >= 1e-9
    projected = cv2.perspectiveTransform(
        quad.reshape(-1, 1, 2), matrix
    ).reshape(-1, 2)
    margin = metadata["page_margin_px"]
    expected = np.float32([
        [margin, margin],
        [metadata["canonical_width"] - margin - 1, margin],
        [
            metadata["canonical_width"] - margin - 1,
            metadata["canonical_height"] - margin - 1,
        ],
        [margin, metadata["canonical_height"] - margin - 1],
    ])
    assert float(np.max(np.abs(projected - expected))) < 1.0
    json.dumps(metadata, allow_nan=False)


def test_canonicalization_is_byte_and_metadata_deterministic(tmp_path) -> None:
    root, source, first_output = _workspace(tmp_path)
    second_output = root / "canonical-repeat" / "template.png"

    first = canonicalize_template(root, source, first_output)
    second = canonicalize_template(root, source, second_output)

    assert first_output.read_bytes() == second_output.read_bytes()
    assert first.canonicalization == second.canonicalization
    assert first.canonicalization["canonical_sha256"] == _sha256(second_output)


def test_canonicalization_applies_exif_before_defining_source_coordinates(tmp_path) -> None:
    root = tmp_path / "rust-workspace"
    source = root / "incoming" / "teacher.jpg"
    output = root / "canonical" / "template.png"
    source.parent.mkdir(parents=True)
    display_page = Image.fromarray(_synthetic_page(), mode="RGB")
    encoded = display_page.transpose(Image.Transpose.ROTATE_90)
    exif = Image.Exif()
    exif[274] = 6
    encoded.save(source, format="JPEG", quality=95, exif=exif)

    result = canonicalize_template(root, source, output)
    metadata = result.canonicalization

    assert (metadata["encoded_width"], metadata["encoded_height"]) == encoded.size
    assert (metadata["source_width"], metadata["source_height"]) == display_page.size
    assert metadata["exif_orientation"] == 6
    assert metadata["source_coordinate_space"] == "exif_display_pixels_top_left"


def test_canonicalization_accepts_phone_mpo_jpeg_primary_frame(tmp_path) -> None:
    root = tmp_path / "rust-workspace"
    source = root / "incoming" / "teacher.jpg"
    output = root / "canonical" / "template.png"
    source.parent.mkdir(parents=True)
    primary = Image.fromarray(_synthetic_page(), mode="RGB")
    primary.save(
        source,
        format="MPO",
        save_all=True,
        append_images=[Image.new("RGB", primary.size, "black")],
    )

    result = canonicalize_template(root, source, output)

    assert output.is_file()
    assert result.canonicalization["source_format"] == "jpeg"


def test_canonicalization_accepts_content_template_without_visible_page_corners(tmp_path) -> None:
    root = tmp_path / "rust-workspace"
    source = root / "incoming" / "teacher.png"
    output = root / "canonical" / "template.png"
    source.parent.mkdir(parents=True)
    page = _synthetic_page()
    # Simulate scanner/tight-crop output: printed content reaches every image
    # edge, so there is no bright surround or independently visible paper quad.
    page[:12, :] = 35
    page[-12:, :] = 35
    page[:, :12] = 35
    page[:, -12:] = 35
    Image.fromarray(page, mode="RGB").save(source)

    result = canonicalize_template(root, source, output)

    assert output.is_file()
    assert result.canonicalization["detection_method"] in {
        "detected_quadrilateral",
        "full_frame_scan",
    }


@pytest.mark.parametrize("kind", ["invalid", "blank"])
def test_canonicalization_rejects_bad_images_without_output(tmp_path, kind) -> None:
    root = tmp_path / "rust-workspace"
    source = root / "incoming" / "teacher.png"
    output = root / "canonical" / "template.png"
    source.parent.mkdir(parents=True)
    if kind == "invalid":
        source.write_bytes(b"not an image")
        expected_code = "template_source_invalid"
    else:
        Image.new("RGB", (900, 1200), "white").save(source)
        expected_code = "template_page_rejected"

    with pytest.raises(TemplateCanonicalizationError) as error:
        canonicalize_template(root, source, output)

    assert error.value.code == expected_code
    assert not output.exists()


def test_non_finite_transform_is_rejected_before_output(tmp_path, monkeypatch) -> None:
    root, source, output = _workspace(tmp_path)
    real_canonical_page = canonicalization_module._canonical_page

    def non_finite(loaded):
        image, metadata = real_canonical_page(loaded)
        metadata["source_to_canonical"][0][0] = float("nan")
        return image, metadata

    monkeypatch.setattr(canonicalization_module, "_canonical_page", non_finite)

    with pytest.raises(TemplateCanonicalizationError) as error:
        canonicalize_template(root, source, output)

    assert error.value.code == "template_metadata_invalid"
    assert not output.exists()


def test_paths_must_resolve_inside_rust_workspace(tmp_path) -> None:
    root, source, output = _workspace(tmp_path)
    outside_source = tmp_path / "outside.png"
    Image.fromarray(_synthetic_page(), mode="RGB").save(outside_source)

    cases = [
        (root, outside_source, output, "path_outside_workspace"),
        (root, source, tmp_path / "outside-output.png", "path_outside_workspace"),
        (Path("relative-root"), source, output, "workspace_root_invalid"),
        (root, source, root / "canonical" / "template.jpg", "canonical_path_invalid"),
        (root, source, source, "canonical_path_invalid"),
    ]
    for workspace, input_path, output_path, expected in cases:
        with pytest.raises(TemplateCanonicalizationError) as error:
            canonicalize_template(workspace, input_path, output_path)
        assert error.value.code == expected


def test_real_sidecar_command_returns_only_bounded_canonical_contract(tmp_path) -> None:
    root, source, output = _workspace(tmp_path)
    request = json.dumps({
        "command": "canonicalize_template",
        "requestId": "canonical-1",
        "workspaceRoot": str(root.resolve()),
        "sourcePath": str(source.resolve()),
        "canonicalPath": str(output.resolve()),
    })
    output_stream = io.StringIO()

    assert serve(
        io.StringIO(request + "\n" + '{"command":"shutdown"}\n'),
        output_stream,
    ) == 0

    messages = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    response = messages[1]
    assert response["status"] == "ok"
    assert response["requestId"] == "canonical-1"
    assert response["canonicalPath"] == str(output.resolve())
    assert "source_image" not in response["canonicalization"]
    assert set(response) == {
        "status",
        "canonicalPath",
        "canonicalization",
        "requestId",
    }
    assert "sourcePath" not in response
    assert messages[-1] == {"status": "stopping"}
