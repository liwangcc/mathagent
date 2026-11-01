"""Independent graphic checks over frozen, restored student evidence.

Fixed references use the pixel gate plus Flash. When the pixel gate cannot
measure a shape (the standard lies on ink already on the page, the template
page is missing, or registration is off), Flash still compares the overlay and
may pass it, but that path never awards an automatic zero.
Questions without a fixed reference use the saved drawing region and send the
bounded question text and student crop to Flash. The accepted probability is a
model report, not a measured pixel score.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image

from services.drawing_reference import validate_confirmed_reference, render_overlay, render_reference_mask
from services.grading_persistence import image_content_hash

POLICY = "drawing-pixel-model-v5"
ACCEPT_THRESHOLD = 90
PIXEL_THRESHOLD = 80
PIXEL_VERSION = "drawing-pixel-coarse-v1"
PROMPT = (
    "浅青蓝半透明线/点是标准答案，黑色深色笔迹是学生作答，网格、坐标轴和原有参照是底图。"
    "比较学生作图与标准是否一致，允许轻微手绘偏差；检查位置、方向、连接、长度、缺画和多余笔画，"
    "明显过长过短不能忽略。重合处黑色仍是学生笔迹，参考层不算作答。"
    "p_correct表示学生作图符合标准的概率(0到100)，不是对判错的确信度，也不是像素重合率。"
    "看不清返回needs_review及null。只返回JSON："
    '{"result":"correct或wrong或needs_review","p_correct":数字或null,"reason":"简短图像依据"}'
)
MANUAL_PROMPT = (
    "这是学生在题目指定作图区内的作答原图。题干如下：\n"
    "{question_text}\n"
    "图中包含印刷底图；规则实线/虚线分格、网格、坐标轴不算学生作答。涂色题要看到新增填涂，不能把印刷框线当成已涂色；无法区分底图与新增笔迹时返回needs_review及null。"
    "请根据题干判断学生作图是否满足要求，允许轻微手绘偏差；检查位置、方向、连接、"
    "长度、缺画和多余笔画。无法看清或题干不完整时返回needs_review及null。"
    "p_correct表示符合题干要求的概率(0到100)。只返回JSON："
    '{{"result":"correct或wrong或needs_review","p_correct":数字或null,"reason":"简短图像依据"}}'
)


# Without a pixel measurement the model must see the uncovered student strokes
# too: blue ink under the cyan layer is otherwise easy to misread.
FALLBACK_PREFIX = ("第一张是学生局部原图（无标准覆盖），第二张是同一区域叠加标准的对照图。"
                   "先在第一张图中确认学生新增的笔迹，再对照第二张判断；第二张的浅青蓝层只表示标准。")


def _reference_prompt(variant: dict) -> str:
    strokes = variant["strokes"]
    if all(stroke["kind"] == "point" for stroke in strokes):
        return "第一张是学生局部原图，第二张是同一区域叠加标准的对照图。" + PROMPT.replace(
            "比较学生作图与标准是否一致，允许轻微手绘偏差；检查位置、方向、连接、长度、缺画和多余笔画，"
            "明显过长过短不能忽略。",
            f"本方案标准仅有{len(strokes)}个点，不要求连线。核对这些点的位置是否有学生标记，允许轻微手绘偏差；"
            "位置标记可以是描点、小圈、叉号或学生画线的明确顶点，不强制要求涂成实心圆。"
            "先在第一张无参考覆盖的图中检查深浅铅笔痕迹，再对照第二张的位置；不能仅凭网格交点认定作答。"
            "原有点和辅助线不是新增作图要求。",
        )
    return PROMPT


def transcript_digest(answer: object) -> str:
    return hashlib.sha256(str(answer or "").encode("utf-8")).hexdigest()


def rubric_digest(question: dict) -> str:
    fields = {key: question.get(key) for key in ("standard_answer", "drawing_regions", "answer_kind")}
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def validate_verdict(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"result", "p_correct", "reason"}:
        raise ValueError("invalid drawing response fields")
    if value["result"] not in {"correct", "wrong", "needs_review"}:
        raise ValueError("invalid drawing verdict")
    if not isinstance(value["reason"], str) or not value["reason"].strip() or len(value["reason"]) > 2000:
        raise ValueError("drawing response requires bounded reason")
    probability = value["p_correct"]
    if probability is None:
        if value["result"] != "needs_review":
            raise ValueError("null probability requires review")
    elif (isinstance(probability, bool) or not isinstance(probability, (int, float))
          or not math.isfinite(probability) or not 0 <= probability <= 100):
        raise ValueError("invalid drawing probability")
    return value


def accepted(value: dict) -> bool:
    validate_verdict({key: value.get(key) for key in ("result", "p_correct", "reason")})
    return value["result"] == "correct" and value["p_correct"] >= ACCEPT_THRESHOLD


def _box(raw, width, height):
    if (not isinstance(raw, list) or len(raw) != 4
            or any(isinstance(v, bool) or not isinstance(v, (int, float))
                   or not math.isfinite(v) or not 0 <= v <= 1000 for v in raw)
            or raw[0] >= raw[2] or raw[1] >= raw[3]):
        raise ValueError("invalid drawing evidence region")
    return (int(math.floor(raw[0] * width / 1000)), int(math.floor(raw[1] * height / 1000)),
            min(width, int(math.ceil(raw[2] * width / 1000))),
            min(height, int(math.ceil(raw[3] * height / 1000))))


def _png_uri(rgb: np.ndarray) -> str:
    image = Image.fromarray(rgb)
    # Tiny graph crops lose faint pencil marks in a vision encoder. Integer
    # nearest-neighbour enlargement adds no inferred ink and preserves aspect.
    factor = min(4, max(1, math.ceil(960 / max(image.size))))
    if factor > 1:
        image = image.resize((image.width * factor, image.height * factor), Image.Resampling.NEAREST)
    image.thumbnail((1600, 1600))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    if buffer.tell() > 4_000_000:
        raise ValueError("drawing evidence exceeds image limit")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _manual_input(canvas: np.ndarray, question: dict) -> str:
    """Crop only the saved student drawing regions for manual/custom grading."""
    height, width = canvas.shape[:2]
    regions = question.get("drawing_regions") or []
    if not 1 <= len(regions) <= 64:
        raise ValueError("missing confirmed drawing regions")
    visible = np.zeros((height, width), dtype=bool)
    for region in regions:
        x1, y1, x2, y2 = _box(region, width, height)
        visible[y1:y2, x1:x2] = True
    yy, xx = np.where(visible)
    if not len(xx):
        raise ValueError("empty drawing regions")
    isolated = np.where(visible[:, :, None], canvas, 255).astype(np.uint8)
    return _png_uri(isolated[yy.min():yy.max() + 1, xx.min():xx.max() + 1])


async def assess_manual_input(canvas: np.ndarray, question: dict, *, teacher_id=None) -> dict:
    """Judge a teacher-selected/custom drawing without a fixed reference image."""
    from services.flash_grading import call_flash
    text = question.get("question_text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("manual drawing requires complete question text")
    image = _manual_input(canvas, question)
    prompt = MANUAL_PROMPT.format(question_text=text.strip())
    try:
        response = await call_flash(
            [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image}},
            ]}], teacher_id=teacher_id, phase="drawing", json_object=True,
            response_validator=lambda content: validate_verdict(json.loads(content)),
        )
        verdict = validate_verdict(json.loads(response["content"]))
        return {**verdict, "variant_index": 0, "model_called": True,
                "model": response["model"], "parameters": response["parameters"],
                "receipt": response.get("receipt", {})}
    except Exception:
        return {"variant_index": 0, "model_called": True, "result": "needs_review",
                "p_correct": None, "reason": "自定义作图核对未返回有效结果，请老师查看证据"}


def _manual_outcome(check: dict) -> tuple[str, str]:
    validate_verdict({key: check.get(key) for key in ("result", "p_correct", "reason")})
    if check["result"] == "wrong":
        return "wrong", check["reason"]
    if check["result"] == "correct" and check["p_correct"] >= ACCEPT_THRESHOLD:
        return "correct", check["reason"]
    return "needs_review", "自定义作图模型置信度未达到自动通过门槛，请老师复核"


def validate_reference_geometry(reference: dict, regions: list) -> None:
    """Check a fixed teacher reference against the saved full-page regions."""
    if reference["comparison_mode"] != "fixed":
        return
    if not regions:
        raise ValueError("标准图缺少已保存的作答区域")
    visible = np.zeros((1000, 1000), dtype=bool)
    for region in regions:
        x1, y1, x2, y2 = _box(region, 1000, 1000)
        visible[y1:y2, x1:x2] = True
    for region in reference["context_regions"]:
        x1, y1, x2, y2 = _box(region, 1000, 1000)
        if not visible[y1:y2, x1:x2].all():
            raise ValueError("标准图需要的参照不在已保留的作答区域内，请扩大作答区域或改走自定义作图判定")
    for index in range(len(reference["variants"])):
        mask = render_reference_mask(reference, 1000, 1000, index)
        if np.any((mask != 0) & ~visible):
            raise ValueError("标准图有笔画画在画图区外：请把笔画移回画图区内（可撤销后重画），或把画图区拉大到包住整个标准图")


def _source_rect(column: dict, image_size: tuple, width: int, height: int) -> tuple:
    """Validate an additive label rail without reinterpreting template geometry."""
    from services.evidence_label_layout import SLOT_LABEL_LAYOUT_VERSION
    mapping = column.get("label_layout")
    if mapping is None:
        if image_size != (width, height):
            raise ValueError("column image dimensions mismatch")
        return (0, 0, width, height)
    if (not isinstance(mapping, dict)
            or mapping.get("version") not in {"non-occluding-labels-v1", SLOT_LABEL_LAYOUT_VERSION}
            or mapping.get("source_size_px") != [width, height]):
        raise ValueError("invalid label source mapping")
    rect = mapping.get("source_rect_px")
    if (not isinstance(rect, list) or len(rect) != 4
            or any(type(v) is not int for v in rect)):
        raise ValueError("invalid label source rectangle")
    x1, y1, x2, y2 = rect
    if (not 0 <= x1 < x2 <= image_size[0] or not 0 <= y1 < y2 <= image_size[1]
            or [x2 - x1, y2 - y1] != [width, height]
            or mapping.get("source_offset_px") != [x1, y1]):
        raise ValueError("label source rectangle mismatch")
    return tuple(rect)


def reconstruct_canvas(paths: tuple[str, ...], diagnostics: dict, *, restore_source: bool = True) -> np.ndarray:
    """Only replay measured column endpoints; never guess equal column widths."""
    width, height = diagnostics.get("template_width"), diagnostics.get("template_height")
    if (type(width) is not int or type(height) is not int or min(width, height) < 1
            or width * height > 40_000_000):
        raise ValueError("missing verified template dimensions")
    if diagnostics.get("answer_source_complete") is not True:
        raise ValueError("incomplete student evidence")
    columns = diagnostics.get("evidence_columns") or []
    if len(columns) != len(paths) or not paths:
        raise ValueError("column evidence manifest mismatch")
    result = np.full((height, width, 3), 255, dtype=np.uint8)
    coverage = np.zeros(width, dtype=bool)
    for path, column in zip(paths, columns):
        left, right = column.get("crop_left_px"), column.get("crop_right_px")
        if len(paths) == 1 and left is None and right is None:
            left, right = 0, width
        if type(left) is not int or type(right) is not int or not 0 <= left < right <= width:
            raise ValueError("missing measured column endpoints")
        with Image.open(path) as image:
            rect = _source_rect(column, image.size, right - left, height)
            pixels = np.array(image.convert("RGB").crop(rect))
            if restore_source:
                from services.evidence_label_layout import restore_source_patches
                pixels = restore_source_patches(image, pixels, column.get("label_layout") or {})
        # Overlapping columns must agree. A conflicting replay is not evidence.
        overlap = coverage[left:right]
        if overlap.any() and not np.array_equal(result[:, left:right][:, overlap], pixels[:, overlap]):
            raise ValueError("conflicting overlapping columns")
        result[:, left:right] = pixels
        coverage[left:right] = True
    if not coverage.all():
        raise ValueError("column evidence does not cover full page")
    return result


def build_text_evidence(paths: tuple[str, ...], diagnostics: dict,
                        questions: list[dict], output_dir: str) -> tuple[str, ...]:
    """Whiten graphic regions only in the Plus copy; retain Flash source bytes."""
    graphic = [q for q in questions if q.get("answer_kind") in {"drawing", "mixed"}]
    if not graphic:
        return paths
    # OCR retains the visible labels. Graphic evaluation uses the default
    # exact source replay, so alpha blending cannot affect its pixel gates.
    canvas = reconstruct_canvas(paths, diagnostics, restore_source=False)
    height, width = canvas.shape[:2]
    for question in graphic:
        regions = question.get("drawing_regions") or []
        if not regions:
            raise ValueError("missing confirmed drawing regions for text masking")
        for region in regions:
            x1, y1, x2, y2 = _box(region, width, height)
            canvas[y1:y2, x1:x2] = 255
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    rendered = []
    for index, column in enumerate(diagnostics["evidence_columns"]):
        left, right = column.get("crop_left_px"), column.get("crop_right_px")
        if len(paths) == 1 and left is None and right is None:
            left, right = 0, width
        path = root / f"text-column-{index + 1}.png"
        # Keep labels/rail in the Plus copy, but apply whitening in measured
        # source coordinates. Flash reconstruction never sees the added rail.
        with Image.open(paths[index]) as original:
            output = original.convert("RGB")
            rect = _source_rect(column, output.size, right - left, height)
            output.paste(Image.fromarray(canvas[:, left:right]), rect[:2])
            output.save(path, format="PNG")
        rendered.append(str(path))
    return tuple(rendered)


def build_inputs(canvas: np.ndarray, question: dict, reference: dict) -> dict:
    height, width = canvas.shape[:2]
    regions = question.get("drawing_regions") or []
    if not 1 <= len(regions) <= 64:
        raise ValueError("missing confirmed drawing regions")
    visible = np.zeros((height, width), dtype=bool)
    for region in regions:
        x1, y1, x2, y2 = _box(region, width, height)
        visible[y1:y2, x1:x2] = True
    for region in reference["context_regions"]:
        x1, y1, x2, y2 = _box(region, width, height)
        if not visible[y1:y2, x1:x2].all():
            raise ValueError("reference context is outside restored student evidence")
    # The complete rendered mask below validates curved boundaries and brush
    # width as well as vertices; centerline-only checks are insufficient.
    yy, xx = np.where(visible)
    crop = (slice(yy.min(), yy.max() + 1), slice(xx.min(), xx.max() + 1))
    isolated = np.where(visible[:, :, None], canvas, 255).astype(np.uint8)
    overlays = []
    for index in range(len(reference["variants"])):
        mask = render_reference_mask(reference, width, height, index)
        if np.any((mask != 0) & ~visible):
            raise ValueError("complete reference stroke leaves restored student evidence")
        overlay = render_overlay(isolated, reference, index)
        overlay[~visible] = 255
        overlays.append(_png_uri(overlay[crop]))
    student_image = _png_uri(isolated[crop])
    return {"student_image": student_image, "overlays": overlays,
            "variant_prompts": [_reference_prompt(variant) for variant in reference["variants"]],
            "variant_source_images": [student_image if all(s["kind"] == "point" for s in variant["strokes"])
                                      else None for variant in reference["variants"]]}


async def assess_inputs(inputs: dict, *, teacher_id=None, pixel_gate: dict | None = None) -> list[dict]:
    from services.flash_grading import call_flash

    async def check(uri, index):
        if pixel_gate is not None:
            pixel = next(row for row in pixel_gate["variants"] if row["variant_index"] == index)
            # Only a computed coarse failure skips the model. "unavailable"
            # (e.g. a standard traced over print) still gets the overlay check.
            if pixel["status"] == "fail":
                return {"variant_index": index, "model_called": False, "p_correct": None,
                        "result": "pixel_rejected", "reason": pixel["reason"]}
        fallback = pixel_gate is not None and pixel["status"] == "unavailable"
        try:
            prompt = (inputs.get("variant_prompts") or [PROMPT] * len(inputs["overlays"]))[index]
            source_view = (inputs.get("variant_source_images") or [None] * len(inputs["overlays"]))[index]
            if fallback and not source_view and inputs.get("student_image"):
                source_view, prompt = inputs["student_image"], FALLBACK_PREFIX + prompt
            content = [{"type": "text", "text": prompt}]
            if source_view:
                content.append({"type": "image_url", "image_url": {"url": source_view}})
            content.append({"type": "image_url", "image_url": {"url": uri}})
            response = await call_flash([
                {"role": "user", "content": content},
            ], teacher_id=teacher_id, phase="drawing", json_object=True,
                response_validator=lambda content: validate_verdict(json.loads(content)))
            verdict = validate_verdict(json.loads(response["content"]))
            return {**verdict, "variant_index": index, "model_called": True, "model": response["model"],
                    "parameters": response["parameters"], "receipt": response.get("receipt", {}),
                    **({"pixel_fallback": True} if fallback else {})}
        except Exception:
            # A failed graphic check never blocks OCR/ordinary questions or awards zero.
            return {"variant_index": index, "model_called": True, "result": "needs_review", "p_correct": None,
                    "reason": "画图核对未返回有效结果，请老师查看证据", **({"pixel_fallback": True} if fallback else {})}
    return await asyncio.gather(*(check(uri, index) for index, uri in enumerate(inputs["overlays"])))


def _validated_pixels(gate: object, reference: dict, source_hash: str) -> dict[int, dict]:
    """A stale pre-pixel assessment cannot award credit or an automatic zero."""
    if (not isinstance(gate, dict) or gate.get("version") != PIXEL_VERSION
            or type(gate.get("threshold")) not in (int, float) or gate["threshold"] != PIXEL_THRESHOLD
            or gate.get("reference_hash") != reference["reference_hash"]
            or gate.get("source_sha256") != source_hash):
        raise ValueError("pixel gate is not bound to current evidence")
    rows = gate.get("variants")
    if not isinstance(rows, list) or len(rows) != len(reference["variants"]):
        raise ValueError("pixel gate variant mismatch")
    mapped = {}
    for row in rows:
        if (not isinstance(row, dict) or type(row.get("variant_index")) is not int
                or row["variant_index"] in mapped or not isinstance(row.get("reason"), str)):
            raise ValueError("invalid pixel result")
        score, status = row.get("score"), row.get("status")
        if status == "unavailable":
            if score is not None:
                raise ValueError("unavailable pixel result must not claim a score")
        elif status in {"pass", "fail"}:
            if (type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 100
                    or (score >= PIXEL_THRESHOLD) != (status == "pass")):
                raise ValueError("pixel score contradicts decision")
        else:
            raise ValueError("invalid pixel gate status")
        mapped[row["variant_index"]] = row
    if set(mapped) != set(range(len(reference["variants"]))):
        raise ValueError("pixel gate candidate identity mismatch")
    return mapped


def _dual_gate_outcome(gate: dict, checks: list, reference: dict, source_hash: str) -> tuple[str, str]:
    pixels = _validated_pixels(gate, reference, source_hash)
    if not isinstance(checks, list) or len(checks) != len(pixels):
        raise ValueError("graphic checks must cover each candidate")
    checked = {}
    for check in checks:
        if (not isinstance(check, dict) or type(check.get("variant_index")) is not int
                or check["variant_index"] not in pixels or check["variant_index"] in checked):
            raise ValueError("invalid graphic candidate identity")
        index = check["variant_index"]
        checked[index] = check
        status = pixels[index]["status"]
        if status == "pass" or (status == "unavailable" and check.get("model_called") is not False):
            validate_verdict({key: check.get(key) for key in ("result", "p_correct", "reason")})
            if check.get("model_called") is not True:
                raise ValueError("pixel pass requires separate model evidence")
        elif (check.get("model_called") is not False or check.get("p_correct") is not None
              or check.get("result") != ("pixel_rejected" if status == "fail" else "needs_review")):
            # An unavailable row without a model call is the earlier, stricter
            # record shape; it stays valid and resolves to teacher review.
            raise ValueError("pixel rejection must not be a model verdict")
    # Both gates belong to the SAME complete candidate, never separate variants.
    for index, pixel in pixels.items():
        if pixel["status"] == "pass" and accepted(checked[index]):
            return "correct", checked[index]["reason"]
    for index, pixel in pixels.items():
        if pixel["status"] == "unavailable" and checked[index].get("model_called") is True and accepted(checked[index]):
            return "correct", "像素门无法测量该图形，按 Flash 对照标准图判定通过：" + checked[index]["reason"]
    if pixels and all(pixel["status"] == "fail" for pixel in pixels.values()):
        best = max(pixel["score"] for pixel in pixels.values())
        displayed = math.floor(best * 100) / 100
        return "wrong", f"所有完整标准答案的像素匹配均低于{PIXEL_THRESHOLD}%（最高{displayed:g}%），按像素粗筛规则判错"
    if any(pixel["status"] == "unavailable" for pixel in pixels.values()):
        return "needs_review", "像素门无法测量该图形，Flash 对照标准图未达到自动通过门槛，请老师查看分项证据"
    return "needs_review", "画图尚未同时通过像素与模型两关，请老师查看分项证据"


async def assess_page(*, source_path: str, page_type_id: str, questions: list[dict],
                      transcriptions: list[dict], evidence_paths: tuple[str, ...],
                      diagnostics: dict, teacher_id=None, template_mode: str = "marked",
                      blank_template: np.ndarray | None = None,
                      marked_template: np.ndarray | None = None) -> dict:
    selected = [q for q in questions if q.get("answer_kind") in {"drawing", "mixed"}]
    if not selected:
        return {}
    source_hash = await asyncio.to_thread(image_content_hash, source_path)
    answers = {str(q.get("question_no") or q.get("question_number") or ""): q.get("student_answer")
               for q in transcriptions}
    canvas = None
    results = {}
    for question in selected:
        number = str(question.get("question_no") or question.get("question_number") or "")
        item = {"question_number": number, "policy": POLICY, "threshold": ACCEPT_THRESHOLD,
                "source_sha256": source_hash, "page_type_id": page_type_id,
                "transcript_sha256": transcript_digest(answers.get(number)), "checks": [],
                "rubric_sha256": rubric_digest(question),
                "decision": "needs_review", "reason": "请老师确认可比较的标准图"}
        try:
            raw = question.get("drawing_reference")
            if not isinstance(raw, dict):
                # A generated/teacher reference is optional. When it is absent,
                # preserve the drawing region and let Flash judge the question
                # directly from its text and the student's bounded crop.
                if not question.get("drawing_regions"):
                    item["reason"] = "画图题缺少已保存的作图区，请老师复核"
                    results[number] = item
                    continue
                if canvas is None:
                    canvas = await asyncio.to_thread(reconstruct_canvas, evidence_paths, diagnostics)
                item["comparison_mode"] = "custom"
                item["checks"] = [await assess_manual_input(canvas, question, teacher_id=teacher_id)]
                item["decision"], item["reason"] = _manual_outcome(item["checks"][0])
                results[number] = item
                continue
            reference = validate_confirmed_reference(raw)
            item["reference_hash"] = reference["reference_hash"]
            item["comparison_mode"] = reference["comparison_mode"]
            if not source_hash or not reference["teacher_confirmed"]:
                item["reason"] = "标准图未确认，请老师复核"
            else:
                if canvas is None:
                    canvas = await asyncio.to_thread(reconstruct_canvas, evidence_paths, diagnostics)
                if reference["comparison_mode"] == "manual":
                    item["checks"] = [await assess_manual_input(canvas, question, teacher_id=teacher_id)]
                    item["decision"], item["reason"] = _manual_outcome(item["checks"][0])
                elif reference["comparison_mode"] == "fixed":
                    inputs = await asyncio.to_thread(build_inputs, canvas, question, reference)
                    item["evidence"] = inputs
                    from services.drawing_pixel_gate import evaluate_pixel_gate
                    gate = await asyncio.to_thread(evaluate_pixel_gate, canvas, question, reference,
                                                   blank_template=blank_template, template_mode=template_mode,
                                                   marked_template=marked_template)
                    gate.update(reference_hash=reference["reference_hash"], source_sha256=source_hash)
                    _validated_pixels(gate, reference, source_hash)
                    item["pixel_gate"] = gate
                    item["checks"] = await assess_inputs(inputs, teacher_id=teacher_id, pixel_gate=gate)
                    item["decision"], item["reason"] = _dual_gate_outcome(gate, item["checks"], reference, source_hash)
                else:
                    item["reason"] = "标准图比较模式不受支持，请老师复核"
        except (ValueError, OSError, TypeError):
            item["reason"] = "标准图或实际作答证据不完整，请老师复核"
        results[number] = item
    return results


def bound_assessment(paper: dict, unit: dict) -> dict:
    number = str(unit["parent_question_no"])
    reference = unit["entry"].get("drawing_reference")
    assessments = paper.get("drawing_assessments") or paper.get("_drawing_assessments") or {}
    raw = assessments.get(number) if isinstance(assessments, dict) else None
    fallback = {"decision": "needs_review", "policy": POLICY, "threshold": ACCEPT_THRESHOLD,
                "reason": "缺少当前标准图绑定的画图核对证据，请老师复核", "checks": []}
    try:
        if not isinstance(raw, dict):
            return fallback
        custom = reference is None and raw.get("comparison_mode") == "custom"
        if custom:
            if (raw.get("policy") != POLICY or raw.get("question_number") != number
                    or type(raw.get("threshold")) not in (int, float) or raw["threshold"] != ACCEPT_THRESHOLD
                    or not raw.get("source_sha256")
                    or raw["source_sha256"] != image_content_hash(paper.get("image_path", ""))
                    or raw.get("transcript_sha256") != transcript_digest(unit.get("student_answer"))
                    or raw.get("rubric_sha256") != rubric_digest(unit["entry"])
                    or raw.get("page_type_id") != (paper.get("page_type_id") or paper.get("ocr_page_type"))):
                return fallback
            checks = raw.get("checks")
            if not isinstance(checks, list) or len(checks) != 1:
                return {**raw, "decision": "needs_review", "comparison_mode": "custom",
                        "reason": "自定义作图核对证据不完整，请老师复核"}
            decision, reason = _manual_outcome(checks[0])
            return {**raw, "decision": decision, "comparison_mode": "custom", "reason": reason}
        if not isinstance(reference, dict):
            return fallback
        reference = validate_confirmed_reference(reference)
        if (raw.get("reference_hash") != reference["reference_hash"]
                or raw.get("policy") != POLICY or raw.get("question_number") != number
                or type(raw.get("threshold")) not in (int, float) or raw["threshold"] != ACCEPT_THRESHOLD
                or not raw.get("source_sha256")
                or raw["source_sha256"] != image_content_hash(paper.get("image_path", ""))
                or raw.get("transcript_sha256") != transcript_digest(unit.get("student_answer"))
                or raw.get("rubric_sha256") != rubric_digest(unit["entry"])
                or raw.get("page_type_id") != (paper.get("page_type_id") or paper.get("ocr_page_type"))):
            return fallback
        if reference["comparison_mode"] == "manual":
            checks = raw.get("checks")
            if not isinstance(checks, list) or len(checks) != 1:
                return {**raw, "decision": "needs_review", "comparison_mode": "manual",
                        "reason": "此题为自选比例尺或无法列全合法作图，保留人工核对，不按固定坐标自动判分"}
            decision, reason = _manual_outcome(checks[0])
            return {**raw, "decision": decision, "comparison_mode": "manual", "reason": reason}
        decision, reason = _dual_gate_outcome(raw.get("pixel_gate"), raw.get("checks"), reference, raw["source_sha256"])
        return {**raw, "decision": decision, "reason": reason}
    except (ValueError, TypeError, KeyError):
        return fallback
