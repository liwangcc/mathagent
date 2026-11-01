from __future__ import annotations

from pathlib import Path

from PIL import Image


DESKTOP_ROOT = Path(__file__).resolve().parents[1]
ICON_DIRECTORY = DESKTOP_ROOT / "src-tauri/icons"
SOURCE = ICON_DIRECTORY / "app-icon-source.png"

ICO_SIZES = [
    (16, 16),
    (24, 24),
    (32, 32),
    (48, 48),
    (64, 64),
    (128, 128),
    (256, 256),
]


def main() -> None:
    ICON_DIRECTORY.mkdir(parents=True, exist_ok=True)
    with Image.open(SOURCE) as source:
        if source.width != source.height:
            raise ValueError(f"Icon source must be square, got {source.size}")

        rgba = source.convert("RGBA")
        rgba.resize((32, 32), Image.Resampling.LANCZOS).save(
            ICON_DIRECTORY / "32x32.png"
        )
        rgba.resize((128, 128), Image.Resampling.LANCZOS).save(
            ICON_DIRECTORY / "128x128.png"
        )
        rgba.save(
            ICON_DIRECTORY / "icon.ico",
            sizes=ICO_SIZES,
        )


if __name__ == "__main__":
    main()
