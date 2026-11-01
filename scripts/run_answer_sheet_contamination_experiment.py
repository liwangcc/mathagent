"""Synthetic A/B experiment, outside production routes.

Live:   python scripts/run_answer_sheet_contamination_experiment.py run --out outputs/contamination-20260928
Replay: python scripts/run_answer_sheet_contamination_experiment.py replay --out outputs/contamination-20260928

Imports the application's configuration normally only in live mode. Never reads
credential files/values itself. Archives full HTTP response bodies and request
JSON (no headers/URLs), all synthetic images, service results and source hashes.
The HTTP budget includes retries. Replay uses archived service replies, never
fake replies presented as live results. No database connections are permitted.
"""
from __future__ import annotations

import argparse
import asyncio
import contextvars
import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BUDGET = 20
PHASE = contextvars.ContextVar("contamination_phase", default="setup")
SOURCE_FILES = [
    "scripts/run_answer_sheet_contamination_experiment.py",
    "services/answer_sheet_column_transcription.py", "services/answer_sheet_batch_recognition.py",
    "services/answer_sheet_bubble_recognizer.py", "services/answer_sheet_layout_contract.py",
    "services/template_anchor_crops.py", "services/grader.py", "services/scoring_engine.py",
    "services/grading_contract.py", "services/analyzer.py", "services/flash_grading.py",
    "services/numeric_answers.py", "services/quick_grader.py", "services/task_queue.py",
    "services/ocr_service.py", "services/teaching_context.py", "services/math_curriculum.py",
]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded(value))


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


class BudgetExceeded(BaseException):
    """Do not let production retry handlers swallow an experiment hard stop."""


class WireArchive:
    def __init__(self, out, *, allow_network=True):
        self.out, self.calls, self.allow_network = out, [], allow_network

    def install(self):
        import httpx
        original = httpx.AsyncClient.send

        async def send(client, request, **kwargs):
            if not self.allow_network:
                raise BudgetExceeded("offline replay forbids network")
            payload = json.loads(request.content)
            if not isinstance(payload, dict) or "model" not in payload or "messages" not in payload:
                raise BudgetExceeded("non-model HTTP request is outside this experiment")
            if len(self.calls) >= BUDGET:
                raise BudgetExceeded("model HTTP attempt budget exhausted")
            number = len(self.calls) + 1
            stem = f"wire/{number:02d}"
            receipt = {"attempt": number, "phase": PHASE.get(), "model": payload["model"],
                       "request": stem + "-request.json", "response": stem + "-response.bin"}
            self.calls.append(receipt)
            save(self.out / receipt["request"], payload)
            save(self.out / "calls.json", self.calls)
            started = time.monotonic()
            try:
                response = await original(client, request, **kwargs)
                # Both streaming and ordinary clients can consume cached content.
                body = await response.aread()
                (self.out / receipt["response"]).write_bytes(body)
                receipt.update(status=response.status_code, response_sha256=digest(body))
                return response
            except Exception as exc:
                receipt.update(error_type=type(exc).__name__)
                raise
            finally:
                receipt["elapsed_seconds"] = round(time.monotonic() - started, 3)
                save(self.out / "calls.json", self.calls)

        return patch.object(httpx.AsyncClient, "send", send)


def sample_definitions():
    """The only source of handwriting truth; never sent to either model."""
    return [
        {"id": "short", "cases": [
            {"no": "1", "kind": "fill", "case": "correct_equivalent", "text": "0.5",
             "question": "计算 1/4 + 1/4", "answer": "1/2", "expected": "correct"},
            {"no": "2", "kind": "fill", "case": "wrong_fraction", "text": "3/4",
             "question": "计算 1/4 + 1/4", "answer": "1/2", "expected": "wrong"},
        ]},
        {"id": "blank_bubbles", "cases": [
            {"no": "1", "kind": "solve", "case": "blank_text", "text": "",
             "question": "解方程 2x+1=5，写出过程", "answer": "2x=4，x=2", "expected": "wrong"},
            *[{"no": str(i), "kind": "choice_mark", "case": case, "text": marked,
               "question": "单选：哪个数是偶数？A.1 B.3 C.4 D.5", "answer": "C", "expected": expected}
              for i, case, marked, expected in [(2, "multiple_marks", "CD", "wrong"),
                   (3, "blank_bubbles", "", "wrong"), (4, "correct_bubbles", "C", "correct"),
                   (5, "wrong_bubbles", "A", "wrong")]],
        ]},
        {"id": "long", "cases": [
            {"no": "1", "kind": "solve", "case": "long_correct", "expected": "correct",
             "question": "计算 24+9/5，写出过程，以带分数表示结果", "answer": "25又4/5",
             "text": "24 + 9/5\n= 120/5 + 9/5\n= 129/5\n= 25 + 4/5\n= 25 4/5"},
            {"no": "2", "kind": "solve", "case": "long_wrong_propagated", "expected": "wrong",
             "question": "计算 24+9/5，写出过程，以带分数表示结果", "answer": "25又4/5",
             "text": "24 + 9/5\n= 120/5 + 9/5\n= 127/5\n= 25 + 2/5\n= 25 2/5"},
        ]},
    ]


def make_samples(out):
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    from services.template_anchor_crops import build_anchor_crop_plan
    from services.answer_sheet_column_transcription import build_page_column_plan
    from services.answer_sheet_layout_contract import resolve_bubble_grid_pixels
    from services.grading_contract import AnswerSpec, finalize_answer_spec_for_category

    font_path = Path("C:/Windows/Fonts/segoepr.ttf")
    if not font_path.is_file():
        raise RuntimeError("fixture font unavailable; use archived images for exact replay")
    font = ImageFont.truetype(str(font_path), 32)
    printed = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 24)
    samples = sample_definitions()
    for sample in samples:
        image = Image.new("RGB", (1000, 1200), "white")
        draw = ImageDraw.Draw(image)
        draw.text((28, 10), "SYNTHETIC FIXTURE - NO STUDENT DATA", font=printed, fill=(110, 110, 110))
        specs, regions, grids = [], [], []
        for index, case in enumerate(sample["cases"]):
            y = 70 + index * (440 if sample["id"] == "long" else 165)
            bottom = y + (405 if sample["id"] == "long" else 140)
            bbox = [100, round(y / 1.2), 930, round(bottom / 1.2)]
            no = case["no"]
            region = {"region_id": "r" + no, "page_id": "page_1", "question_no": no,
                      "subquestion_no": "", "bbox": bbox, "region_type": case["kind"],
                      "reading_order": index + 1, "required": True, "teacher_confirmed": True,
                      "choice_options": list("ABCD"), "allow_multiple": False}
            regions.append(region)
            draw.text((35, y + 15), no + ".", font=printed, fill=(70, 70, 70))
            draw.rectangle((100, y, 930, bottom), outline=(190, 190, 190), width=1)
            if case["kind"] == "choice_mark":
                cells = []
                for cell_index, option in enumerate("ABCD"):
                    x = 150 + cell_index * 170
                    top = round((y + 44) / 1.2)
                    box = [x, top, x + 60, top + 45]
                    cells.append({"option": option, "bbox": box, "cell_size": [60, 45]})
                    px = [x, round(top * 1.2), x + 60, round((top + 45) * 1.2)]
                    draw.rectangle(px, outline=(100, 100, 100), width=2)
                    draw.text((x + 10, y + 7), option, font=printed, fill=(70, 70, 70))
                    if option in case["text"]:
                        draw.rectangle((px[0] + 5, px[1] + 5, px[2] - 6, px[3] - 6), fill=(30, 30, 30))
                grids.append({"question_no": no, "subquestion_no": "", "page_id": "page_1",
                              "region_id": "r" + no, "allow_multiple": False,
                              "grid_order": "left_to_right", "grid_bbox": [cells[0]["bbox"][0], cells[0]["bbox"][1],
                                  cells[-1]["bbox"][2], cells[-1]["bbox"][3]],
                              "cells": cells, "teacher_confirmed": True})
            else:
                draw.multiline_text((130, y + 18), case["text"], font=font, fill=(20, 40, 120), spacing=13)
            spec = AnswerSpec.model_validate({"question_no": no, "question_text": case["question"],
                "question_type": "choice" if case["kind"] == "choice_mark" else case["kind"],
                "choice_mode": "single" if case["kind"] == "choice_mark" else "unknown",
                "standard_answer": case["answer"], "matched": True, "max_score": 5,
                "subquestion_scores": [5] if case["kind"] == "solve" else []})
            specs.append(finalize_answer_spec_for_category(spec, "exam"))
        image_path = out / "fixtures" / (sample["id"] + ".png")
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(image_path)
        artifact_sha = digest(image_path.read_bytes())
        artifact = {"width": 1000, "height": 1200, "sha256": artifact_sha,
                    "question_crop_plan": build_anchor_crop_plan({"column_layout": {
                        "column_count": 1, "columns": [{"column_index": 1, "x1_1000": 0, "x2_1000": 1000}]}},
                        artifact_sha256=artifact_sha)}
        rows = resolve_bubble_grid_pixels(grids, {"page_1": (1000, 1200)}) if grids else {}
        plan = build_page_column_plan(page_id="page_1", artifact=artifact, answer_regions=regions, bubble_rows=rows)
        sample.update(image=image_path.relative_to(out).as_posix(), plan=plan, bubble_rows=rows,
                      answer_map={spec.question_no: spec.to_answer_map_entry() for spec in specs},
                      specs=[spec.model_dump() for spec in specs])
        assert np.asarray(image).shape == (1200, 1000, 3)
    save(out / "samples.json", samples)
    save(out / "fixture-font.json", {"name": font_path.name, "sha256": digest(font_path.read_bytes())})
    return samples


def combined_prompt(primary, sample):
    # B differs only by the explicit second task and answer/rubric exposure.
    # Keep all production exclusions, layout and verbatim transcription rules.
    contracts = [{"entry_id": entry["entry_id"], "question_no": entry["question_no"],
                  "contract": sample["answer_map"][entry["question_no"]]}
                 for entry in sample["plan"]["entries"]]
    return primary + (
        "\n本次为合并转录与批阅对照实验：在同一次响应中完成两项任务。先按上文原样转录，"
        "再根据下面的教师答案合同判断转录作答。不得依据标准答案改写、补全学生笔迹。"
        "本次覆盖上文的只转录/输出字段要求：仅返回JSON对象，含entries和judgments两个字段。"
        "entries内容与上文完全相同。judgments按entry_id逐条返回"
        '{"entry_id":"...","result":"correct|partial|wrong|needs_review","explanation":"..."}。'
        "\n教师答案合同=" + json.dumps(contracts, ensure_ascii=False)
    )


def extract_combined(raw, request):
    from services.answer_sheet_column_transcription import _load_json_object, parse_transcription_response
    value = _load_json_object(raw)
    if set(value) != {"entries", "judgments"} or not isinstance(value["judgments"], list):
        raise ValueError("combined_response_shape_invalid")
    if [row.get("entry_id") for row in value["judgments"]] != request["entry_ids"]:
        raise ValueError("combined_judgment_identity_invalid")
    for row in value["judgments"]:
        if (set(row) != {"entry_id", "result", "explanation"}
                or row["result"] not in {"correct", "partial", "wrong", "needs_review"}
                or not isinstance(row["explanation"], str)):
            raise ValueError("combined_judgment_invalid")
    text = json.dumps({"entries": value["entries"]}, ensure_ascii=False)
    parse_transcription_response(text, request)
    return text, value["judgments"]


def request_identity(request):
    """JSON object key order has no semantic meaning; all other bytes do.

    samples.json is canonical JSON. Loading it can reorder an embedded rubric
    compared with the live Python construction. Verify the complete original
    request, allowing ONLY this key-order difference in the appended rubric.
    """
    value = copy.deepcopy(request)
    prefix, separator, contract = value["prompt"].partition("\n教师答案合同=")
    if separator:
        value["prompt"] = prefix + separator + json.dumps(json.loads(contract), ensure_ascii=False, sort_keys=True)
    return value


async def run_sample(sample, arm, out, *, replay=False):
    import numpy as np
    from PIL import Image
    from services import grader
    from services.answer_sheet_batch_recognition import batch_page_answers, vision_images_call
    from services.answer_sheet_bubble_recognizer import recognize_bubble_page
    from services.answer_sheet_column_transcription import (
        TranscriptionEvidence, build_transcription_request, transcribe_page_columns,
    )

    name = f"{sample['id']}-{arm}"
    folder = out / "runs" / name
    folder.mkdir(parents=True, exist_ok=True)
    image = np.asarray(Image.open(out / sample["image"]).convert("RGB"))
    plan = sample["plan"]
    primary = build_transcription_request(plan)
    call = vision_images_call("synthetic-contamination-experiment")
    call_index = 0
    combined = None

    async def vision(paths, prompt, *, max_tokens):
        nonlocal call_index, combined
        call_index += 1
        is_primary = prompt == primary["prompt"]
        actual_prompt = combined_prompt(prompt, sample) if arm == "B" and is_primary else prompt
        request = {"prompt": actual_prompt, "max_tokens": max_tokens,
                   "image_sha256": [digest(Path(path).read_bytes()) for path in paths]}
        req_file, raw_file = folder / f"vision-{call_index}-request.json", folder / f"vision-{call_index}-raw.txt"
        if replay:
            if request_identity(read(req_file)) != request_identity(request):
                raise RuntimeError("replay vision request mismatch")
            raw = raw_file.read_text(encoding="utf-8")
        else:
            save(req_file, request)
            PHASE.set(name + ("-primary" if is_primary else "-blind-verification"))
            raw = await call(paths, actual_prompt, max_tokens=max_tokens)
            raw_file.write_text(raw, encoding="utf-8")
        if arm == "B" and is_primary:
            text, combined = extract_combined(raw, primary)
            return text
        return raw

    evidence_root = out / ("replay-work" if replay else "runs") / name
    evidence = TranscriptionEvidence(evidence_root, "synthetic", "page_1", None if replay else "ab-v1")
    record = await transcribe_page_columns(image, plan, evidence, vision)
    if record["status"] == "failed":
        raise RuntimeError("transcription_failed: " + str(record.get("failure", {}).get("reason_code")))
    bubbles = {"status": "not_required", "questions": {}}
    if sample["bubble_rows"]:
        bubbles = {"status": "recognized", "questions": recognize_bubble_page(image,
            list(sample["bubble_rows"].values()), {case["no"]: list(case["answer"])
                for case in sample["cases"] if case["kind"] == "choice_mark"})}
    answers, reasons = batch_page_answers(plan, record, bubbles)
    # Offline score-impact simulation uses raw first readings, NOT blind-read
    # corrections. Production teacher holds are preserved alongside the scores.
    fields = {field["question_no"]: field for field in record["fields"]}
    questions = []
    for case in sample["cases"]:
        no = case["no"]
        if case["kind"] == "choice_mark":
            text = "".join(bubbles["questions"][no]["marked_options"])
            state = "visible" if text else "blank"
        else:
            text, state = fields[no]["student_answer"], fields[no]["state"]
        questions.append({"question_no": no, "question_type": sample["answer_map"][no]["question_type"],
                          "student_answer": text, "answer_state": state})
    grade_input = {"paper": {"questions": questions, "ocr_answers_only": True,
                            "ocr_alignment_verified": True, "ocr_requires_separate_grading": True},
                   "answer_map": sample["answer_map"], "student_name": "synthetic-" + sample["id"],
                   "teacher_id": 0, "category": "exam"}
    if replay and read(folder / "grading-input.json") != grade_input:
        raise RuntimeError("replay grading input mismatch")
    if not replay:
        save(folder / "grading-input.json", grade_input)
    PHASE.set(name + "-production-grading")
    result = await grader.grade_paper_unified(**grade_input)
    stable_record = {key: record[key] for key in ("status", "fields", "failure")}
    output = {"transcription": stable_record, "bubbles": bubbles, "answers": answers,
              "review_reasons": reasons, "combined_judgments": combined, "grading": result}
    if replay:
        if output != read(folder / "result.json"):
            raise RuntimeError("replay result differs")
    else:
        save(folder / "result.json", output)
    return output


def compare(samples, results):
    rows = []
    for sample in samples:
        first, second = [results[f"{sample['id']}-{arm}"] for arm in ("A", "B")]
        for case in sample["cases"]:
            no = case["no"]
            row = {"sample": sample["id"], "question_no": no, "case": case["case"],
                   "truth": case["text"], "expected": case["expected"]}
            for arm, result in (("A", first), ("B", second)):
                answer = next(item for item in result["answers"] if item["question_no"] == no)
                detail = next(item for item in result["grading"]["details"] if item["question_no"] == no)
                row[arm] = {"text": answer["answer"], "gate": answer["status"], "reason": answer["reason"],
                            "result": detail["result"], "score": detail["awarded_score"],
                            "explanation": detail.get("explanation", "")}
            row["text_differs"] = row["A"]["text"] != row["B"]["text"]
            row["grade_differs"] = (row["A"]["result"], row["A"]["score"]) != (row["B"]["result"], row["B"]["score"])
            row["gate_differs"] = row["A"]["gate"] != row["B"]["gate"]
            rows.append(row)
    return rows


def manifest(out, status):
    entries = []
    for path in sorted(out.rglob("*")):
        if path.is_file() and path.name not in {"manifest.json", "replay.json"} and "replay-work" not in path.parts:
            data = path.read_bytes()
            entries.append({"path": path.relative_to(out).as_posix(), "bytes": len(data), "sha256": digest(data)})
    save(out / "manifest.json", {"version": 1, "status": status, "model_attempt_budget": BUDGET,
                                "synthetic_only": True, "files": entries})


def verify_manifest(out):
    saved = read(out / "manifest.json")
    for entry in saved["files"]:
        path = (out / entry["path"]).resolve()
        if not path.is_relative_to(out.resolve()) or digest(path.read_bytes()) != entry["sha256"]:
            raise ValueError("evidence hash mismatch")
    for source, sha in read(out / "sources.json").items():
        path = ROOT / source
        if source == SOURCE_FILES[0] and (out / "harness-live.py").is_file():
            # Preserve the exact live harness when only offline analysis or
            # replay plumbing is repaired later. Production sources must match.
            path = out / "harness-live.py"
        if digest(path.read_bytes()) != sha:
            raise ValueError("source hash mismatch: " + source)


def verify_wire_derivations(out):
    """Reparse complete transport bodies with production parsers, then check
    the service replies used by replay really came from those wire records.
    """
    import httpx
    from services.ocr_service import _parse_streamed_vision_response
    from services.flash_grading import parse_flash_response
    from services.analyzer import _validate_paper_judgment

    calls = read(out / "calls.json")
    if len(calls) > BUDGET or [call["attempt"] for call in calls] != list(range(1, len(calls) + 1)):
        raise ValueError("invalid attempt ledger")
    content_by_phase = {}
    for call in calls:
        if call.get("status") != 200:
            continue
        response = httpx.Response(200, content=(out / call["response"]).read_bytes())
        if call["phase"].endswith("-production-grading"):
            if call["model"] == "qwen3.8-flash":
                content = parse_flash_response(response)["content"]
            else:
                content = _parse_streamed_vision_response(response)["choices"][0]["message"]["content"]
        else:
            content = _parse_streamed_vision_response(response)["choices"][0]["message"]["content"]
        content_by_phase.setdefault(call["phase"], []).append(content)
    for sample in read(out / "samples.json"):
        for arm in ("A", "B"):
            name = f"{sample['id']}-{arm}"
            folder = out / "runs" / name
            for raw in folder.glob("vision-*-raw.txt"):
                index = int(raw.name.split("-")[1])
                phase = name + ("-primary" if index == 1 else "-blind-verification")
                if raw.read_text(encoding="utf-8") not in content_by_phase.get(phase, []):
                    raise ValueError("vision service output does not match raw transport")
    for saved in read(out / "grading-judgments.json").values():
        qnos = [question["question_no"] for question in saved["questions"]]
        expected = {key: {field: value for field, value in row.items() if field != "grading_receipt"}
                    for key, row in saved["response"].items()}
        if not any(_validate_paper_judgment(content, qnos) == expected
                   for content in content_by_phase.get(saved["phase"], [])):
            raise ValueError("grading service output does not match raw transport")


async def experiment(out, *, replay=False):
    from services import grader
    from services.teaching_context import make_teaching_context, use_teaching_context
    from sqlalchemy.engine import Engine
    if replay:
        verify_manifest(out)
        verify_wire_derivations(out)
        samples = read(out / "samples.json")
    else:
        out.mkdir(parents=True, exist_ok=False)
        samples = make_samples(out)
        save(out / "sources.json", {path: digest((ROOT / path).read_bytes()) for path in SOURCE_FILES})
    judgments_path = out / "grading-judgments.json"
    judgments = read(judgments_path) if replay else {}
    original_judge = grader.judge_paper

    async def judge(questions, detail_level="detailed"):
        key = digest(encoded({"questions": questions, "detail_level": detail_level}))
        if replay:
            entry = judgments[key]
            return copy.deepcopy(entry["response"])
        response = await original_judge(questions, detail_level=detail_level)
        if getattr(response, "__task_queue_cacheable__", True) is False:
            raise RuntimeError("live grading did not return a usable result")
        judgments[key] = {"questions": questions, "detail_level": detail_level,
                          "response": copy.deepcopy(response), "phase": PHASE.get()}
        save(judgments_path, judgments)
        return response

    wire = WireArchive(out, allow_network=not replay)
    status, results = "failed", {}
    try:
        # No database is needed for these pure services; an accidental future
        # dependency fails closed rather than touching a teacher/student store.
        with patch.object(Engine, "connect", side_effect=RuntimeError("experiment disallows databases")), \
                patch.object(grader, "judge_paper", judge), wire.install(), \
                use_teaching_context(make_teaching_context("primary")):
            for index, sample in enumerate(samples):
                # Counterbalance order without adding unbudgeted repetitions.
                for arm in (("A", "B") if index % 2 == 0 else ("B", "A")):
                    print(f"{sample['id']} {arm}: {'replay' if replay else 'live'}", flush=True)
                    results[f"{sample['id']}-{arm}"] = await run_sample(sample, arm, out, replay=replay)
        comparison = compare(samples, results)
        if replay:
            if comparison != read(out / "comparison.json"):
                raise RuntimeError("replay comparison differs")
            save(out / "replay.json", {"status": "identical", "model_calls": 0, "cases": len(comparison),
                 "replay_harness_sha256": digest(Path(__file__).read_bytes()),
                 "wire_derivations_verified": True})
        else:
            save(out / "comparison.json", comparison)
            save(out / "run.json", {"status": "complete", "samples": len(samples), "cases": len(comparison),
                 "model_calls": len(wire.calls), "grading_provider": grader.get_grading_provider(),
                 "teaching_context": make_teaching_context("primary"),
                 "note": "Scores are offline raw-transcription simulations; teacher holds remain holds."})
        status = "complete"
        print(f"complete; {len(comparison)} cases; {len(wire.calls)} model HTTP attempts", flush=True)
    except (Exception, BudgetExceeded) as exc:
        # Exception messages from model adapters can contain URLs or secrets.
        if not replay:
            save(out / "run.json", {"status": "failed", "error_type": type(exc).__name__,
                 "phase": PHASE.get(), "completed_arms": list(results), "model_calls": len(wire.calls)})
        print(f"experiment failed: {type(exc).__name__}; phase={PHASE.get()}", flush=True)
        if replay:
            raise
        return 1
    finally:
        if not replay:
            manifest(out, status)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["run", "replay", "fixtures"])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    if args.mode != "run":
        # Documented dotenv switch: offline replay never loads credentials.
        os.environ["PYTHON_DOTENV_DISABLED"] = "1"
        os.environ["JWT_SECRET"] = "synthetic-offline-replay-no-auth-at-least-32-characters"
    if args.mode == "fixtures":
        args.out.mkdir(parents=True, exist_ok=False)
        make_samples(args.out)
        print("synthetic fixtures created; no model calls")
        return 0
    return asyncio.run(experiment(args.out.resolve(), replay=args.mode == "replay"))


if __name__ == "__main__":
    raise SystemExit(main())
