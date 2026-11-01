from PIL import Image, ImageDraw

from services.paper_orientation import (
    create_teacher_oriented_copy,
    sha256_file,
)


def test_teacher_rotation_keeps_original_and_is_idempotent(tmp_path):
    source = tmp_path / "source.png"
    image = Image.new("RGB", (120, 80), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 39, 79), fill="red")
    image.save(source)
    before = source.read_bytes()
    source_hash = sha256_file(source)

    first = create_teacher_oriented_copy(source, tmp_path, "paper-1", 1)
    second = create_teacher_oriented_copy(source, tmp_path, "paper-1", 1)

    assert first == second
    assert first["source_sha256"] == source_hash
    assert first["image_path"] != str(source)
    assert source.read_bytes() == before
    with Image.open(first["image_path"]) as rotated:
        assert rotated.size == (80, 120)
        assert rotated.getpixel((79, 0))[0] > 200
        assert rotated.getpixel((79, 0))[1] < 80

    half_turn = create_teacher_oriented_copy(source, tmp_path, "paper-1", 2)
    counter_clockwise = create_teacher_oriented_copy(source, tmp_path, "paper-1", 3)
    with Image.open(half_turn["image_path"]) as rotated:
        assert rotated.size == (120, 80)
        assert rotated.getpixel((119, 79))[0] > 200
        assert rotated.getpixel((119, 79))[1] < 80
    with Image.open(counter_clockwise["image_path"]) as rotated:
        assert rotated.size == (80, 120)
        assert rotated.getpixel((0, 119))[0] > 200
        assert rotated.getpixel((0, 119))[1] < 80
    assert source.read_bytes() == before


def test_teacher_rotation_rejects_non_quarter_turn(tmp_path):
    source = tmp_path / "source.png"
    Image.new("RGB", (80, 120), "white").save(source)
    try:
        create_teacher_oriented_copy(source, tmp_path, "paper-1", 4)
    except ValueError as exc:
        assert "0, 1, 2, or 3" in str(exc)
    else:
        raise AssertionError("invalid quarter turn must be rejected")


def test_teacher_rotation_rejects_pixel_bomb_before_decode(monkeypatch, tmp_path):
    source = tmp_path / "oversized.png"
    source.write_bytes(b"header-only-test")

    class OversizedImage:
        size = (5_001, 5_000)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def load(self):
            raise AssertionError("oversized image must not be decoded")

    monkeypatch.setattr(Image, "open", lambda *_args, **_kwargs: OversizedImage())

    try:
        create_teacher_oriented_copy(source, tmp_path, "paper-oversized", 1)
    except ValueError as exc:
        assert "dimensions" in str(exc)
    else:
        raise AssertionError("oversized image must be rejected")
