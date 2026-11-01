"""Live Qwen probe using synthetic full-page evidence, never student data.

fixtures --out DIR; run --out DIR; replay --out DIR
Six HTTP attempts maximum, one per fixture. Calls the production PaperCache
transcription path and archives request JSON and full response bytes, without
headers, endpoint URLs or credentials. Controls are experiment-only images.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BUDGET = 6
SOURCES = ["scripts/run_translucent_slot_label_probe.py", "services/evidence_label_layout.py",
           "services/template_slots.py", "services/template_evidence.py", "services/paper_cache.py",
           "services/ocr_service.py", "services/analyzer.py", "services/teaching_context.py"]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def source_hashes():
    return {name: sha((ROOT / name).read_bytes()) for name in SOURCES}


def definitions():
    # Ground truth is used only to draw/assess the synthetic images, never in prompts.
    return [
        {"id": "sequential", "rows": [
            {"parent": "1", "ids": [1, 2, 3], "text": ["7.5", "-1.2", "3/4"], "gray": [35, 35, 35]},
            {"parent": "2", "ids": [1, 2, 3], "text": ["-0.6", "5/8", ""], "gray": [150, 35, 35]},
        ]},
        {"id": "shuffled_hierarchy", "rows": [
            {"parent": "12.3", "ids": [10, 2, 7], "text": ["-8.25", "6/7", "31"], "gray": [35, 35, 35]},
            {"parent": "8", "ids": [3, 1, 2], "text": ["12.04", "", "0.9"], "gray": [35, 35, 150]},
        ]},
    ]


def make_fixtures(out):
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    from services.evidence_label_layout import render_labels, source_patch_pnginfo
    from services.template_evidence import _evidence_question_label_font, EVIDENCE_Q_LABEL_COLOR

    out.mkdir(parents=True, exist_ok=False)
    font_file = Path("C:/Windows/Fonts/segoepr.ttf")
    font = ImageFont.truetype(str(font_file), 40)
    label_font = _evidence_question_label_font()
    blank = Image.new("RGB", (1080, 1000), "white")
    scratch = ImageDraw.Draw(blank)
    fixtures = []
    for sample in definitions():
        targets, boxes, cases, questions = [], [], [], []
        for row_i, row in enumerate(sample["rows"]):
            y = 160 + row_i * 260
            for col, (index, content, gray) in enumerate(zip(row["ids"], row["text"], row["gray"])):
                left = 92 + col * 300
                box = [left, y, left + 294, y + 90]
                slot = f"{row['parent']}[{index}]"
                boxes.append(box)
                targets.append({"question_number": row["parent"], "slot_id": slot,
                                "region_index": col, "region_px": box, "anchor_px": [left, y + 45]})
                cases.append({"parent": row["parent"], "slot_id": slot, "text": content,
                              "state": "visible" if content else "blank", "box": box, "gray": gray,
                              "stress_tail": col < 2 and bool(content)})
            # Deliberately canonical rather than physical order in the shuffled
            # stress fixture: content-to-ID binding must come from visible labels.
            ids = [f"{row['parent']}[{i}]" for i in sorted(row["ids"])]
            questions.append({"question_no": row["parent"], "question_type": "fill",
                              "question_text": "", "slot_ids": ids, "region_kinds": ["answer"] * len(ids),
                              "blank_count": len(ids), "answer_slots": [""] * len(ids)})
        box = [150, 720, 710, 810]
        boxes.append(box)
        targets.append({"question_number": "20", "slot_id": "20[1]", "region_index": 0,
                        "region_px": box, "anchor_px": [150, 765]})
        cases.append({"parent": "20", "slot_id": "20[1]", "text": "x+3=5\nx=2",
                      "state": "visible", "box": box, "gray": 35, "stress_tail": False})
        questions.append({"question_no": "20", "question_type": "solve", "question_text": "",
                          "slot_ids": ["20[1]"], "region_kinds": ["answer"],
                          "blank_count": 1, "answer_slots": [""]})
        from services.grading_contract import AnswerSpec
        for question in questions:
            AnswerSpec.model_validate(question)
        with patch("services.evidence_label_layout.SLOT_OVERLAP_OPACITY", 1.0):
            blank_labeled, _, _ = render_labels(np.array(blank), targets, boxes, font=label_font,
                                                color=EVIDENCE_Q_LABEL_COLOR, padding_x=8, padding_y=8, gap=12)
        for variant in ("clear", "alpha65", "opaque"):
            stem = f"{sample['id']}-{variant}"
            source = blank.copy()
            draw = ImageDraw.Draw(source)
            for case in cases:
                left, top, _, _ = case["box"]
                gray, content = case["gray"], case["text"]
                if case["parent"] == "20":
                    draw.text((180, 748), "x + 3 = 5", font=font, fill=(gray,) * 3, anchor="lt")
                    # Deliberate overflow below the locator box, not cropped.
                    draw.text((220, 850), "x = 2", font=font, fill=(gray,) * 3, anchor="lt")
                    continue
                width = scratch.textbbox((0, 0), content, font=font)[2]
                x = left + 30
                if variant != "clear" and case["stress_tail"]:
                    x = left + 300 - 3 - width
                draw.text((x, top + 25), content, font=font, fill=(gray,) * 3, anchor="lt")
            pixels = np.array(source)
            canvas, placements, mapping = render_labels(pixels, targets, boxes, font=label_font,
                color=EVIDENCE_Q_LABEL_COLOR, padding_x=8, padding_y=8, gap=12)
            metadata = source_patch_pnginfo(pixels, placements, mapping)
            if variant == "opaque":
                # Same exact stress ink and label geometry; replace only the
                # four translucent label rectangles with the blank label raster.
                # This is a control, NOT a production constant/config change.
                for p in placements:
                    if p["label_opacity"] < 1:
                        l, t, r, b = p["label_bbox"]
                        rect = (l, t, r + 1, b + 1)
                        canvas.paste(blank_labeled.crop(rect), (l, t))
                metadata = None
            source.save(out / f"{stem}-source.png")
            canvas.save(out / f"{stem}.png", pnginfo=metadata)
            review = canvas.copy()
            reviewer = ImageDraw.Draw(review)
            dx, dy = mapping["source_offset_px"]
            for l, t, r, b in boxes:
                reviewer.rectangle((l+dx, t+dy, r-1+dx, b-1+dy), outline=(180, 185, 193))
            review.save(out / f"{stem}-review-only.png")
            fixtures.append({"id": stem, "sample": sample["id"], "variant": variant,
                             "image": f"{stem}.png", "image_sha256": sha((out/f"{stem}.png").read_bytes()),
                             "source_sha256": sha(source.tobytes()), "cases": cases, "questions": questions,
                             "placements": placements, "mapping": mapping,
                             "control_override": "opaque rectangles" if variant == "opaque" else None})
    assert len(fixtures) == BUDGET
    for sample in definitions():
        a = next(f for f in fixtures if f["id"] == sample["id"] + "-alpha65")
        b = next(f for f in fixtures if f["id"] == sample["id"] + "-opaque")
        assert a["source_sha256"] == b["source_sha256"]
        assert a["mapping"]["blended_foreground_pixels"] > 0
    save(out / "fixtures.json", fixtures)
    save(out / "setup.json", {"synthetic_only": True, "max_http_attempts": BUDGET,
        "font_sha256": sha(font_file.read_bytes()), "sources": source_hashes(),
        "note": "Clear moves ink inside the same boxes; alpha65/opaque share exact source pixels."})
    (out / "harness.py").write_bytes(Path(__file__).read_bytes())


class ProbeStop(BaseException):
    """Hard budget/model boundary, not swallowed by service retry handlers."""


class WireArchive:
    def __init__(self, out):
        self.out, self.calls, self.fixture = out, [], None

    def install(self):
        import httpx
        original = httpx.AsyncClient.send

        async def send(client, request, **kwargs):
            payload = json.loads(request.content)
            if payload.get("model") != "qwen3.7-plus":
                raise ProbeStop("configured model does not match authorized probe")
            if len(self.calls) >= BUDGET or self.fixture is None:
                raise ProbeStop("budget exceeded or no fixture")
            if any(c["fixture"] == self.fixture["id"] for c in self.calls):
                raise ProbeStop("one attempt per fixture; no hidden retries")
            contents = [c for m in payload["messages"] for c in m.get("content", []) if isinstance(c, dict)]
            images = [base64.b64decode(c["image_url"]["url"].split(",", 1)[1])
                      for c in contents if c.get("type") == "image_url"]
            if len(images) != 1 or sha(images[0]) != self.fixture["image_sha256"]:
                raise ProbeStop("wire image is not the frozen synthetic full page")
            number = len(self.calls) + 1
            stem = f"wire/{number:02d}"
            receipt = {"attempt": number, "fixture": self.fixture["id"], "model": payload["model"],
                       "request": stem + "-request.json", "response": stem + "-response.bin",
                       "image_sha256": sha(images[0])}
            self.calls.append(receipt)
            save(self.out / receipt["request"], payload)
            save(self.out / "calls.json", self.calls)
            started = time.monotonic()
            try:
                response = await original(client, request, **kwargs)
                body = await response.aread()
                (self.out / receipt["response"]).write_bytes(body)
                receipt.update(status=response.status_code, response_sha256=sha(body))
                return response
            except Exception as exc:
                receipt["error_type"] = type(exc).__name__
                raise
            finally:
                receipt["elapsed_seconds"] = round(time.monotonic() - started, 3)
                save(self.out / "calls.json", self.calls)
        return patch.object(httpx.AsyncClient, "send", send)


def canonical(text):
    # Formatting only: never compute/correct an answer or use fuzzy similarity.
    text = text.replace("−", "-").replace("﹣", "-")
    text = re.sub(r"\\(?:dfrac|tfrac|frac)\s*\{([^{}]+)\}\s*\{([^{}]+)\}", r"\1/\2", text)
    text = text.replace("\\(", "").replace("\\)", "").replace("\\[", "").replace("\\]", "")
    return re.sub(r"\s+|\$|\\,|\\;|\\!", "", text)


def assess(fixtures, results):
    rows = []
    for f in fixtures:
        result = results[f["id"]]
        actual = {s["slot_id"]: s for q in result.get("questions", []) for s in q.get("student_slots", [])}
        expected_ids = {c["slot_id"] for c in f["cases"]}
        exact_ids = set(actual) == expected_ids and not any(q.get("slot_identity_error") for q in result.get("questions", []))
        for case in f["cases"]:
            observed = actual.get(case["slot_id"], {})
            rows.append({"fixture": f["id"], "slot_id": case["slot_id"], "expected": case["text"],
                "actual": observed.get("student_answer"), "expected_state": case["state"],
                "actual_state": observed.get("answer_state"), "id_coverage": exact_ids,
                "content_and_binding_correct": exact_ids and observed.get("answer_state") == case["state"]
                    and canonical(observed.get("student_answer", "")) == canonical(case["text"])})
    return rows


async def run(out, replay=False):
    import httpx
    from sqlalchemy.engine import Engine
    from services import ocr_service
    from services.paper_cache import PaperCache
    from services.analyzer import parse_llm_json
    from services.teaching_context import use_teaching_context, make_teaching_context

    fixtures = read(out / "fixtures.json")
    if not replay and (out / "calls.json").exists():
        raise ProbeStop("existing live attempt ledger; never rerun silently")
    setup = read(out / "setup.json")
    if setup["sources"] != source_hashes():
        raise ProbeStop("source changed since fixture preparation")
    if any(sha((out/f["image"]).read_bytes()) != f["image_sha256"] for f in fixtures):
        raise ProbeStop("fixture changed")
    wire = WireArchive(out)
    results = {}
    original_call = ocr_service.call_vision_images

    async def vision(paths, prompt, **kwargs):
        f = wire.fixture
        if kwargs.get("max_attempts") != 1:
            raise ProbeStop("production evidence retry contract changed")
        if replay:
            stored = read(out / f"{f['id']}-service.json")
            if stored["prompt"] != prompt or stored["kwargs"] != kwargs:
                raise ProbeStop("replay prompt/arguments changed")
            receipt = next(c for c in read(out / "calls.json") if c["fixture"] == f["id"])
            body = (out / receipt["response"]).read_bytes()
            if sha(body) != receipt["response_sha256"]:
                raise ProbeStop("raw response changed")
            data = ocr_service._parse_streamed_vision_response(httpx.Response(200, content=body))
            if data["choices"][0]["message"]["content"] != stored["raw"]:
                raise ProbeStop("service content does not match archived wire response")
            return stored["raw"]
        save(out / f"{f['id']}-prompt.json", {"prompt": prompt, "kwargs": kwargs})
        raw = await original_call(paths, prompt, **kwargs)
        save(out / f"{f['id']}-service.json", {"prompt": prompt, "kwargs": kwargs, "raw": raw,
                                               "parsed_raw": parse_llm_json(raw)})
        return raw

    async def no_network(*args, **kwargs):
        raise ProbeStop("offline replay forbids network")

    guard = patch.object(httpx.AsyncClient, "send", no_network) if replay else wire.install()
    with patch.object(Engine, "connect", side_effect=RuntimeError("probe disallows database access")), \
            patch.object(ocr_service, "call_vision_images", vision), guard, \
            use_teaching_context(make_teaching_context("primary")):
        # Counterbalance clear/stress order across the two synthetic samples.
        order = fixtures[:3] + list(reversed(fixtures[3:]))
        for f in order:
            wire.fixture = f
            print(f"{f['id']}: {'replay' if replay else 'live'}", flush=True)
            cache = PaperCache.from_confirmed_template_pages([{"page_index": 0, "questions": f["questions"]}])
            result = await cache._recognize_answers_only(str(out/f["image"]), "page_1", evidence_mode=True,
                evidence_paths=[str(out/f["image"])], evidence_strategy="full_column_difference")
            results[f["id"]] = result
            if replay:
                if result != read(out/f"{f['id']}-result.json"):
                    raise ProbeStop("replay normalized result changed")
            else:
                save(out/f"{f['id']}-result.json", result)
            if result.get("error"):
                raise ProbeStop("production service returned an error; inspect archived evidence")
            print(f"{f['id']}: returned {len(result.get('questions', []))} questions", flush=True)
    rows = assess(fixtures, results)
    totals = {f["id"]: {"correct": sum(r["content_and_binding_correct"] for r in rows if r["fixture"] == f["id"]),
                          "total": len(f["cases"])} for f in fixtures}
    if replay:
        if rows != read(out / "comparison.json"):
            raise ProbeStop("replay comparison changed")
        save(out / "replay.json", {"identical": True, "network_calls": 0, "rows": len(rows), "totals": totals})
    else:
        save(out / "comparison.json", rows)
        save(out / "run.json", {"status": "complete", "http_attempts": len(wire.calls), "totals": totals,
                                "source_hashes_after": source_hashes(), "synthetic_only": True})
        save(out / "manifest.json", {"files": {str(p.relative_to(out)).replace("\\", "/"): sha(p.read_bytes())
             for p in sorted(out.rglob("*")) if p.is_file() and p.name != "manifest.json"}})
    print(json.dumps({"replay": replay, "totals": totals, "http_attempts": len(wire.calls)}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["fixtures", "run", "replay"])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    if args.mode != "run":
        os.environ["PYTHON_DOTENV_DISABLED"] = "1"
        os.environ["JWT_SECRET"] = "synthetic-offline-probe-no-auth-at-least-32-characters"
    try:
        if args.mode == "fixtures":
            make_fixtures(args.out.resolve())
            print("6 synthetic fixtures created; no model calls")
        else:
            asyncio.run(run(args.out.resolve(), replay=args.mode == "replay"))
        return 0
    except (Exception, ProbeStop) as exc:
        # Provider exceptions may contain secrets/URLs. Only the class is public.
        print(f"probe failed: {type(exc).__name__}", flush=True)
        if args.out.exists():
            save(args.out / f"{args.mode}-failure.json", {"error_type": type(exc).__name__})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
