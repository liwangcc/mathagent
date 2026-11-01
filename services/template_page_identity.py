"""Coarse visual fingerprint of a template page, for page-type routing only.

A 16x16 difference hash of the whole page: stable under scale, JPEG noise and
light handwriting, sensitive to layout (an answer sheet's boxes versus a
question page's text blocks).  It is a supporting routing signal, never an
authority on its own -- callers accept it only with a clear margin.

Pillow is imported lazily: app startup must not load the image runtime.
"""
from __future__ import annotations

FINGERPRINT_PREFIX = "dhash256:"
_HASH_SIZE = 16
# A conclusive visual match must be close to one page and clearly closer to it
# than to every other page. Distances are normalized Hamming distances (0..1).
VISUAL_MATCH_MAX_DISTANCE = 0.25
VISUAL_MATCH_MIN_MARGIN = 0.12


def compute_visual_fingerprint(image_or_path) -> str:
    """Return ``dhash256:<64 hex>`` for an image path or PIL image."""
    from PIL import Image, ImageOps

    if isinstance(image_or_path, Image.Image):
        image = image_or_path.convert("L")
    else:
        with Image.open(image_or_path) as opened:
            image = opened.convert("L")
    image = ImageOps.autocontrast(image)
    small = image.resize((_HASH_SIZE + 1, _HASH_SIZE), Image.Resampling.LANCZOS)
    pixels = small.tobytes()
    bits = 0
    for row in range(_HASH_SIZE):
        offset = row * (_HASH_SIZE + 1)
        for column in range(_HASH_SIZE):
            bits = (bits << 1) | (pixels[offset + column] > pixels[offset + column + 1])
    return FINGERPRINT_PREFIX + f"{bits:064x}"


def fingerprint_distance(left: str, right: str) -> float | None:
    """Normalized Hamming distance, or ``None`` when either side is unusable."""
    values = []
    for value in (left, right):
        text = str(value or "").strip().lower()
        if not text.startswith(FINGERPRINT_PREFIX):
            return None
        try:
            values.append(int(text[len(FINGERPRINT_PREFIX):], 16))
        except ValueError:
            return None
    return bin(values[0] ^ values[1]).count("1") / (_HASH_SIZE * _HASH_SIZE)


def visual_winner(fingerprint: str, candidates: dict[str, str]) -> str:
    """Return the one page label that visually wins with a clear margin, else ``""``.

    At least two fingerprinted pages are required: with one page there is no
    margin to measure, so the signal stays silent rather than confirming.
    """
    ranked = sorted(
        (distance, label)
        for label, candidate in candidates.items()
        if (distance := fingerprint_distance(fingerprint, candidate)) is not None
    )
    if len(ranked) < 2:
        return ""
    best_distance, best_label = ranked[0]
    if best_distance > VISUAL_MATCH_MAX_DISTANCE:
        return ""
    if ranked[1][0] - best_distance < VISUAL_MATCH_MIN_MARGIN:
        return ""
    return best_label
