"""Fail-closed orientation checks for uploaded paper images."""

from __future__ import annotations

import hashlib
import importlib
import os
from pathlib import Path
import uuid

MAX_SOURCE_PIXELS = 16_000_000


class _LazyModule:
    """Keep disabled legacy orientation algorithms out of web-worker startup."""

    def __init__(self, module_name: str):
        object.__setattr__(self, "_module_name", module_name)
        object.__setattr__(self, "_module", None)

    def _load(self):
        module = object.__getattribute__(self, "_module")
        if module is None:
            module = importlib.import_module(object.__getattribute__(self, "_module_name"))
            object.__setattr__(self, "_module", module)
        return module

    def __getattr__(self, name):
        return getattr(self._load(), name)

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        setattr(self._load(), name, value)


Image = _LazyModule("PIL.Image")
ImageOps = _LazyModule("PIL.ImageOps")


def _dimensions_are_safe(image: Image.Image) -> bool:
    width, height = image.size
    return width > 0 and height > 0 and width * height <= MAX_SOURCE_PIXELS


def orientation_is_ready(paper: dict) -> bool:
    return str(paper.get("orientation_status") or "") in {
        "auto_passed",
        "teacher_confirmed",
    }


def sha256_file(path: str | Path) -> str:
    """Return a stable source identity without exposing image bytes."""
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_teacher_oriented_copy(
    original_path: str | Path,
    output_dir: str | Path,
    paper_id: str,
    quarter_turns_clockwise: int,
) -> dict:
    """Create a lossless, EXIF-normalized working copy; never rewrite source.

    The destination is deterministic for one source hash and selected rotation,
    so an HTTP retry reuses the same artifact instead of accumulating copies.
    """
    if isinstance(quarter_turns_clockwise, bool):
        raise ValueError("quarter_turns_clockwise must be 0, 1, 2, or 3")
    try:
        turns = int(quarter_turns_clockwise)
    except (TypeError, ValueError) as exc:
        raise ValueError("quarter_turns_clockwise must be 0, 1, 2, or 3") from exc
    if turns not in {0, 1, 2, 3}:
        raise ValueError("quarter_turns_clockwise must be 0, 1, 2, or 3")

    source = Path(original_path).resolve()
    root = Path(output_dir).resolve()
    if not source.is_file():
        raise ValueError("original paper image does not exist")
    root.mkdir(parents=True, exist_ok=True)
    source_hash = sha256_file(source)
    safe_paper_id = hashlib.sha256(str(paper_id).encode("utf-8")).hexdigest()[:12]
    destination = root / f"teacher-oriented-{safe_paper_id}-{source_hash[:16]}-q{turns}.png"

    if not destination.is_file():
        temporary = root / f".{destination.name}.{uuid.uuid4().hex}.tmp"
        try:
            with Image.open(source) as opened:
                if not _dimensions_are_safe(opened):
                    raise ValueError("paper image dimensions are too large")
                opened.load()
                image = ImageOps.exif_transpose(opened)
                if image.mode in {"RGBA", "LA"} or (
                    image.mode == "P" and "transparency" in image.info
                ):
                    rgba = image.convert("RGBA")
                    background = Image.new("RGBA", rgba.size, "white")
                    image = Image.alpha_composite(background, rgba).convert("RGB")
                elif image.mode not in {"RGB", "L"}:
                    image = image.convert("RGB")

                transpose = {
                    1: Image.Transpose.ROTATE_270,
                    2: Image.Transpose.ROTATE_180,
                    3: Image.Transpose.ROTATE_90,
                }.get(turns)
                if transpose is not None:
                    image = image.transpose(transpose)
                image.save(temporary, format="PNG", optimize=True)
            os.replace(temporary, destination)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    return {
        "original_image_path": str(source),
        "image_path": str(destination),
        "source_sha256": source_hash,
        "working_sha256": sha256_file(destination),
        "manual_quarter_turns": turns,
    }
