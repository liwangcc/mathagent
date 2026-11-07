"""Hermetic template-editor harness: the real app with scripted model replies.

The browser drives the production frontend build against the production
FastAPI app. Only the model boundary (Flash/Max replies) is scripted, from a
seeded scenario generator that varies question type, slot count, answer vs
drawing positions, a model drawing box on the printed stem, empty answers and
Max extra/refused reports. Page correction, column layout, the pipeline, every save
and confirm validator and the diagnostics boundary are the real code.

Synthetic data only. ``tests.conftest`` isolation is imported first: no .env,
no credentials, temporary database/uploads, and no outbound network.

The desktop client's ``canonicalize_template_image`` command is served at
``/__harness/desktop/canonicalize`` by the real sidecar canonicalizer
(desktop/worker), so the browser can submit desktop canonical candidates.

Run from the repository root (after ``npm run build`` in frontend/):

    env -u PYTHONPATH ./venv_local/Scripts/python.exe -m tests.e2e.template_editor_harness --port 8765

then: node frontend/tests/template-editor-fuzz.browser.mjs http://127.0.0.1:8765
"""
from __future__ import annotations

import os

# The production app refuses to start without an explicit CORS origin.
os.environ.setdefault("CORS_ORIGINS", "http://127.0.0.1")

import tests.conftest  # noqa: F401,E402  (isolation must precede config/app import)

import argparse
import asyncio
import copy
import json
import random
import shutil
import struct
import sys
import threading
import uuid
from pathlib import Path

from fastapi import Request
from fastapi.responses import Response
from PIL import Image, ImageDraw

import app as app_module
import config
from routers import (
    grade_availability,
    grade_recognition,
    grade_report,
    grade_template_availability,
    grade_template_bank,
    grade_template_confirm,
    grade_template_cut,
    grade_template_import,
)
from services import template_directory_pipeline as pipeline
from services import template_one_three as one_three

PAGE_SIZE = (1240, 1754)
DESKTOP_WORKER = Path(__file__).resolve().parents[2] / "desktop" / "worker"
_lock = threading.Lock()
_current: dict = {}
# (template dir, page index) of every page imported from an accepted client
# canonical candidate. Identical synthetic pages can canonicalize to the same
# bytes on both sides, so only the acceptance call itself is evidence.
_client_canonical_pages: set[tuple[str, int]] = set()


# ---------------------------------------------------------------------------
# Scenario generation (all coordinates are full-page normalized_1000_v1)
# ---------------------------------------------------------------------------

def generate_scenario(seed: int, columns: int = 1) -> dict:
    rng = random.Random(seed)
    count = rng.randint(1, 4 * columns)
    top, bottom = 60, 960
    rows = -(-count // columns)
    band = (bottom - top) / rows
    spans = [(40, 960)] if columns == 1 else [(40, 480), (520, 960)]
    questions = []
    for index in range(1, count + 1):
        column, row_index = divmod(index - 1, rows)
        cx0, cx1 = spans[column]
        # Design coordinates span 40..960; squeeze them into this column.
        X = lambda x, cx0=cx0, cx1=cx1: round(cx0 + (x - 40) * (cx1 - cx0) / 920)
        y0 = round(top + row_index * band)
        y1 = round(top + (row_index + 1) * band) - 10
        number = str(index)
        roll = rng.random()
        if roll < 0.25:
            qtype, kinds = "choice", ["answer"]
        elif roll < 0.33:
            qtype, kinds = "solve", ["drawing"]
        else:
            qtype = "fill" if rng.random() < 0.45 else "solve"
            kinds = ["answer"] * rng.randint(1, 3 if qtype == "fill" else 4)
            if rng.random() < 0.4:
                kinds.insert(rng.randrange(len(kinds) + 1), "drawing")
        stem = [X(90), y0 + 5, X(900), min(y1, y0 + 55)]
        stem_drawing = "drawing" in kinds and rng.random() < 0.5
        slots, boxes, values = [], [], []
        text_index = 0
        for slot_index, kind in enumerate(kinds, 1):
            slot_id = f"{number}[{slot_index}]"
            if kind == "drawing":
                box = list(stem) if stem_drawing else [X(560), min(y1 - 30, y0 + 70), X(940), y1]
                if box[3] - box[1] < 25:
                    box[3] = box[1] + 25
                # Stay under the 25%-of-page box limit (an oversized model box
                # fails the whole import; tracked separately from editing).
                box[3] = min(box[3], box[1] + 200_000 // max(1, box[2] - box[0]))
                cue = "作图区：画出图形"
                value = ""
            elif qtype == "choice":
                box = [X(800), y0 + 10, X(880), min(y1, y0 + 50)]
                cue = "从A/B/C/D中选择"
                value = rng.choice("ABCD")
            else:
                row, col = divmod(text_index, 3)
                left = 100 + col * 150
                box = [X(left), min(y1 - 25, y0 + 70 + row * 70), X(left + 130), min(y1, y0 + 120 + row * 70)]
                text_index += 1
                cue = f"({slot_index})"
                value = "" if rng.random() < 0.15 else rng.choice(["12", "x=2", "3/4", "y=2x+1", "60°"])
            slots.append({"slot_id": slot_id, "cue": cue})
            boxes.append([round(v) for v in box])
            values.append(value)
        flag = None
        if rng.random() < 0.2:
            flag = {
                "status": rng.choice(["extra", "refused"]),
                "branch": rng.choice(["locator", "solver"]),
                "slot": rng.randrange(len(kinds)) if rng.random() < 0.85 else None,
            }
        questions.append({
            "number": number, "type": qtype, "kinds": kinds, "slots": slots, "boxes": boxes,
            "values": values, "band": [cx0, y0, cx1, y1], "stem": stem, "stem_drawing": stem_drawing,
            "number_bbox": [cx0, y0 + 10, X(80), min(y1, y0 + 45)], "flag": flag,
            "text": f"Q{number} synthetic: see figure.",
        })
    return {"seed": seed, "columns": columns, "questions": questions}


def oversize_box(spec: dict, kind: str) -> str | None:
    """Make one model box of ``kind`` cover more than a quarter of the page.

    Returns the slot id, or None when the scenario has no such box. The box
    stays inside its question's column so only its size is wrong.
    """
    for question in spec["questions"]:
        # A scripted locator report replaces its box with "no position".
        locator_flag = (question["flag"] or {}).get("branch") == "locator"
        if kind not in question["kinds"] or question["stem_drawing"] or locator_flag:
            continue
        slot = question["kinds"].index(kind)
        x0, y0, x1, _ = question["band"]
        width = x1 - x0 - 20
        height = 260_000 // width + 20
        top = max(10, min(y0, 990 - height))
        question["boxes"][slot] = [x0 + 10, top, x1 - 10, top + height]
        return question["slots"][slot]["slot_id"]
    return None


def render_scenario(spec: dict, path: Path) -> None:
    width, height = PAGE_SIZE
    image = Image.new("RGB", PAGE_SIZE, "white")
    draw = ImageDraw.Draw(image)
    px = lambda box: [box[0] * width / 1000, box[1] * height / 1000, box[2] * width / 1000, box[3] * height / 1000]
    draw.rectangle([8, 8, width - 8, height - 8], outline="black", width=3)
    # Printed body lines in every column (avoiding answer/drawing boxes) give
    # the page the two-sided ink density of a real paper, so the production
    # local column detector, not a model, decides the layout.
    spans = [(40, 960)] if spec.get("columns", 1) == 1 else [(40, 480), (520, 960)]
    occupied = [box for q in spec["questions"] for box in q["boxes"] + [q["number_bbox"]]]
    for x0, x1 in spans:
        for y in range(70, 950, 18):
            line = [x0 + 60, y, x1 - 20, y + 6]
            if any(line[0] < b[2] and b[0] < line[2] and line[1] < b[3] + 6 and b[1] - 6 < line[3] for b in occupied):
                continue
            draw.rectangle(px(line), fill=(60, 60, 60))
    for question in spec["questions"]:
        nx, ny, _, _ = px(question["number_bbox"])
        draw.text((nx, ny), f"{question['number']}.", fill="black")
        sx, sy, ex, ey = px(question["stem"])
        draw.text((sx, sy), question["text"], fill="black")
        if question["stem_drawing"]:
            # A printed figure inside the stem: the model mistakes it for a drawing slot.
            fx = sx + (ex - sx) * 0.6
            draw.polygon([(fx, ey - 4), (fx + 40, sy + 4), (fx + 80, ey - 4)], outline="black")
        for kind, box, value in zip(question["kinds"], question["boxes"], question["values"]):
            if kind == "drawing" and question["stem_drawing"]:
                continue
            rect = px(box)
            draw.rectangle(rect, outline=(120, 120, 120), width=2)
            if value:
                draw.text((rect[0] + 8, rect[1] + 8), value, fill=(30, 30, 160))
    image.save(path, format="PNG")


def _q(question: dict) -> str:
    return "Q" + question["number"]


def model_payloads(spec: dict) -> dict:
    questions = spec["questions"]
    hierarchy = {"contract_version": 4, "questions": [
        {"question_number": q["number"], "printed_path": ["一、综合", f"{q['number']}."], "slots": q["slots"]}
        for q in questions]}
    coarse = {"coordinate_space": "normalized_1000_v1", "questions": [
        {"question_number": q["number"], "bbox": q["band"]} for q in questions]}
    locator_rows, answers = [], []
    for q in questions:
        regions = [{"slot_id": slot["slot_id"], "kind": kind, "bbox": box}
                   for slot, kind, box in zip(q["slots"], q["kinds"], q["boxes"])]
        drawing = [box for kind, box in zip(q["kinds"], q["boxes"]) if kind == "drawing"]
        row = {"question_number": q["number"], "question_number_bbox": q["number_bbox"],
               "question_type": q["type"], "choice_mode": "single" if q["type"] == "choice" else "unknown",
               "regions": regions,
               "label_anchor": [max(5, drawing[0][0] - 15), (drawing[0][1] + drawing[0][3]) // 2] if drawing else None}
        items = [{"slot_id": slot["slot_id"], "answer": value} for slot, value in zip(q["slots"], q["values"])]
        answer = {"q_id": _q(q), "standard_answer": "; ".join(v for v in q["values"] if v) or "按作图要求",
                  "answer_items": items, "solution_steps": [], "visual_facts": [], "uncertain": []}
        flag = q["flag"]
        if flag:
            report = {"status": flag["status"], "reason": "合成测试：原图没有该作答位置"}
            if flag["branch"] == "locator":
                if flag["slot"] is None:
                    row = {"question_number": q["number"], **report}
                else:
                    regions[flag["slot"]] = {"slot_id": q["slots"][flag["slot"]]["slot_id"], **report}
            elif flag["slot"] is None:
                answer = {"q_id": _q(q), **report}
            else:
                items[flag["slot"]] = {"slot_id": q["slots"][flag["slot"]]["slot_id"], "answer": "", **report}
        locator_rows.append(row)
        answers.append(answer)
    return {
        "template-hierarchy-v1": hierarchy,
        "template-coarse-locator-v1": coarse,
        "template-directory-v1": {"coordinate_space": "normalized_1000_v1", "questions": locator_rows, "needs_review": []},
        one_three.SOLVE_PHASE: {"answers": answers},
        one_three.TRANSCRIBE_PHASE: {"questions": [
            {"q_id": _q(q), "question_text": q["text"], "uncertain": []} for q in questions]},
    }


# ---------------------------------------------------------------------------
# Model boundary and hermetic readiness
# ---------------------------------------------------------------------------

async def scripted_request(images, prompt, phase, validator, **kwargs):
    with _lock:
        payloads = copy.deepcopy(_current.get("payloads") or {})
    if phase not in payloads:
        raise ValueError(f"harness has no scripted reply for {phase}")
    return validator(one_three.parse_json(json.dumps(payloads[phase], ensure_ascii=False)))


async def _available(*_args, **_kwargs):
    return {"status": "available", "message": "合成测试服务可用"}


async def _hardware_ready(*_args, **_kwargs):
    return {"status": "ready", "message": "合成测试：无需加速节点"}


async def _no_classifier(_questions):
    raise RuntimeError("knowledge classification is disabled in the template editor harness")


_real_column_layout = grade_template_import.recognize_column_layout


async def scripted_column_layout(image_path, **kwargs):
    """Real local layout first; stand in for the qwen boundary reply only.

    When the local double-column gate falls through to the provider (which the
    isolated harness cannot reach), answer the way a correct model would: the
    printed gutter. Production validation and result building still run.
    """
    import services.template_column_layout as cut
    try:
        return await _real_column_layout(image_path, **kwargs)
    except cut.TemplateCutError:
        modes = [kwargs.get(key) for key in ("column_mode", "layout_mode") if kwargs.get(key) is not None]
        if not modes or cut.normalize_column_mode(modes[0]) != "double":
            raise
    source = cut._validate_template_image(image_path)
    payload = cut._payload_from_columns(
        [{"column_index": 1, "x1_1000": 0, "x2_1000": 500}, {"column_index": 2, "x1_1000": 500, "x2_1000": 1000}],
        source_key="harness_layout_model", trust="original")
    payload["column_layout"]["columns"] = cut.validate_layout_boundaries(
        payload["column_layout"]["columns"], column_mode="double", evidence=payload)
    return cut._build_layout_result(
        source, payload, provider=cut._PROVIDER,
        diagnostics={"strategy": "model_boundary_proposal", "gate_role": "diagnostics_only"},
        column_mode="double", layout_revision=kwargs.get("layout_revision"),
        previous_layout=kwargs.get("previous_layout"))


_real_accept_candidate = grade_template_import.accept_canonical_template_candidate


def recorded_accept_candidate(source_image_path, canonical_image_path, raw_metadata, template_dir, page_index, **kwargs):
    """The real client-candidate acceptance, noting which pages took that path."""
    result = _real_accept_candidate(source_image_path, canonical_image_path, raw_metadata, template_dir, page_index, **kwargs)
    with _lock:
        _client_canonical_pages.add((str(Path(template_dir).resolve()), page_index))
    return result


async def _infrastructure_ready(*_args, **_kwargs):
    # Redis-backed model limiters guard provider capacity, not template logic.
    return {"ready": True, "status": "available"}


def desktop_envelope(source: bytes, work_dir: Path) -> bytes:
    """What the desktop ``canonicalize_template_image`` command returns.

    Mirrors desktop/src-tauri/src/commands.rs: the same source sniffing, the
    real sidecar canonicalizer, and ``build_template_envelope`` (magic, u32 LE
    metadata length, metadata JSON, PNG). The Rust struct carries the matrix,
    quad and confidence as f64, so they are re-emitted as floats like serde.
    """
    if source.startswith(b"\x89PNG\r\n\x1a\n"):
        extension = "png"
    elif source.startswith(b"\xff\xd8\xff"):
        extension = "jpg"
    else:
        raise ValueError("template source must be JPEG or PNG")
    if str(DESKTOP_WORKER) not in sys.path:
        sys.path.insert(0, str(DESKTOP_WORKER))
    from mathagent_worker.canonicalization import canonicalize_template
    workspace = work_dir / f"desktop-{uuid.uuid4().hex}"
    workspace.mkdir(parents=True)
    try:
        source_path = workspace / f"source.{extension}"
        source_path.write_bytes(source)
        result = canonicalize_template(workspace, source_path, workspace / "canonical.png")
        metadata = dict(result.canonicalization)
        for key in ("source_to_canonical", "detected_page_quad"):
            metadata[key] = [[float(value) for value in row] for row in metadata[key]]
        metadata["confidence"] = float(metadata["confidence"])
        payload = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        png = Path(result.canonical_path).read_bytes()
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    return b"MATHCAN1" + struct.pack("<I", len(payload)) + payload + png


def redirect_logs(log_dir: Path) -> None:
    """Keep harness logs out of the developer's repo-level logs/ folder."""
    import services.logging_config as logging_config
    import services.template_output_evidence as output_evidence
    log_dir.mkdir(parents=True, exist_ok=True)
    logging_config.LOG_DIR = log_dir
    logging_config.setup_processing_logging.__defaults__ = (log_dir,)
    output_evidence.EVIDENCE_DIR = log_dir / "template-output-failures"


def install_patches() -> None:
    import services.system_readiness as system_readiness
    system_readiness.get_grading_infrastructure_readiness = _infrastructure_ready
    pipeline._request = scripted_request
    grade_template_cut.recognize_column_layout = scripted_column_layout
    grade_template_import.recognize_column_layout = scripted_column_layout
    grade_template_import.accept_canonical_template_candidate = recorded_accept_candidate
    grade_recognition._check_limits_or_raise = lambda *_args, **_kwargs: None
    grade_availability.batch_grade_availability = _available
    grade_availability._batch_grade_service_availability = _available
    grade_template_availability.get_column_layout_readiness = _available
    grade_availability.get_public_hardware_readiness = _hardware_ready
    grade_report._template_knowledge_classifier = _no_classifier
    # The template modules hold their own references to these shared helpers.
    grade_template_availability._batch_grade_service_availability = _available
    grade_template_availability.get_public_hardware_readiness = _hardware_ready
    grade_template_confirm._template_knowledge_classifier = _no_classifier
    grade_template_bank._template_knowledge_classifier = _no_classifier
    grade_template_import._template_knowledge_classifier = _no_classifier


# ---------------------------------------------------------------------------
# Test-only control endpoints (never part of the production app)
# ---------------------------------------------------------------------------

def _teacher() -> dict:
    from database import SessionLocal, init_db
    from models_user import User
    from services.auth_service import create_token
    init_db()
    with SessionLocal() as db:
        user = db.query(User).filter(User.phone == "19900000001").first()
        if user is None:
            user = User(phone="19900000001", name="合成测试教师", school="合成测试学校",
                        education_stage="senior", role="teacher", status="active")
            db.add(user)
            db.commit()
            db.refresh(user)
        return {"user_id": user.id, "token": create_token(user.id)}


def install_control_routes(work_dir: Path) -> None:
    app = app_module.app

    @app.post("/__harness/scenario")
    async def set_scenario(request: Request):
        body = await request.json()
        seed = int(body["seed"])
        spec = generate_scenario(seed, 2 if body.get("columns") == "double" else 1)
        if body.get("oversize") in ("answer", "drawing"):
            spec["oversized"] = oversize_box(spec, body["oversize"])
        path = work_dir / f"template-{seed}{'-oversize' if spec.get('oversized') else ''}.png"
        render_scenario(spec, path)
        with _lock:
            _current.clear()
            _current.update(spec=spec, payloads=model_payloads(spec))
        return {"seed": seed, "image_path": str(path.resolve()), "spec": spec}

    @app.get("/__harness/teacher")
    async def teacher():
        return _teacher()

    @app.post("/__harness/desktop/canonicalize")
    async def desktop_canonicalize(request: Request):
        source = await request.body()
        try:
            envelope = await asyncio.to_thread(desktop_envelope, source, work_dir)
        except ValueError as exc:  # TemplateCanonicalizationError included
            return Response(str(exc), status_code=422, media_type="text/plain")
        return Response(envelope, media_type="application/octet-stream")

    @app.get("/__harness/session/{template_id}")
    async def session_dump(template_id: str):
        from services.template_session import TemplateSession
        sess = TemplateSession.load_meta(template_id, config.UPLOAD_DIR)
        template_dir = str(Path(sess.template_dir).resolve())
        with _lock:
            client_pages = sorted(index for directory, index in _client_canonical_pages if directory == template_dir)
        return {"status": sess.status, "pages": sess.pages,
                # Page indexes imported from accepted desktop canonical candidates.
                "client_canonical_pages": client_pages,
                "artifacts": {label: {k: v for k, v in (art or {}).items()
                                      if k in ("question_schema", "template_answer_regions", "question_anchors")}
                              for label, art in (sess.page_artifacts or {}).items()}}

    # SPA catch-all routes are registered at import; move ours ahead of them.
    harness = [route for route in app.router.routes if getattr(route, "path", "").startswith("/__harness/")]
    app.router.routes[:] = harness + [route for route in app.router.routes if route not in harness]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    import config
    work_dir = Path(config.DATA_DIR) / "harness"
    work_dir.mkdir(parents=True, exist_ok=True)
    redirect_logs(Path(config.DATA_DIR) / "logs")
    install_patches()
    install_control_routes(work_dir)
    import uvicorn
    uvicorn.run(app_module.app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
