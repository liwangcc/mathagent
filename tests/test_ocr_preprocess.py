from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageDraw

from services import preprocess


def test_preprocessed_paper_uses_compact_high_quality_jpeg(monkeypatch, tmp_path):
    source = tmp_path / "paper.png"
    image = Image.new("RGB", (900, 1200), "white")
    draw = ImageDraw.Draw(image)
    for y in range(80, 1120, 32):
        draw.line((70, y, 830, y), fill="black", width=2)
    image.save(source, format="PNG")

    monkeypatch.setattr(preprocess, "auto_rotate", lambda value: value)
    monkeypatch.setattr(preprocess, "auto_deskew", lambda value: value)
    monkeypatch.setattr(preprocess, "fix_perspective", lambda value: value)

    output = Path(preprocess.preprocess_paper_image(str(source)))

    assert output.name.startswith("paper_preprocessed-")
    assert output.suffix == ".jpg"
    assert output.is_file()
    with Image.open(output) as encoded:
        assert encoded.format == "JPEG"
        assert encoded.mode == "RGB"
        assert encoded.size == image.size
    # Bound payload size without assuming that a synthetic sparse PNG is larger;
    # the explicit JPEG format assertion above is the regression contract.
    assert output.stat().st_size < image.width * image.height


def test_concurrent_preprocess_calls_use_independent_atomic_outputs(tmp_path):
    source = tmp_path / "shared-paper.png"
    Image.new("RGB", (320, 480), "white").save(source)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(
            lambda _index: preprocess.preprocess_paper_image(
                str(source), mode="fast"
            ),
            range(2),
        ))

    assert outputs[0] != outputs[1]
    for output in map(Path, outputs):
        assert output.is_file()
        with Image.open(output) as encoded:
            assert encoded.size == (320, 480)


def test_off_mode_returns_upload_without_opening_or_copying(monkeypatch, tmp_path):
    source = tmp_path / "paper.jpg"

    def fail_if_opened(*_args, **_kwargs):
        raise AssertionError("off mode must not decode the upload")

    monkeypatch.setattr(preprocess.Image, "open", fail_if_opened)

    assert preprocess.preprocess_paper_image(str(source), mode="off") == str(source)
    assert not (tmp_path / "paper_preprocessed.jpg").exists()


def test_fast_mode_normalizes_orientation_and_bounds_long_edge(monkeypatch, tmp_path):
    source = tmp_path / "sideways-large.jpg"
    image = Image.new("RGB", (2400, 1200), "white")
    exif = Image.Exif()
    exif[274] = 6
    image.save(source, format="JPEG", exif=exif)

    def fail_if_called(_image):
        raise AssertionError("fast mode must skip enhanced processing")

    monkeypatch.setattr(preprocess, "auto_deskew", fail_if_called)
    monkeypatch.setattr(preprocess, "fix_perspective", fail_if_called)

    output = Path(
        preprocess.preprocess_paper_image(str(source), mode="fast", max_edge=600)
    )

    with Image.open(output) as encoded:
        assert encoded.format == "JPEG"
        assert encoded.mode == "RGB"
        assert encoded.size == (300, 600)
        assert encoded.getexif().get(274, 1) == 1


def test_enhanced_mode_retains_expensive_pipeline_after_resize(monkeypatch, tmp_path):
    source = tmp_path / "large-paper.png"
    Image.new("RGB", (1600, 800), "white").save(source, format="PNG")
    calls = []

    monkeypatch.setattr(preprocess, "auto_rotate", lambda image: image)

    def record_deskew(image):
        calls.append(("deskew", image.size))
        return image

    def record_perspective(image):
        calls.append(("perspective", image.size))
        return image

    monkeypatch.setattr(preprocess, "auto_deskew", record_deskew)
    monkeypatch.setattr(preprocess, "fix_perspective", record_perspective)

    output = Path(
        preprocess.preprocess_paper_image(
            str(source), mode="enhanced", max_edge=400
        )
    )

    assert calls == [("deskew", (400, 200)), ("perspective", (400, 200))]
    with Image.open(output) as encoded:
        assert encoded.size == (400, 200)


def test_student_ocr_copy_suppresses_red_green_review_ink_but_keeps_blue_black(tmp_path):
    source = tmp_path / "marked-paper.png"
    image = Image.new("RGB", (240, 80), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, 50, 70), fill=(220, 20, 20))
    draw.rectangle((70, 10, 110, 70), fill=(20, 150, 30))
    draw.rectangle((130, 10, 170, 70), fill=(20, 50, 210))
    draw.rectangle((190, 10, 230, 70), fill=(20, 20, 20))
    image.save(source)

    output = Path(preprocess.preprocess_paper_image(
        str(source), mode="fast", suppress_review_ink=True,
    ))

    with Image.open(output) as encoded:
        red = encoded.getpixel((30, 40))
        green = encoded.getpixel((90, 40))
        blue = encoded.getpixel((150, 40))
        black = encoded.getpixel((210, 40))
    assert min(red) > 240
    assert min(green) > 240
    assert blue[2] > blue[0] + 80 and blue[2] > blue[1] + 80
    assert max(black) < 60


def test_preprocess_rejects_invalid_mode_and_max_edge(tmp_path):
    source = tmp_path / "paper.png"
    Image.new("RGB", (20, 20), "white").save(source)

    for mode in ("", "unknown"):
        try:
            preprocess.preprocess_paper_image(str(source), mode=mode)
        except ValueError:
            pass
        else:
            raise AssertionError(f"mode {mode!r} should be rejected")

    for max_edge in (0, -1, "not-a-number"):
        try:
            preprocess.preprocess_paper_image(
                str(source), mode="fast", max_edge=max_edge
            )
        except ValueError:
            pass
        else:
            raise AssertionError(f"max_edge {max_edge!r} should be rejected")


def test_oversized_source_is_rejected_before_pixel_decode(monkeypatch, tmp_path):
    source = tmp_path / "oversized.png"
    source.write_bytes(b"header-only-test")

    class OversizedImage:
        size = (10_000, 8_001)
        loaded = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def load(self):
            self.loaded = True
            raise AssertionError("oversized source must not be decoded")

        def draft(self, *_args, **_kwargs):
            raise AssertionError("oversized source must be rejected before draft")

    oversized = OversizedImage()
    monkeypatch.setattr(preprocess.Image, "open", lambda *_args, **_kwargs: oversized)

    try:
        preprocess.preprocess_paper_image(str(source), mode="fast")
    except ValueError as exc:
        assert "preprocessing failed" in str(exc)
    else:
        raise AssertionError("oversized source must fail closed")
    assert oversized.loaded is False


def test_display_image_copy_applies_exif_orientation_without_changing_upload(tmp_path):
    source = tmp_path / "sideways-paper.jpg"
    image = Image.new("RGB", (80, 40), "white")
    exif = Image.Exif()
    exif[274] = 6  # Rotate 90 degrees clockwise for display.
    image.save(source, format="JPEG", exif=exif)

    output = Path(preprocess.get_display_image_path(str(source)))

    assert output != source
    assert output.name == "sideways-paper_oriented.jpg"
    with Image.open(source) as original:
        assert original.size == (80, 40)
        assert original.getexif().get(274) == 6
    with Image.open(output) as oriented:
        assert oriented.size == (40, 80)
        assert oriented.getexif().get(274, 1) == 1
    assert preprocess.get_display_image_path(str(source)) == str(output)
