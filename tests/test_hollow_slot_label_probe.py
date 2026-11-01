"""Offline evidence/prompt checks for the experimental hollow-label probe."""
import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from scripts.run_hollow_slot_label_probe import (
    DIRECTION, OLD_STYLE_TEXT, STYLES, directional_prompt, hollow_image,
)
from scripts.run_translucent_slot_label_probe import ProbeStop


@pytest.mark.parametrize("style", STYLES)
def test_directional_rule_preserves_contract_and_overflow(style):
    contract = '\n逐槽转录合同={"1":["1[1]","1[2]"]}'
    prompt = directional_prompt("start" + OLD_STYLE_TEXT + contract, style)
    assert contract in prompt
    assert prompt.endswith(DIRECTION)
    assert "不是硬裁切边界" in prompt and "unreadable" in prompt
    if style == "solid65_right":
        assert OLD_STYLE_TEXT in prompt
    else:
        assert OLD_STYLE_TEXT not in prompt
        assert "透明底" in prompt or "没有底色" in prompt


def test_prompt_drift_fails_before_a_call():
    with pytest.raises(ProbeStop):
        directional_prompt("a different production prompt", "digits65_right")


@pytest.mark.parametrize("style", ["outline65_right", "digits65_right"])
def test_hollow_background_leaves_ink_exact_and_local(style):
    source = Image.new("RGB", (160, 100), "white")
    draw = ImageDraw.Draw(source)
    draw.line((30, 40, 105, 40), fill=(50, 50, 50), width=2)
    existing = source.copy()
    ImageDraw.Draw(existing).rectangle((60, 15, 99, 76), fill=(20, 84, 178))
    placements = [{"placement_mode": "attached_stacked_slot", "label_bbox": [60, 15, 99, 76],
                   "label_opacity": .65, "label_lines": ["1", ".2"], "slot_id": "1[2]"}]
    output, stats = hollow_image(source, existing, placements, style, ImageFont.load_default(size=24), (20, 84, 178))
    a, b = np.array(source), np.array(output)
    assert output.size == source.size
    assert np.array_equal(a[:, :60], b[:, :60])
    assert np.array_equal(a[:, 100:], b[:, 100:])
    # Background beside the narrow digits must not get a pale blue fill.
    assert np.array_equal(a[40:42, 62:65], b[40:42, 62:65])
    assert stats[0]["transparent_pixels_unchanged"]
    assert stats[0]["painted_pixels"] < stats[0]["rectangle_pixels"] / 2


def test_solid_control_does_not_change_pixels():
    original = Image.new("RGB", (12, 10), (15, 34, 170))
    output, stats = hollow_image(Image.new("RGB", original.size), original, [], "solid65_right", None, None)
    assert output.tobytes() == original.tobytes()
    assert not stats


def test_clear_control_hash_callback_remains_nonrecursive(monkeypatch):
    from scripts import run_hollow_slot_label_clear_control as clear
    from scripts import run_hollow_slot_label_probe as hollow
    monkeypatch.setattr(hollow, "source_hashes", clear.source_hashes)
    recorded = clear.source_hashes()
    assert "scripts/run_hollow_slot_label_clear_control.py" in recorded
    assert "services/template_slots.py" in recorded
