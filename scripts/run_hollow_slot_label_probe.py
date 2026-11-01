"""Six-call synthetic follow-up: directional ownership vs hollow/digit labels.

The renderer/prompt variants exist only in this experiment. Reuses the existing
wire archive, production PaperCache OCR path and verdict logic without changing
production files or the previous experiment. No keys, uploads or databases.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import logging
import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import run_translucent_slot_label_probe as base

STYLES = ("solid65_right", "outline65_right", "digits65_right")
OLD_STYLE_TEXT = (
    "其他槽位可仍显示Q槽ID。标签紧贴自己作答框左侧，压到前方作答框时以65%不透明度显示；"
)
DIRECTION = (
    "\n横排槽位定位规则：先定位蓝色题号，再从该题号右侧沿水平方向寻找同一行作答。"
    "蓝色题号贴在自己作答区左边，不是答案末尾标记。"
    "题号左侧的已有笔迹，不可因离这个题号较近、被它覆盖或从它透出，就分给这个题号；"
    "先判断是否为左邻槽作答的延续，不把左邻答案挪到右侧空槽。"
    "保持同一串笔迹的数字、正负号、小数点和分数完整，不拼接相邻槽答案。"
    "这是定位起点和阅读方向，不是硬裁切边界；连贯笔迹的少量越界和下方续行仍完整保留。"
    "确实无法确定归属时返回unreadable，不强行猜测。"
)


def source_hashes():
    paths = [*base.SOURCES, "scripts/run_hollow_slot_label_probe.py", "services/grading_contract.py"]
    return {name: base.sha((ROOT/name).read_bytes()) for name in paths}


def directional_prompt(prompt, style):
    if style not in STYLES or prompt.count(OLD_STYLE_TEXT) != 1:
        raise base.ProbeStop("unexpected prompt or experiment style")
    if style != "solid65_right":
        description = "透明底、细蓝色边线和蓝色竖排数字" if style == "outline65_right" else "只有蓝色竖排数字，没有底色或边框"
        prompt = prompt.replace(OLD_STYLE_TEXT,
            "其他槽位可仍显示Q槽ID。横排槽位的程序题号标记是" + description + "，不是学生笔迹；"
            "数字上下排列表示编号层级，不是分数。标签仍紧贴自己作答框左侧，"
            "压到前方作答框时仅数字和细线以65%不透明度显示，空白底部完全透明；")
    return prompt + DIRECTION


def hollow_image(source, existing, placements, style, font, color):
    """Change label paint only; keep source coordinates, full page and font size."""
    import numpy as np
    from PIL import Image, ImageDraw
    if style not in STYLES:
        raise ValueError("unknown style")
    result = existing.convert("RGB").copy()
    stats = []
    if style == "solid65_right":
        return result, stats
    for p in placements:
        if p["placement_mode"] != "attached_stacked_slot":
            continue
        l, t, r, b = p["label_bbox"]
        rect = (l, t, r+1, b+1)
        original = source.crop(rect).convert("RGBA")
        overlay = Image.new("RGBA", original.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        alpha = round(255 * p["label_opacity"])
        paint = (*color, alpha)
        if style == "outline65_right":
            draw.rectangle((0, 0, overlay.width-1, overlay.height-1), outline=paint, width=1)
        boxes = [draw.textbbox((0, 0), line, font=font) for line in p["label_lines"]]
        height = max(tb[3]-tb[1] for tb in boxes)
        for i, (line, tb) in enumerate(zip(p["label_lines"], boxes)):
            draw.text(((overlay.width-(tb[2]-tb[0]))/2-tb[0], 3+i*(height+2)-tb[1]),
                      line, font=font, fill=paint)
        combined = Image.alpha_composite(original, overlay).convert("RGB")
        before, after = np.array(original.convert("RGB")), np.array(combined)
        overlay_mask = np.array(overlay)[:, :, 3] != 0
        # This assertion is the main distinction from a translucent filled box.
        assert np.array_equal(before[~overlay_mask], after[~overlay_mask])
        ink = np.any(before != 255, axis=2)
        stats.append({"slot_id": p["slot_id"], "label_bbox": list(rect), "opacity": p["label_opacity"],
                      "rectangle_pixels": int(overlay_mask.size), "painted_pixels": int(overlay_mask.sum()),
                      "source_ink_pixels": int(ink.sum()),
                      "modified_source_ink_pixels": int((ink & np.any(before != after, axis=2)).sum()),
                      "transparent_pixels_unchanged": True})
        result.paste(combined, (l, t))
    return result, stats


def make_fixtures(out, previous):
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    from services.template_evidence import _evidence_question_label_font, EVIDENCE_Q_LABEL_COLOR
    prior = base.read(previous/"fixtures.json")
    old_manifest = base.read(previous/"manifest.json")["files"]
    out.mkdir(parents=True, exist_ok=False)
    fixtures, used_prior, visuals = [], {}, []
    font = _evidence_question_label_font()
    for f in [p for p in prior if p["variant"] == "alpha65"]:
        if f["mapping"]["source_offset_px"] != [0, 0]:
            raise base.ProbeStop("this follow-up requires the existing zero-offset synthetic fixtures")
        paths = [f["image"], f["id"]+"-source.png"]
        for name in paths:
            contents = (previous/name).read_bytes()
            if base.sha(contents) != old_manifest[name]:
                raise base.ProbeStop("prior synthetic fixture hash differs")
            used_prior[name] = base.sha(contents)
        source = Image.open(previous/paths[1]).convert("RGB")
        existing = Image.open(previous/paths[0]).convert("RGB")
        for style in STYLES:
            item = copy.deepcopy(f)
            item["id"] = f["sample"] + "-" + style
            item["variant"] = style
            item["image"] = item["id"] + ".png"
            item["source_image"] = item["id"] + "-source.png"
            item["control_override"] = "experiment-only label paint and directional prompt"
            canvas, stats = hollow_image(source, existing, f["placements"], style, font, EVIDENCE_Q_LABEL_COLOR)
            canvas.save(out/item["image"])
            source.save(out/item["source_image"])
            item["image_sha256"] = base.sha((out/item["image"]).read_bytes())
            item["source_sha256"] = base.sha(source.tobytes())
            item["paint_stats"] = stats
            arr, raw = np.array(canvas), np.array(source)
            item["actual_modified_source_ink_pixels"] = int((np.any(arr != raw, axis=2) & np.any(raw != 255, axis=2)).sum())
            assert item["source_sha256"] == f["source_sha256"]
            if style == "solid65_right":
                assert np.array_equal(arr, np.array(existing))
            else:
                item["mapping"]["version"] = "experiment-only-hollow-labels-v1"
                item["mapping"].pop("blended_foreground_pixels", None)
                item["mapping"].pop("source_patch_sha256", None)
            review = canvas.copy()
            draw = ImageDraw.Draw(review)
            for c in item["cases"]:
                l, t, r, b = c["box"]
                draw.rectangle((l, t, r-1, b-1), outline=(180, 185, 193), width=1)
            review.save(out/f"{item['id']}-review-only.png")
            if f["sample"] == "sequential":
                visuals.append((style, review))
            fixtures.append(item)
    if len(fixtures) != base.BUDGET:
        raise base.ProbeStop("fixture count must equal six")
    # Human-only overview. The model still receives each complete 1080x1000 page.
    preview = Image.new("RGB", (1130, 675), "white")
    draw = ImageDraw.Draw(preview)
    title_font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 23)
    title = {"solid65_right": "原实心蓝底 · 65%", "outline65_right": "透明底 · 细蓝边线", "digits65_right": "透明底 · 仅蓝色数字"}
    for row, (style, image) in enumerate(visuals):
        y = row*220
        draw.text((20, y+8), title[style], font=title_font, fill=(25,35,50))
        preview.paste(image.crop((30, 135, 1020, 280)), (20, y+58))
    preview.save(out/"styles-review-only.png")
    base.save(out/"fixtures.json", fixtures)
    base.save(out/"setup.json", {"sources": source_hashes(), "synthetic_only": True,
        "max_http_attempts": base.BUDGET, "prior_sources": used_prior,
        "note": "Same exact source ink, label locations/font size; prompt/right-side rule frozen per style."})
    (out/"harness.py").write_bytes(Path(__file__).read_bytes())
    (out/"base-harness.py").write_bytes((ROOT/"scripts/run_translucent_slot_label_probe.py").read_bytes())


async def run(out, replay):
    from services.paper_cache import PaperCache
    from services import template_slots
    original_recognize = PaperCache._recognize_answers_only
    original_build = template_slots.build_slot_evidence_prompt
    fixtures = {str((out/f["image"]).resolve()): f for f in base.read(out/"fixtures.json")}

    async def recognize(cache, image_path, *args, **kwargs):
        fixture = fixtures[str(Path(image_path).resolve())]
        def build(prompt, questions):
            return directional_prompt(original_build(prompt, questions), fixture["variant"])
        with patch.object(template_slots, "build_slot_evidence_prompt", build):
            return await original_recognize(cache, image_path, *args, **kwargs)

    with patch.object(base, "source_hashes", source_hashes), \
            patch.object(PaperCache, "_recognize_answers_only", recognize):
        await base.run(out, replay=replay)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["fixtures", "run", "replay"])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--previous", type=Path, default=ROOT/"outputs/qwen37-slot-labels-20260928-v2")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    if args.mode != "run":
        os.environ["PYTHON_DOTENV_DISABLED"] = "1"
        os.environ["JWT_SECRET"] = "synthetic-offline-probe-no-auth-at-least-32-characters"
    try:
        if args.mode == "fixtures":
            make_fixtures(args.out.resolve(), args.previous.resolve())
            print("6 same-ink synthetic fixtures prepared; 0 model calls")
        else:
            asyncio.run(run(args.out.resolve(), args.mode == "replay"))
        return 0
    except (Exception, base.ProbeStop) as exc:
        print(f"probe failed: {type(exc).__name__}", flush=True)
        if args.out.exists():
            base.save(args.out/f"{args.mode}-failure.json", {"error_type": type(exc).__name__})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
