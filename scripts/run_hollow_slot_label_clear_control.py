"""Two unoccluded digits-only controls for the hollow-label follow-up."""
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
from scripts import run_hollow_slot_label_probe as hollow
from scripts import run_translucent_slot_label_probe as base
_hollow_source_hashes = hollow.source_hashes


def source_hashes():
    return {**_hollow_source_hashes(), "scripts/run_hollow_slot_label_clear_control.py": base.sha(Path(__file__).read_bytes())}


def fixtures(out, previous):
    from PIL import Image
    from services.template_evidence import _evidence_question_label_font, EVIDENCE_Q_LABEL_COLOR
    manifest = base.read(previous/"manifest.json")["files"]
    out.mkdir(parents=True, exist_ok=False)
    result, used = [], {}
    for original in base.read(previous/"fixtures.json"):
        if original["variant"] != "clear":
            continue
        paths = [original["image"], original["id"]+"-source.png"]
        for name in paths:
            if base.sha((previous/name).read_bytes()) != manifest[name]:
                raise base.ProbeStop("prior clear fixture changed")
            used[name] = manifest[name]
        if original["mapping"]["source_offset_px"] != [0, 0]:
            raise base.ProbeStop("expected zero-offset synthetic control")
        source = Image.open(previous/paths[1]).convert("RGB")
        prior = Image.open(previous/paths[0]).convert("RGB")
        item = copy.deepcopy(original)
        item["id"] = original["sample"]+"-clear-digits65_right"
        item["variant"] = "digits65_right"
        item["image"] = item["id"]+".png"
        canvas, stats = hollow.hollow_image(source, prior, item["placements"], item["variant"],
            _evidence_question_label_font(), EVIDENCE_Q_LABEL_COLOR)
        assert sum(s["modified_source_ink_pixels"] for s in stats) == 0
        canvas.save(out/item["image"])
        source.save(out/f"{item['id']}-source.png")
        item["image_sha256"] = base.sha((out/item["image"]).read_bytes())
        item["paint_stats"] = stats
        item["mapping"]["version"] = "experiment-only-hollow-labels-v1"
        item["control_override"] = "unoccluded digits-only; directional prompt"
        result.append(item)
    if len(result) != 2:
        raise base.ProbeStop("expected exactly two clear controls")
    base.save(out/"fixtures.json", result)
    base.save(out/"setup.json", {"sources": source_hashes(), "prior_sources": used,
        "max_http_attempts": 2, "synthetic_only": True})
    (out/"harness.py").write_bytes(Path(__file__).read_bytes())


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
            fixtures(args.out.resolve(), args.previous.resolve())
            print("2 clear controls prepared; 0 model calls")
        else:
            with patch.object(hollow, "source_hashes", source_hashes), patch.object(base, "BUDGET", 2):
                asyncio.run(hollow.run(args.out.resolve(), args.mode == "replay"))
        return 0
    except (Exception, base.ProbeStop) as exc:
        print(f"probe failed: {type(exc).__name__}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
