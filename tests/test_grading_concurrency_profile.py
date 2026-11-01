"""Grading is provider-I/O bound; only live models' lanes may shape it.

Judging runs on qwen3.8-flash alone. Retired MiMo/DeepSeek windows and the
merged Hermes/vision knobs must not reappear in a resource profile, and the
production drop-in must carry the measured Flash lane (2026-09-11 probe:
320 concurrent requests clean -> 256)."""
from pathlib import Path

from config import _RESOURCE_PROFILES

_RETIRED_OR_MERGED = {
    "MIMO_GRADING_BATCH_LOCAL_CONCURRENT",
    "DEEPSEEK_BATCH_LOCAL_CONCURRENT",
    "HERMES_LOCAL_MAX_CONCURRENT",
    "MIMO_VISION_LOCAL_MAX_CONCURRENT",
    "MIMO_VISION_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES",
}


def test_profiles_carry_no_retired_or_merged_lanes():
    for name, profile in _RESOURCE_PROFILES.items():
        assert not _RETIRED_OR_MERGED & set(profile), name


def test_versioned_production_dropin_sets_measured_flash_lane():
    dropin = Path(__file__).resolve().parents[1] / "deploy" / "mathagent.service.d" / "20-resource-profile.conf"
    text = dropin.read_text(encoding="utf-8")
    assert 'Environment="FLASH_LOCAL_MAX_CONCURRENT=256"' in text
    assert not any(name in text for name in _RETIRED_OR_MERGED)


def test_memory_profile_still_bounds_memory_work():
    low = _RESOURCE_PROFILES["low_memory"]
    # Both profiles allow the full network window; only bytes and CPU differ.
    assert int(low["OCR_LOCAL_MAX_CONCURRENT"]) == int(_RESOURCE_PROFILES["standard"]["OCR_LOCAL_MAX_CONCURRENT"]) == 256
    assert int(low["OCR_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES"]) < int(
        _RESOURCE_PROFILES["standard"]["OCR_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES"])


def test_flash_grading_queue_is_not_sized_by_mimo_knobs(monkeypatch):
    import config
    from services import flash_grading, task_queue

    # Retired MiMo windows (subscription batch default was 3) must not cap it.
    monkeypatch.setattr(config, "MIMO_GRADING_BATCH_LOCAL_CONCURRENT", 3, raising=False)
    monkeypatch.setattr(config, "MODEL_TEACHER_BURST_CONCURRENCY", 352)
    monkeypatch.setattr(flash_grading, "FLASH_LOCAL_MAX_CONCURRENT", 256)
    monkeypatch.setattr(task_queue, "_grading_queue", None)
    queue = task_queue.get_grading_queue()
    assert queue._semaphore._value == 256
    assert queue._per_teacher_limit == 256
