import asyncio
import time
from pathlib import Path

import pytest
from PIL import Image

from services import ocr_service
from services.identity_regions import IDENTITY_LEFT_CROP_RATIO, IDENTITY_TOP_CROP_RATIO
from services.ocr_service import _sanitize_student_name
from services import ocr_service_local_image
from services import ocr_service_template_route


@pytest.mark.parametrize("raw", [
    "无法识别",
    "未识别姓名",
    "姓名看不清",
    "没有姓名",
    "学号：123456",
])
def test_student_name_sanitizer_rejects_status_text(raw):
    assert _sanitize_student_name(raw) == ""


def test_student_name_sanitizer_keeps_real_chinese_name():
    assert _sanitize_student_name("姓名：吴静") == "吴静"


def test_identity_crop_ratios_are_shared_with_ocr_service():
    assert ocr_service.IDENTITY_TOP_CROP_RATIO == IDENTITY_TOP_CROP_RATIO == 0.25
    assert ocr_service.IDENTITY_LEFT_CROP_RATIO == IDENTITY_LEFT_CROP_RATIO == 0.15


def test_roster_identity_retry_uses_top_and_left_regions_and_auto_matches_one_high_confidence_candidate(tmp_path, monkeypatch):
    """The retry preserves both source regions and only accepts a roster candidate."""
    source_path = tmp_path / "paper.png"
    source = Image.new("RGB", (100, 200), "white")
    try:
        source.paste((255, 0, 0), (0, 0, 100, 50))       # top 25%
        source.paste((0, 255, 0), (0, 50, 15, 200))       # left 15%, below top
        source.save(source_path)
    finally:
        source.close()

    captured = {}

    async def fake_call_vision(crop_path, prompt, max_tokens):
        captured["path"] = crop_path
        captured["prompt"] = prompt
        captured["max_tokens"] = max_tokens
        with Image.open(crop_path) as crop:
            # No source-region scaling: 100x50 header over a clockwise-rotated
            # full 15x200 left strip (200x15).
            assert crop.size == (200, 65)
            top_pixel = crop.getpixel((50, 25))
            left_pixel = crop.getpixel((75, 57))
            spacer_pixel = crop.getpixel((150, 25))
            assert top_pixel[0] > top_pixel[1] + 100
            assert left_pixel[1] > left_pixel[0] + 100
            assert min(spacer_pixel) > 200
        return '{"raw_name":"周涛","candidate_names":["周涛"],"student_no":"","confidence":0.91}'

    monkeypatch.setattr(ocr_service, "call_vision", fake_call_vision)

    result = asyncio.run(
        ocr_service.recognize_student_info_with_roster(str(source_path), ["周涛", "吴静"])
    )

    assert result == {
        "name": "周涛",
        "student_no": "",
        "confidence": 0.91,
        "candidate_names": ["周涛"],
        "match_status": "unique_high_confidence",
        "name_written": True,
    }
    assert "试卷顶部填写区" in captured["prompt"]
    assert "左侧装订信息栏" in captured["prompt"]
    assert "顺时针旋转后的" in captured["prompt"]
    assert "candidate_names 只能从名单中选择" in captured["prompt"]
    assert "candidate_names" in captured["prompt"]
    assert "raw_name" in captured["prompt"]
    assert captured["max_tokens"] == 512
    assert not Path(captured["path"]).exists()


def _make_plain_paper(tmp_path):
    source_path = tmp_path / "plain-paper.png"
    source = Image.new("RGB", (100, 200), "white")
    try:
        source.save(source_path)
    finally:
        source.close()
    return source_path


def test_roster_identity_retry_defers_multiple_candidates_to_teacher(tmp_path, monkeypatch):
    source_path = _make_plain_paper(tmp_path)

    async def fake_call_vision(*_args, **_kwargs):
        return '{"raw_name":"周涛","candidate_names":["周涛","吴静"],"student_no":"","confidence":0.98}'

    monkeypatch.setattr(ocr_service, "call_vision", fake_call_vision)

    result = asyncio.run(
        ocr_service.recognize_student_info_with_roster(str(source_path), ["周涛", "吴静"])
    )

    assert result["name"] == ""
    assert result["candidate_names"] == ["周涛", "吴静"]
    assert result["match_status"] == "ambiguous"


def test_roster_identity_retry_defers_low_confidence_unique_candidate_to_teacher(tmp_path, monkeypatch):
    source_path = _make_plain_paper(tmp_path)

    async def fake_call_vision(*_args, **_kwargs):
        return '{"raw_name":"周涛","candidate_names":["周涛"],"student_no":"","confidence":0.89}'

    monkeypatch.setattr(ocr_service, "call_vision", fake_call_vision)

    result = asyncio.run(
        ocr_service.recognize_student_info_with_roster(str(source_path), ["周涛", "吴静"])
    )

    assert result["name"] == ""
    assert result["candidate_names"] == ["周涛"]
    assert result["match_status"] == "low_confidence"


def test_roster_identity_retry_rejects_name_outside_current_roster(tmp_path, monkeypatch):
    source_path = _make_plain_paper(tmp_path)

    async def fake_call_vision(*_args, **_kwargs):
        return '{"raw_name":"陈强","candidate_names":[],"student_no":"","confidence":0.99}'

    monkeypatch.setattr(ocr_service, "call_vision", fake_call_vision)

    result = asyncio.run(
        ocr_service.recognize_student_info_with_roster(str(source_path), ["周涛", "吴静"])
    )

    assert result["name"] == ""
    assert result["candidate_names"] == []
    assert result["match_status"] == "no_match"
    # A written but unknown name must never look like a blank page.
    assert result["name_written"] is True


@pytest.mark.parametrize("raw, written", [
    ('{"raw_name":"","candidate_names":[],"student_no":"","confidence":0.0}', False),
    ('{"raw_name":"","candidate_names":["周涛"],"student_no":"","confidence":0.4}', True),
])
def test_roster_identity_reports_whether_any_name_was_written(tmp_path, monkeypatch, raw, written):
    source_path = _make_plain_paper(tmp_path)

    async def fake_call_vision(*_args, **_kwargs):
        return raw

    monkeypatch.setattr(ocr_service, "call_vision", fake_call_vision)

    result = asyncio.run(
        ocr_service.recognize_student_info_with_roster(str(source_path), ["周涛", "吴静"])
    )

    assert result["match_status"] == "no_match"
    assert result["name_written"] is written


def test_roster_identity_retry_rejects_forced_roster_substitution(tmp_path, monkeypatch):
    source_path = _make_plain_paper(tmp_path)

    async def fake_call_vision(*_args, **_kwargs):
        return '{"raw_name":"陈强","candidate_names":["赵磊"],"student_no":"","confidence":0.99}'

    monkeypatch.setattr(ocr_service, "call_vision", fake_call_vision)

    result = asyncio.run(
        ocr_service.recognize_student_info_with_roster(str(source_path), ["赵磊", "周涛"])
    )

    assert result["name"] == ""
    assert result["candidate_names"] == []
    assert result["match_status"] == "no_match"


def test_identity_crop_does_not_block_the_event_loop(tmp_path, monkeypatch):
    source_path = _make_plain_paper(tmp_path)
    crop_path = tmp_path / "identity-crop.jpg"

    def slow_crop(_image_path):
        time.sleep(0.15)
        crop_path.write_bytes(b"crop")
        return str(crop_path)

    async def fake_call_vision(*_args, **_kwargs):
        return '{"raw_name":"","candidate_names":[],"student_no":"","confidence":0}'

    monkeypatch.setattr(ocr_service, "_build_identity_crop", slow_crop)
    monkeypatch.setattr(ocr_service, "call_vision", fake_call_vision)

    async def run():
        started = time.perf_counter()
        task = asyncio.create_task(
            ocr_service.recognize_student_info_with_roster(
                str(source_path), ["周涛"]
            )
        )
        await asyncio.sleep(0.02)
        elapsed = time.perf_counter() - started
        result = await task
        return elapsed, result

    elapsed, result = asyncio.run(run())

    assert elapsed < 0.10
    assert result["match_status"] == "no_match"
    assert not crop_path.exists()


def test_identity_crop_also_uses_the_shared_image_gate(tmp_path, monkeypatch):
    source_path = _make_plain_paper(tmp_path)
    crop_path = tmp_path / "identity-shared-gate.jpg"
    crop_started = False
    gate_holder = {}

    def tracked_crop(_image_path):
        nonlocal crop_started
        crop_started = True
        crop_path.write_bytes(b"crop")
        return str(crop_path)

    async def fake_call_vision(*_args, **_kwargs):
        return '{"raw_name":"","candidate_names":[],"student_no":"","confidence":0}'

    monkeypatch.setattr(ocr_service, "_build_identity_crop", tracked_crop)
    monkeypatch.setattr(ocr_service, "call_vision", fake_call_vision)
    monkeypatch.setattr(
        ocr_service_template_route,
        "_get_local_ocr_preprocess_semaphore",
        lambda: gate_holder["gate"],
    )
    monkeypatch.setattr(
        ocr_service_local_image,
        "_get_local_ocr_preprocess_semaphore",
        lambda: gate_holder["gate"],
    )
    monkeypatch.setattr(
        ocr_service,
        "_get_local_ocr_preprocess_semaphore",
        lambda: gate_holder["gate"],
    )

    async def run():
        gate = asyncio.Semaphore(1)
        gate_holder["gate"] = gate
        await gate.acquire()
        task = asyncio.create_task(
            ocr_service.recognize_student_info_with_roster(
                str(source_path),
                ["周涛"],
            )
        )
        await asyncio.sleep(0.03)
        assert crop_started is False
        gate.release()
        return await task

    result = asyncio.run(run())

    assert crop_started is True
    assert result["match_status"] == "no_match"
    assert not crop_path.exists()
