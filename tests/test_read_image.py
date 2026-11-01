"""ocr_service.read_image: 识图 and roster photos on the Flash transcription route."""
import asyncio
import base64
import io
import os

import pytest
from PIL import Image

from services import ocr_service


async def _read_then_teacher(*args, **kwargs):
    """Run read_image, then report the teacher id left in the same task."""
    text = await ocr_service.read_image(*args, **kwargs)
    return text, ocr_service._vision_teacher_id.get()


@pytest.fixture
def requests(monkeypatch):
    calls = []

    async def fake_call_vision(path, prompt, *, max_tokens):
        with open(path, "rb") as handle:
            data = handle.read()
        calls.append({
            "path": path,
            "bytes": data,
            "prompt": prompt,
            "max_tokens": max_tokens,
            "teacher": ocr_service._vision_teacher_id.get(),
        })
        return "名单文字"

    monkeypatch.setattr(ocr_service, "call_vision", fake_call_vision)
    return calls


@pytest.mark.parametrize(("mime", "suffix"), [
    ("image/png", ".png"),
    ("image/jpg", ".jpg"),
    (" IMAGE/WEBP ", ".webp"),
])
def test_supported_image_is_sent_unchanged_under_the_teacher(requests, mime, suffix):
    raw = b"not-decoded-image-bytes"

    text, teacher_after = asyncio.run(_read_then_teacher(
        raw, mime, "识别名单", teacher_id="teacher-4", max_tokens=512,
    ))

    [call] = requests
    assert text == "名单文字"
    assert call["bytes"] == raw
    assert call["path"].endswith(suffix)
    assert call["prompt"] == "识别名单"
    assert call["max_tokens"] == 512
    assert call["teacher"] == "teacher-4"
    assert teacher_after == "system"
    assert not os.path.exists(os.path.dirname(call["path"]))


def test_base64_text_is_decoded_before_the_request(requests):
    raw = b"jpeg-bytes"

    asyncio.run(ocr_service.read_image(base64.b64encode(raw).decode(), "image/jpeg", "识别"))

    assert requests[0]["bytes"] == raw
    assert requests[0]["path"].endswith(".jpg")
    assert requests[0]["teacher"] == "system"


def test_other_formats_are_reencoded_as_png(requests):
    buffer = io.BytesIO()
    Image.new("RGB", (4, 3), "white").save(buffer, format="BMP")

    asyncio.run(ocr_service.read_image(buffer.getvalue(), "image/bmp", "识别"))

    assert requests[0]["path"].endswith(".png")
    assert requests[0]["bytes"].startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.parametrize("image", [b"", "", "not base64!"])
def test_empty_or_invalid_input_fails_before_any_request(requests, image):
    with pytest.raises(ValueError):
        asyncio.run(ocr_service.read_image(image, "image/png", "识别"))
    assert requests == []


def test_failed_request_still_removes_the_image_and_restores_the_teacher(monkeypatch):
    paths = []

    async def failing_call_vision(path, prompt, *, max_tokens):
        paths.append(path)
        raise RuntimeError("provider down")

    monkeypatch.setattr(ocr_service, "call_vision", failing_call_vision)

    async def run():
        with pytest.raises(RuntimeError, match="provider down"):
            await ocr_service.read_image(b"image", "image/png", "识别", teacher_id="teacher-5")
        return ocr_service._vision_teacher_id.get()

    assert asyncio.run(run()) == "system"
    assert not os.path.exists(os.path.dirname(paths[0]))
