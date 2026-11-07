"""The template image a column layout is recognized from.

- ``_validate_template_image`` accepts only an existing JPG or PNG under
  ``_MAX_IMAGE_BYTES`` with sane dimensions and aspect ratio, and returns its
  path, SHA-256 (``_sha256_file``), size and format.
- ``_encode_template_data_url`` sends it as a Base64 data URL;
  ``_estimated_template_payload_bytes`` estimates how much memory that request holds.

Moved unchanged from services/template_column_layout.py, which still exposes these names.
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from typing import Any

from services.template_column_layout_contract import TemplateCutError


_MAX_IMAGE_BYTES = 10 * 1024 * 1024


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_template_image(image_path: str | os.PathLike[str]) -> dict[str, Any]:
    from PIL import Image

    path = Path(image_path)
    if not path.is_file():
        raise TemplateCutError("IMAGE_NOT_FOUND", "教师模板图片不存在")
    size_bytes = path.stat().st_size
    if size_bytes <= 0 or size_bytes >= _MAX_IMAGE_BYTES:
        raise TemplateCutError("IMAGE_SIZE_INVALID", "教师模板图片必须小于 10MB")
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
            image_format = str(image.format or "").lower()
    except (OSError, ValueError) as exc:
        raise TemplateCutError("IMAGE_INVALID", "教师模板图片无法解码") from exc
    if not (15 < width < 8192 and 15 < height < 8192):
        raise TemplateCutError(
            "IMAGE_DIMENSIONS_INVALID",
            "教师模板图片边长必须在 15 到 8192 像素之间",
        )
    if max(width, height) / float(min(width, height)) >= 50:
        raise TemplateCutError(
            "IMAGE_ASPECT_INVALID",
            "教师模板图片长宽比必须小于 50",
        )
    image_format = {"jpeg": "jpg", "jpg": "jpg", "png": "png"}.get(image_format, "")
    if not image_format:
        raise TemplateCutError(
            "IMAGE_TYPE_INVALID",
            "模板识别仅接受 JPG 或 PNG 图片",
        )
    return {
        "path": str(path),
        "sha256": _sha256_file(path),
        "width": width,
        "height": height,
        "size_bytes": size_bytes,
        "image_format": image_format,
    }


def _estimated_template_payload_bytes(raw_bytes: int) -> int:
    """Estimate raw, Base64, JSON text, and serialized HTTP body residency."""
    if raw_bytes < 1:
        raise ValueError("template image payload is empty")
    encoded_bytes = 4 * ((int(raw_bytes) + 2) // 3)
    return int(raw_bytes) + 3 * encoded_bytes


def _encode_template_data_url(image_path: str, mime: str) -> str:
    image_bytes = Path(image_path).read_bytes()
    return f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"
