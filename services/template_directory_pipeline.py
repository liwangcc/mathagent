"""Authoritative leaf directory shared by template solving and visual evidence.

Teacher templates share a Flash directory between the Qwen locator and the
independent Max solver and Flash text transcription. All calls share the import budget.
"""
from __future__ import annotations

import asyncio
import copy
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
import math
import re
import tempfile
from pathlib import Path

from PIL import Image

from services.analyzer import parse_llm_json
from services.grading_contract import merge_directory_leaf_score_layer
from services.ocr_service import call_gpt_vision, set_vision_phase_key, reset_vision_phase_key
from services.processing_errors import describe_failure, processing_stage, report_failure
from services.teaching_context import current_teaching_context
from services import template_one_three as one_three
from services import template_locator_tiles as locator_tiles
from services import template_solver_shards as solver_shards


logger = logging.getLogger(__name__)
_import_context: ContextVar[tuple[float, dict] | None] = ContextVar("template_import_context", default=None)
_REQUEST_PHASES = frozenset({one_three.SOLVE_PHASE, one_three.TRANSCRIBE_PHASE, "template-merged-v1", "template-hierarchy-v1", "template-coarse-locator-v1", "template-directory-v1",
                             "template-directory-drawing-v1"})
_COARSE_LOCATOR_MAX_TOKENS = 4096

@contextmanager
def template_import_context(deadline_at: float, progress: dict):
    """Share the caller's absolute model deadline and safe stage telemetry."""
    if (isinstance(deadline_at, bool) or not isinstance(deadline_at, (int, float))
            or not math.isfinite(deadline_at)):
        raise ValueError("template model deadline must be finite")
    if not isinstance(progress, dict) or not isinstance(progress.setdefault("stages", {}), dict):
        raise ValueError("template progress stages must be a dictionary")
    token = _import_context.set((float(deadline_at), progress))
    try:
        yield
    finally:
        _import_context.reset(token)


def _stage_diagnostics() -> dict:
    context = _import_context.get()
    return {"model_stages": copy.deepcopy(context[1]["stages"])} if context is not None else {}


async def _request(
    images,
    prompt: str,
    phase: str,
    validator,
    *,
    max_tokens: int = 16000,
    reasoning_effort: str | None = "high",
    pool_route: str = "fallback",
    output_adapter=None,
    expected_ids=(),
    prompt_first: bool = True,
    stage_key: str | None = None,
):
    """Keep transport admission and execution inside one shared import budget.

    ``stage_key`` names the progress record when one phase runs as several
    parallel requests (solver shards); it defaults to the phase.
    """
    if phase not in _REQUEST_PHASES:
        raise ValueError("unknown template model phase")
    if output_adapter is not None and phase != one_three.TRANSCRIBE_PHASE:
        raise ValueError("template envelope adaptation is transcription-only")
    if not prompt_first and phase != one_three.SOLVE_PHASE:
        raise ValueError("image-first ordering is solver-only")
    stage_key = stage_key or phase
    loop = asyncio.get_running_loop()
    started = loop.time()
    context = _import_context.get()
    deadline_at = context[0] if context is not None else None
    trace_id = context[1].get("trace_id", "") if context is not None else ""
    if not isinstance(trace_id, str) or not re.fullmatch(r"[0-9a-f]{12}p[1-9][0-9]{0,3}", trace_id):
        trace_id = "none"
    record = {"phase": phase, "status": "running", "call_started": False,
              "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": None,
              "elapsed_s": 0.0, "remaining_s": max(0.0, deadline_at - started) if deadline_at is not None else None}
    if context is not None:
        context[1]["stages"][stage_key] = record
    logger.info("Template model stage started trace_id=%s phase=%s remaining_s=%s",
                trace_id, stage_key, record["remaining_s"])
    token = set_vision_phase_key(phase)
    strict_phase = phase in {one_three.SOLVE_PHASE, one_three.TRANSCRIBE_PHASE, "template-hierarchy-v1", "template-coarse-locator-v1", "template-directory-v1"}
    parse_response = one_three.parse_json if strict_phase else parse_llm_json
    diagnostic_context = {
        "stage": "solve" if phase == one_three.SOLVE_PHASE else (
            "transcription" if phase == one_three.TRANSCRIBE_PHASE else "template"),
        "model": "qwen3.8-flash" if phase in {"template-hierarchy-v1", "template-coarse-locator-v1", one_three.TRANSCRIBE_PHASE} else "qwen3.8-max",
        "route": "payg" if phase in {"template-hierarchy-v1", "template-coarse-locator-v1", one_three.TRANSCRIBE_PHASE} else pool_route,
        "phase": phase, "reasoning_effort": reasoning_effort,
    }
    raw = None

    def validate_output(text):
        nonlocal raw
        raw = text
        return validator(parse_response(text))

    try:
        # timeout_at only schedules cancellation on the next event-loop turn;
        # check explicitly so an already-expired phase never invokes transport.
        if deadline_at is not None and loop.time() >= deadline_at:
            raise TimeoutError("template model budget exhausted before phase")
        async with asyncio.timeout_at(deadline_at):
            record["call_started"] = True
            if phase in {"template-hierarchy-v1", "template-coarse-locator-v1", one_three.TRANSCRIBE_PHASE}:
                from services.ocr_service import _encode_vision_image
                paths = [images] if isinstance(images, str) else list(images)
                encoding = asyncio.gather(*(
                    asyncio.to_thread(_encode_vision_image, path) for path in paths))
                try:
                    # Shield native file reads from sibling-stage cancellation;
                    # otherwise Windows can tear down the temporary crop
                    # directory while a worker thread still owns one PNG.
                    encoded = await asyncio.shield(encoding)
                except asyncio.CancelledError:
                    try:
                        await asyncio.shield(encoding)
                    except Exception:
                        pass
                    raise
                content = [{"type": "text", "text": prompt}] + [
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}
                    for data, mime in encoded]
                from services.flash_grading import call_flash
                text_ocr = phase == one_three.TRANSCRIBE_PHASE
                structured_flash = phase in {"template-hierarchy-v1", "template-coarse-locator-v1"} or text_ocr
                # Template reading includes task selection; its caller owns
                # the thinking tier. Student handwriting OCR is separate.
                flash_thinking = reasoning_effort not in {None, "none"} if structured_flash else None
                flash_reasoning_effort = reasoning_effort if structured_flash else None
                flash_stage = "transcription" if text_ocr else "template"
                with processing_stage(flash_stage, model="qwen3.8-flash", route="payg", phase=phase):
                    response = await call_flash(
                        # Existing image-capable payg transport; semantic phase
                        # remains separately recorded in template progress.
                        [{"role": "user", "content": content}], phase="template_hierarchy" if phase in {"template-hierarchy-v1", "template-coarse-locator-v1"} or text_ocr else "drawing",
                        enable_thinking=flash_thinking,
                        reasoning_effort=flash_reasoning_effort,
                        max_tokens=max_tokens if phase in {"template-hierarchy-v1", "template-coarse-locator-v1"} or text_ocr else None,
                        json_object=phase in {"template-hierarchy-v1", "template-coarse-locator-v1"} or text_ocr,
                        response_validator=validate_output,
                        output_adapter=output_adapter,
                        diagnostic_phase=phase,
                    )
                raw = response["content"]
                record.update(model=response["model"], receipt=response.get("receipt", {}))
                if phase in {"template-hierarchy-v1", "template-coarse-locator-v1"} or text_ocr:
                    record.update(reasoning_effort=reasoning_effort,
                                  enable_thinking=flash_thinking, max_tokens=max_tokens, route="payg")
            else:
                stage = "solve" if phase == one_three.SOLVE_PHASE else "template"
                with processing_stage(stage, model="qwen3.8-max", route=pool_route, phase=phase):
                    raw = await call_gpt_vision(
                        images, prompt, max_tokens=max_tokens, pool_route=pool_route,
                        reasoning_effort=reasoning_effort, prompt_first=prompt_first,
                        json_object=strict_phase,
                        response_validator=validate_output,
                        cancel_on_waiter_cancel=True,
                    )
                record.update(model="qwen3.8-max", reasoning_effort=reasoning_effort, max_tokens=max_tokens)
            parsed = parse_response(raw)
            result = validator(parsed)
        record["status"] = "completed"
        return result
    except TimeoutError as exc:
        record["status"] = "timeout"
        describe_failure(exc, **diagnostic_context, duration_ms=(loop.time() - started) * 1000)
        raise
    except asyncio.CancelledError:
        record["status"] = "cancelled"
        raise
    except Exception as exc:
        record["status"] = "failed"
        info = describe_failure(exc, **diagnostic_context, duration_ms=(loop.time() - started) * 1000)
        from services.template_output_evidence import save_output_failure
        receipt_id = save_output_failure(
            exc, error_info=info, phase=phase, model=diagnostic_context["model"],
            route=diagnostic_context["route"], prompt=prompt, content=raw,
            expected_ids=expected_ids, trace_id=trace_id,
        )
        if receipt_id:
            describe_failure(exc, **diagnostic_context, artifact_refs=[receipt_id])
        raise
    finally:
        finished = loop.time()
        record.update(finished_at=datetime.now(timezone.utc).isoformat(), elapsed_s=round(finished - started, 3),
                      remaining_s=max(0.0, deadline_at - finished) if deadline_at is not None else None)
        logger.info("Template model stage finished trace_id=%s phase=%s status=%s elapsed_s=%s remaining_s=%s",
                    trace_id, stage_key, record["status"], record["elapsed_s"], record["remaining_s"])
        reset_vision_phase_key(token)


_HIERARCHY_PROMPT = """按试卷阅读顺序建立题目与作答位清单：分栏先左后右，栏内从上到下。
每道独立题用question_number从1连续编号；printed_path记录已看见的所属分区与原题号，没有单独印刷题号时仅包含已看见的非空上级路径，路径项均为非空文字。同题多个作答位列在slots内：每个独立填空或计算式一项，连续演算区一项，需学生完成的作图位置也一项。
印刷示意图、坐标图、几何图、统计图、观察图属于题干条件；“看图回答”不等于需要作图。只有题干明确要求学生新增画图、补画、作图、描点、连线、涂色、标注或补全图形，才建立作图槽位；不明确时不要猜成作图题。
slot_id按题内阅读顺序编号为题号[序号]；cue引用该位置的印刷小问号、算式或简短位置说明，供后续模型认同一处。题数取questions长度，每题作答位数取slots长度，每题1至20项；超出时不截断、不合并或遗漏真实作答位，由系统拒收并报告。
完整响应是单个JSON对象：{"contract_version":4,"questions":[{"question_number":"1","printed_path":["基础练习","1."],"slots":[{"slot_id":"1[1]","cue":"第一个填空"},{"slot_id":"1[2]","cue":"第二个填空"}]}]}"""

_COARSE_LOCATOR_PROMPT = """按试卷阅读顺序粗定位每道独立题。只识别题目整体边界，不判断答案、题型或作图属性。
坐标为整页 normalized_1000_v1；输入若为分栏图，各栏沿用整页坐标，不重新归一化。框必须同时覆盖完整题干、图形和该题作答区域，宁可适度留白，不可裁掉小刻度或印刷条件。
只返回一个JSON对象，结构示例（坐标须按原图实测）：{"coordinate_space":"normalized_1000_v1","questions":[{"question_number":"1","bbox":[100,200,900,800]}]}。题号从1连续编号；有歧义时仍返回可见候选，后续由服务端校验，不能推测标准答案。"""


# Must agree with the base rule (choice question_text is "") and merge_branches,
# which sends Flash text on a Max-confirmed choice question to teacher review.
_PARALLEL_TRANSCRIPTION_NOTE = (
    "\n本轮 Flash 转录与 Max 定位并行，转录阶段尚未收到 Max 的最终题型，请按上述规则自行分流："
    "判断为选择题的 question_text 为空字符串，其余题按原图转录印刷正文。"
    "若与 Max 最终题型不一致，合并阶段以 Max 为准并交教师核对，不能据此覆盖 Max 结论。"
)


def _receive_coarse_locator(payload: object, *, width: int, height: int) -> dict:
    """Receive independent Flash crop hints without making them authoritative."""
    if (not isinstance(payload, dict) or set(payload) != {"coordinate_space", "questions"}
            or payload["coordinate_space"] != "normalized_1000_v1"
            or not isinstance(payload["questions"], list)
            or not 1 <= len(payload["questions"]) <= 200):
        raise ValueError("Flash 粗定位合同无效")
    rows = []
    for index, row in enumerate(payload["questions"], 1):
        if (not isinstance(row, dict) or set(row) != {"question_number", "bbox"}
                or row["question_number"] != str(index)):
            raise ValueError("Flash 粗定位题号无效")
        bbox = _box_or_empty(row["bbox"], "Flash 粗定位 bbox", allow_empty=False)
        # Image dimensions are bound before transport so a syntactically valid
        # box cannot silently map to an empty pixel crop.
        x1, y1, x2, y2 = bbox
        if round(x2 * width / 1000) <= round(x1 * width / 1000) or round(y2 * height / 1000) <= round(y1 * height / 1000):
            raise ValueError("Flash 粗定位裁图为空")
        rows.append({"question_number": str(index), "bbox": bbox})
    return {"coordinate_space": "normalized_1000_v1", "questions": rows}


def _coarse_crop_paths(image_path: str, coarse: dict, output_dir: str, *, width: int, height: int) -> list[str]:
    """Create padded evidence views; always keep the original full page."""
    paths = []
    seen = set()
    total_area = 0
    max_total_area = width * height * 4
    with Image.open(image_path) as opened:
        image = opened.convert("RGB")
        try:
            if image.size != (width, height):
                raise ValueError("粗定位原图尺寸已变化")
            for row in coarse["questions"][:11]:
                x1, y1, x2, y2 = row["bbox"]
                key = tuple(row["bbox"])
                if key in seen:
                    continue
                seen.add(key)
                # Preserve the question and nearby axis labels even when a fast
                # proposal is a little tight. Each crop remains evidence only.
                left = max(0, math.floor((x1 - 30) * width / 1000))
                top = max(0, math.floor((y1 - 50) * height / 1000))
                right = min(width, math.ceil((x2 + 30) * width / 1000))
                bottom = min(height, math.ceil((y2 + 35) * height / 1000))
                if right <= left or bottom <= top:
                    raise ValueError("Flash 粗定位扩边后裁图为空")
                area = (right - left) * (bottom - top)
                if paths and total_area + area > max_total_area:
                    continue
                path = Path(output_dir) / f"coarse-question-{row['question_number']}.png"
                crop = image.crop((left, top, right, bottom))
                try:
                    crop.save(path, format="PNG")
                finally:
                    crop.close()
                paths.append(str(path))
                total_area += area
        finally:
            image.close()
    return paths


def _answer_slot_count(row: dict) -> int:
    """Return a bounded Flash slot count, with legacy parts fallback."""
    kinds = row.get("area_kinds")
    if isinstance(kinds, list) and kinds:
        return sum(kind == "answer" for kind in kinds) or 1
    value = row.get("answer_area_count", row.get("answer_slot_count"))
    if value is None:
        value = len(row.get("parts") or []) or 1
    # Some Flash responses use zero for a section node. Sections never become
    # grading leaves, so normalize that harmless marker to the safe default.
    if value == 0:
        value = 1
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 20:
        raise ValueError("Flash 作答空数无效")
    return value


def _region_slot_count(row: dict) -> int:
    """Return the physical leaf-region count, including drawing regions."""
    kinds = row.get("area_kinds")
    if isinstance(kinds, list) and kinds:
        if any(kind not in {"answer", "drawing"} for kind in kinds):
            raise ValueError("Flash 作答区域类型无效")
        return len(kinds)
    return _answer_slot_count(row)


def _drawing_area_count(row: dict) -> int:
    kinds = row.get("area_kinds")
    if isinstance(kinds, list) and kinds:
        if any(kind not in {"answer", "drawing"} for kind in kinds):
            raise ValueError("Flash 作答区域类型无效")
        return sum(kind == "drawing" for kind in kinds)
    value = row.get("drawing_area_count", 0)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 20:
        raise ValueError("Flash 作图区数量无效")
    return value


def _box_or_empty(value: object, field: str, *, allow_empty: bool = True) -> list[float]:
    if value is None and allow_empty:
        return []
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError(f"{field} 必须是四点坐标")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1000 for v in value):
        raise ValueError(f"{field} 坐标无效")
    box = [float(v) for v in value]
    if box[0] >= box[2] or box[1] >= box[3]:
        raise ValueError(f"{field} 必须具有正面积")
    return box


def _point_or_none(value: object, field: str):
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1000 for v in value):
        raise ValueError(f"{field} 坐标无效")
    return [float(v) for v in value]


def _validate_locator(payload: object, hierarchy: dict) -> dict:
    if hierarchy.get("question_identity_version") in {3, 4}:
        return one_three.max_owned.receive_locator(payload, one_three.question_contract(hierarchy))
    if not isinstance(payload, dict) or set(payload) - {"coordinate_space", "questions", "needs_review"}:
        raise ValueError("Max 定位返回字段无效")
    if payload.get("coordinate_space") != "normalized_1000_v1":
        raise ValueError("Max 定位坐标系无效")
    rows = payload.get("questions")
    expected = hierarchy["leaf_ids"]
    if not isinstance(rows, list) or [row.get("question_number") if isinstance(row, dict) else None for row in rows] != expected:
        raise ValueError("Max 定位题号与 Flash 层级不一致")
    allowed = {"question_number", "question_number_bbox", "question_type", "choice_mode", "requires_drawing", "answer_kind", "regions", "drawing_regions", "label_anchor", "answer_slot_count", "answer_region_count", "slot_ids"}
    normalized = []
    issues = list(payload.get("needs_review") or [])
    for row in rows:
        if set(row) - allowed or not isinstance(row.get("question_number"), str):
            raise ValueError("Max 定位题目字段无效")
        if row["question_type"] not in {"choice", "fill", "solve"} or row["choice_mode"] not in {"single", "multiple", "unknown"}:
            raise ValueError("Max 定位题型无效")
        if not isinstance(row.get("requires_drawing"), (bool, type(None))):
            raise ValueError("Max 定位作图判断无效")
        kind = row.get("answer_kind")
        if kind not in {"non_graphic", "drawing", "mixed"}:
            raise ValueError("Max 定位作答类型无效")
        regions = row.get("regions", [])
        if not isinstance(regions, list):
            raise ValueError("Max 定位作答框必须为数组")
        regions = [_box_or_empty(box, "regions", allow_empty=False) for box in regions]
        node = next(node for node in hierarchy["nodes"] if node["id"] == row["question_number"])
        expected_slots = _region_slot_count(node)
        expected_answer_slots = _answer_slot_count(node)
        if row.get("answer_region_count") is not None and row["answer_region_count"] != len(regions):
            issues.append(f"{row['question_number']} 作答框数量 {row['answer_region_count']} 与实际框数 {len(regions)} 不一致")
        if row.get("answer_slot_count") is not None and row["answer_slot_count"] not in {expected_slots, expected_answer_slots}:
            raise ValueError("Max 定位作答空数与 Flash 层级不一致")
        slot_ids = row.get("slot_ids")
        expected_slot_ids = [f"{row['question_number']}[{index}]" for index in range(1, expected_slots + 1)]
        # A count mismatch is a teacher-review draft; do not reject it merely
        # because the locator cannot provide a trustworthy slot map yet.
        slot_map_is_incomplete = len(regions) != expected_slots and slot_ids == []
        if (hierarchy.get("region_count_contract") or slot_ids is not None) and slot_ids != expected_slot_ids and not slot_map_is_incomplete:
            raise ValueError("Max 定位作答空顺序与 Flash 契约不一致")
        if len(regions) != expected_slots:
            issues.append(f"{row['question_number']} 作答框数量 {len(regions)} 与 Flash 预期 {expected_slots} 不一致")
        drawing = row.get("drawing_regions", [])
        if not isinstance(drawing, list):
            raise ValueError("Max 定位作图区必须为数组")
        drawing = [_box_or_empty(box, "drawing_regions", allow_empty=False) for box in drawing]
        if hierarchy.get("region_count_contract") and any(box not in regions for box in drawing):
            issues.append(f"{row['question_number']} 作图区未对应目录中的作答框")
        if kind == "non_graphic" and (drawing or row.get("label_anchor") is not None):
            raise ValueError("non_graphic 不得包含作图区域")
        anchor = _point_or_none(row.get("label_anchor"), "label_anchor")
        if kind != "non_graphic" and (
            not drawing or anchor is None or anchor[0] >= drawing[0][0]
        ):
            # The artifact consumer requires this geometry. Surface a review
            # draft at the response boundary, not a deceptively ready template
            # that fails later while materializing student evidence.
            issues.append(f"{row['question_number']} 作图区域或左侧题号标记不完整，需教师补充")
        normalized.append({
            "question_number": row["question_number"],
            "question_number_bbox": _box_or_empty(row.get("question_number_bbox"), "question_number_bbox"),
            "question_type": row["question_type"], "choice_mode": row["choice_mode"],
            "requires_drawing": row.get("requires_drawing"), "answer_kind": kind,
            "regions": regions, "drawing_regions": drawing,
            "label_anchor": anchor,
            "answer_slot_count": expected_slots,
            "answer_area_count": expected_answer_slots,
            "region_kinds": list(node.get("area_kinds") or ["answer"] * expected_slots),
            "slot_ids": expected_slot_ids if len(regions) == expected_slots else [],
        })
    return {"coordinate_space": "normalized_1000_v1", "questions": normalized,
            "needs_review": copy.deepcopy(issues)}


def _validate_one_three_locator(payload: object, hierarchy: dict) -> dict:
    """Keep the approved locating semantics but reject incomplete slot bindings."""
    result = _validate_locator(payload, hierarchy)
    if hierarchy.get("question_identity_version") in {3, 4}:
        return result
    if not isinstance(payload.get("needs_review", []), list) or any(
            not isinstance(issue, str) for issue in payload.get("needs_review", [])):
        raise ValueError("Max 定位 needs_review 必须为字符串数组")
    for raw, row, expected in zip(payload["questions"], result["questions"], one_three.slot_contract(hierarchy)):
        ids = [slot["slot_id"] for slot in expected["slots"]]
        if hierarchy.get("question_identity_version") == 2 and row["question_type"] == "choice" and len(ids) != 1:
            raise ValueError("每道独立选择题必须单独编号，不能合并为多槽选择题")
        if raw.get("slot_ids") != ids or len(row["regions"]) != len(ids):
            raise ValueError("Max 定位槽位或作答框不完整")
        for field in ("answer_region_count", "answer_slot_count"):
            if field in raw and (type(raw[field]) is not int or raw[field] != len(ids)):
                raise ValueError("Max 定位声明槽位数量与物理槽位不一致")
        drawing = [box for box, kind in zip(row["regions"], row["region_kinds"]) if kind == "drawing"]
        kind = "non_graphic" if not drawing else ("drawing" if len(drawing) == len(ids) else "mixed")
        if row["drawing_regions"] != drawing or row["answer_kind"] != kind or row["requires_drawing"] is not bool(drawing):
            raise ValueError("Max 定位作图类型与物理槽位不一致")
        if drawing and (row["label_anchor"] is None or row["label_anchor"][0] >= drawing[0][0]):
            raise ValueError("Max 定位作图标签几何不完整")
    return result


def _directory_from_hierarchy(hierarchy: dict, locator: dict) -> dict:
    max_owned = hierarchy.get("question_identity_version") in {3, 4}
    expected_source = (one_three.max_owned.SHARED_SLOT_SOURCE if hierarchy.get("question_identity_version") == 4
                       else one_three.max_owned.SLOT_SOURCE)
    if max_owned and (locator.get("slot_source") != expected_source
                      or {r["question_number"] for r in locator["questions"]} != set(hierarchy["leaf_ids"])):
        raise ValueError("纯层级目录缺少对应的Max定位槽合同")
    by_id = {row["question_number"]: row for row in locator["questions"]}
    node_by_id = {row["id"]: row for row in hierarchy["nodes"]}
    node_ids = {node_id: f"n{index:03d}" for index, node_id in enumerate((row["id"] for row in hierarchy["nodes"]), 1)}
    leaves, flat = [], []
    for row in hierarchy["nodes"]:
        node_id = row["id"]
        parent = row.get("parent_id")
        leaf = node_id in by_id
        located = by_id.get(node_id, {})
        printed_path = list(row.get("source_path") or [])
        if not printed_path and not hierarchy.get("region_count_contract") and not max_owned:
            cursor = node_id
            while cursor is not None:
                parent_row = node_by_id[cursor]
                printed_path.append(parent_row.get("raw_label"))
                cursor = parent_row.get("parent_id")
            printed_path.reverse()
        entry = {"node_id": node_ids[node_id], "parent_id": node_ids.get(parent), "printed_label": row.get("raw_label"),
                 "printed_path": printed_path,
                 "numeric_path": node_id.split("."), "source_hierarchy_path": printed_path,
                 "kind": "question" if leaf else ("section" if parent is None else "group"),
                 "context_summary": row.get("cue", ""), "shared_context": [], "question_type": located.get("question_type"),
                 "choice_mode": located.get("choice_mode", "unknown"), "question_number_bbox": located.get("question_number_bbox") or None,
                 "number_bbox": located.get("question_number_bbox") or None, "regions": located.get("regions", []), "exemptions": [],
                 "is_leaf": leaf, "answer_kind": located.get("answer_kind", "non_graphic"), "drawing_regions": located.get("drawing_regions", []),
                 "label_anchor": located.get("label_anchor"), "requires_drawing": located.get("requires_drawing"),
                 "answer_slot_count": located["answer_slot_count"] if max_owned else located.get("answer_slot_count", _region_slot_count(row)),
                 "answer_area_count": located["answer_area_count"] if max_owned else located.get("answer_area_count", _answer_slot_count(row)),
                 "drawing_area_count": len(located.get("drawing_regions", [])) if hierarchy.get("region_count_contract") or hierarchy.get("question_identity_version") in {3, 4} else _drawing_area_count(row),
                 "region_kinds": list(located["region_kinds"] if max_owned else (located.get("region_kinds") or row.get("area_kinds") or ["answer"] * _region_slot_count(row))),
                 "slot_ids": located.get("slot_ids", []),
                 **({"answer_position": located["answer_position"]} if located.get("answer_position") else {}),
                 "question_number": node_id if leaf else None, "q_id": "Q" + node_id if leaf else None}
        flat.append(entry)
        if leaf:
            leaves.append(copy.deepcopy(entry))
    issues = list(hierarchy.get("needs_review") or []) + list(locator.get("needs_review") or [])
    return {"version": "question_directory_v1", "coordinate_space": "normalized_1000_v1", "flat_compatible": all("." not in leaf["question_number"] for leaf in leaves),
            "nodes": flat, "leaves": leaves, "needs_review": issues}


def _template_manifest(hierarchy: dict) -> str:
    if hierarchy.get("question_identity_version") in {3, 4}:
        return json.dumps({"question_groups": one_three.question_contract(hierarchy), "question_numbers": hierarchy["leaf_ids"]},
                          ensure_ascii=False, separators=(",", ":"))
    if not hierarchy.get("canonicalized"):
        slot_contract = [{"q_id": "Q" + node_id, "answer_slot_count": _answer_slot_count(next(node for node in hierarchy["nodes"] if node["id"] == node_id)),
                         "slot_ids": [f"{node_id}[{index}]" for index in range(1, _answer_slot_count(next(node for node in hierarchy["nodes"] if node["id"] == node_id)) + 1)],
                         "slot_order": "reading_order"} for node_id in hierarchy["leaf_ids"]]
        return json.dumps({"nodes": hierarchy["nodes"], "leaf_ids": hierarchy["leaf_ids"], "answer_slot_contract": slot_contract}, ensure_ascii=False, separators=(",", ":"))
    region_contract = []
    for node_id in hierarchy["leaf_ids"]:
        node = next(node for node in hierarchy["nodes"] if node["id"] == node_id)
        count = _region_slot_count(node)
        region_contract.append({"question_number": node_id,
                                "q_id": "Q" + node_id,
                                "answer_region_count": count,
                                "answer_area_count": _answer_slot_count(node),
                                "region_kinds": list(node.get("area_kinds") or ["answer"] * count),
                                "region_order": "reading_order"})
    if hierarchy.get("region_count_contract"):
        for group in region_contract:
            group["slot_ids"] = [f"{group['question_number']}[{index}]" for index in range(1, group["answer_region_count"] + 1)]
    if hierarchy.get("question_identity_version") == 2:
        by_id = {node["id"]: node for node in hierarchy["nodes"]}
        for group in region_contract:
            node = by_id[group["question_number"]]
            group.update(printed_label=node["raw_label"], cue=node["cue"])
    # Source labels identify pixels; sequential Q and slot IDs remain authority.
    return json.dumps({"question_groups": region_contract, "question_numbers": hierarchy["leaf_ids"]},
                      ensure_ascii=False, separators=(",", ":"))


def _parallel_template_prompts(hierarchy: dict, *, width: int, height: int, columns: list[dict]) -> tuple[str, str]:
    if hierarchy.get("question_identity_version") in {3, 4}:
        return one_three.max_owned.locator_prompt(one_three.question_contract(hierarchy), width=width, height=height, columns=columns), ""
    manifest = _template_manifest(hierarchy)
    column_hint = json.dumps(columns, ensure_ascii=False, separators=(",", ":"))
    slot_schema = ',"slot_ids":["1[1]"]' if hierarchy.get("region_count_contract") else ""
    locator = (f"按目录定位作答区域，坐标 normalized_1000_v1，整页 {width}×{height}。\n"
               "只输出目录中的自然数 Q；每个独立区域一个框，同题多框逐个返回，按阅读顺序排列。\n"
               "各图网格均为整页坐标，分栏裁图不重新归一化。无论是否有笔迹均框选作答区域，有笔迹须完整覆盖。\n"
               "drawing/mixed 必须有 drawing_regions 及位于主作图区左侧的 label_anchor=[x,y]；non_graphic 才为 null。\n"
               f"分栏：{column_hint}\n目录：{manifest}\n"
               '严格返回裸 JSON，不加 Markdown 代码围栏：{"coordinate_space":"normalized_1000_v1","questions":[{"question_number":"1"'
               + slot_schema + ',"question_number_bbox":[x1,y1,x2,y2],"question_type":"choice|fill|solve","choice_mode":"single|multiple|unknown","requires_drawing":false,"answer_kind":"non_graphic|drawing|mixed","answer_region_count":1,"regions":[[x1,y1,x2,y2]],"drawing_regions":[],"label_anchor":null}],"needs_review":[]}')
    if hierarchy.get("question_identity_version") == 2:
        locator += "\nprinted_label和cue用于原图认同一题，Q与槽位已固定，不重新编号、不拆并题。每道独立选择题对应一个Q和一个answer槽。"
    if hierarchy.get("region_count_contract"):
        locator += "\n逐栏、栏内从上到下、同行从左到右。slot_ids 沿用目录，regions 按其顺序一一对应，含作图区；目录中的region_kinds标明每个slot是answer或drawing，drawing_regions 是其中的作图框。"
    if not hierarchy.get("canonicalized"):
        locator += "\n兼容旧目录字段：" + json.dumps({"q_id": "Q" + hierarchy["leaf_ids"][0], "answer_slot_count": _answer_slot_count(next(node for node in hierarchy["nodes"] if node["id"] == hierarchy["leaf_ids"][0])), "slot_ids": [f"{hierarchy['leaf_ids'][0]}[{index}]" for index in range(1, _answer_slot_count(next(node for node in hierarchy["nodes"] if node["id"] == hierarchy["leaf_ids"][0])) + 1)]}, ensure_ascii=False, separators=(",", ":"))
    region_schema = ',"region_cues":["该填写位置的印刷文字或图示位置"]' if hierarchy.get("region_count_contract") else ""
    transcription = ("按目录顺序转录完整印刷题干（含选项和共用条件），忽略学生笔迹，不解题。\n"
                     "将解题所需的详细图形、图表信息写入 solve_visual_context，不能只写‘如图’或图形名称。\n"
                     "facts 逐项记录对象名称及标注、数值与单位、坐标轴方向和范围、已标刻度、未标数字的小刻度及间隔数、"
                     "比例尺、表格行列标题与数据、点或图线对应的位置；分格图还需记录总格数、分组和印刷阴影。\n"
                     "relations 记录图上可见或明确标注的连接、交点、端点、箭头方向、平行、垂直、等长和角度关系。"
                     "没有直接标数但可以数刻度的位置，要记录两端已标数值、等分间隔数及目标对应第几格，供解题推算；"
                     "不要因为目标旁未印数值就断言缺少条件，也不要仅凭图形外观假定精确关系。\n"
                     "uncertain 只列确实无法辨清的对象、标注及原因；区分直接标注、刻度估读和不确定信息。"
                     "学生填写的数值、涂色和辅助线不能作为印刷条件；无法区分印刷与笔迹时明确注明。\n"
                     f"目录：{manifest}\n"
                     '返回 JSON：{"questions":[{"q_id":"Q1"' + slot_schema + region_schema
                     + ',"question_text":"完整题干","solve_visual_context":{"facts":[],"relations":[],"uncertain":[]}}]}')
    if hierarchy.get("region_count_contract"):
        transcription += "\n逐栏、栏内从上到下、同行从左到右。slot_ids 沿用目录，region_cues逐项对应；不得调整slot顺序，drawing位置保留空答案槽位，供学生转录和解题按区域回填。"
    if not hierarchy.get("canonicalized"):
        transcription += "\n兼容旧目录题号：Q" + hierarchy["leaf_ids"][0]
    return locator, transcription


_text_ready_tasks: set[asyncio.Task] = set()


def _notify_text_ready(callback, transcription):
    """Fire a derived-data observer without joining it to template readiness."""
    if callback is None:
        return
    try:
        import inspect
        result = callback(copy.deepcopy(transcription))
        if inspect.isawaitable(result):
            task = asyncio.ensure_future(result)
            _text_ready_tasks.add(task)

            def done(completed):
                _text_ready_tasks.discard(completed)
                if not completed.cancelled() and completed.exception():
                    logger.warning("template text observer failed: %s", type(completed.exception()).__name__)
            task.add_done_callback(done)
    except Exception as exc:
        logger.warning("template text observer failed: %s", type(exc).__name__)


async def recognize_directory_template(image_path: str, *, template_mode: str, layout_result: dict | None, on_text_ready=None) -> dict:
    """Run Flash hierarchy/coarse hints, then parallel Max locate/solve and Flash transcription."""
    if template_mode not in {"blank", "marked"}:
        return {"error": "模板类型无效，只能选择空白模板或有痕迹模板", "questions": []}
    if layout_result is None:
        return {"error": "模板必须先完成矫正和分栏，再进入完整页面识别", "questions": []}
    stage = current_teaching_context().get("education_stage")
    if stage not in {"primary", "junior", "senior"}:
        return {"error": "模板任务缺少学段，请重新创建模板", "questions": [], "layout_result": layout_result}
    from services.template_recognition import _render_coordinate_grid
    started = asyncio.get_running_loop().time()
    with tempfile.TemporaryDirectory(prefix="mathagent-parallel-template-") as temp:
        with Image.open(image_path) as opened:
            page = opened.convert("RGB")
        width, height = page.size
        # Keep all columns in one ordered request. A single full-width fallback
        # remains valid when the layout review has collapsed to one column.
        raw_columns = ((layout_result.get("column_layout") or {}).get("columns")
                       if isinstance(layout_result, dict) else None) or [{"column_index": 1, "x1_1000": 0, "x2_1000": 1000}]
        if not isinstance(raw_columns, list) or not 1 <= len(raw_columns) <= 12:
            return {"error": "模板分栏数量必须在 1 到 12 栏之间", "questions": [], "layout_result": layout_result}
        # Validate the declared page space before any model is billed. PIL crop
        # otherwise silently pads out-of-bounds rectangles with black pixels.
        previous_right = 0.0
        for column in raw_columns:
            if not isinstance(column, dict):
                return {"error": "模板分栏坐标无效", "questions": [], "layout_result": layout_result}
            left, right = column.get("x1_1000"), column.get("x2_1000")
            if (any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (left, right))
                    or not 0 <= left < right <= 1000 or left != previous_right):
                return {"error": "模板分栏必须在整页坐标内连续覆盖且不重叠", "questions": [], "layout_result": layout_result}
            previous_right = right
        if previous_right != 1000:
            return {"error": "模板分栏未覆盖整页", "questions": [], "layout_result": layout_result}
        # The locator returns full-page coordinates. Stamp that space ONCE,
        # then crop: re-stamping each crop 0..1000 changed the meaning of x.
        coordinate_page = _render_coordinate_grid(page)
        image_paths, original_paths, columns = [], [], []
        for index, column in enumerate(raw_columns, 1):
            x1 = round(float(column.get("x1_1000", 0)) * width / 1000)
            x2 = round(float(column.get("x2_1000", 1000)) * width / 1000)
            if x2 <= x1:
                return {"error": "模板分栏裁图为空", "questions": [], "layout_result": layout_result}
            crop = page.crop((x1, 0, x2, height))
            original_path = Path(temp) / f"original-column-{index}.png"
            crop.save(original_path, format="PNG")
            original_paths.append(str(original_path))
            path = Path(temp) / f"column-{index}.png"
            coordinate_page.crop((x1, 0, x2, height)).save(path, format="PNG")
            image_paths.append(str(path))
            columns.append({"column_index": int(column.get("column_index", index)), "x1_1000": float(column.get("x1_1000", 0)), "x2_1000": float(column.get("x2_1000", 1000))})
        hierarchy_task = asyncio.create_task(_request(
            original_paths, _HIERARCHY_PROMPT, "template-hierarchy-v1",
            lambda raw: one_three.receive_hierarchy(raw, version=4),
            max_tokens=8000, reasoning_effort="low",
        ), name="template-flash-hierarchy")
        coarse_task = asyncio.create_task(_request(
            image_paths, _COARSE_LOCATOR_PROMPT, "template-coarse-locator-v1",
            lambda raw: _receive_coarse_locator(raw, width=width, height=height),
            max_tokens=_COARSE_LOCATOR_MAX_TOKENS, reasoning_effort="low", pool_route="payg",
        ), name="template-flash-coarse-locator")
        try:
            hierarchy = await hierarchy_task
        except ValueError as exc:
            if not coarse_task.done():
                coarse_task.cancel()
            await asyncio.gather(coarse_task, return_exceptions=True)
            return {"error": f"Flash 题号层级失败：{exc}", "error_info": report_failure(exc, stage="template"),
                    "questions": [], "layout_result": layout_result,
                    "template_ocr_diagnostics": {"strategy": one_three.STRATEGY, "failed": True, "stage": "hierarchy", **_stage_diagnostics()}}
        except asyncio.CancelledError:
            coarse_task.cancel()
            await asyncio.gather(coarse_task, return_exceptions=True)
            raise
        except Exception as exc:
            if not coarse_task.done():
                coarse_task.cancel()
            await asyncio.gather(coarse_task, return_exceptions=True)
            return {"error": f"Flash 题号层级失败：{exc}", "error_info": report_failure(exc, stage="template"),
                    "questions": [], "layout_result": layout_result,
                    "template_ocr_diagnostics": {"strategy": one_three.STRATEGY, "failed": True, "stage": "hierarchy", **_stage_diagnostics()}}
        coarse_failure_info = None
        try:
            coarse = await coarse_task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Coarse boxes are hints only. A failed hint must not discard a
            # valid hierarchy or prevent the authoritative Max locator/solver.
            coarse = None
            coarse_failure_info = describe_failure(
                exc,
                stage="template",
                model="qwen3.8-flash",
                route="payg",
                phase="template-coarse-locator-v1",
            )
            logger.warning("Flash 粗定位失败，继续使用 Max 权威定位: %s", exc)
        if coarse and len(coarse["questions"]) != len(hierarchy["leaf_ids"]):
            exc = ValueError("Flash 粗定位题数与层级目录不一致")
            coarse = None
            coarse_failure_info = describe_failure(
                exc,
                stage="template",
                model="qwen3.8-flash",
                route="payg",
                phase="template-coarse-locator-v1",
            )
            logger.warning("Flash 粗定位题数与层级不一致，继续使用 Max 权威定位")
        coarse_paths = []
        if coarse:
            try:
                coarse_paths = _coarse_crop_paths(image_path, coarse, temp, width=width, height=height)
            except Exception as exc:
                coarse = None
                coarse_failure_info = describe_failure(
                    exc,
                    stage="template",
                    model="qwen3.8-flash",
                    route="payg",
                    phase="template-coarse-locator-v1",
                )
                logger.warning("Flash 粗定位裁图失败，继续使用整页: %s", exc)
        locator_prompt, _ = _parallel_template_prompts(hierarchy, width=width, height=height, columns=columns)
        if coarse:
            locator_prompt += (
                "\n以下是按量 Flash 的非权威粗定位提示，仅用于帮助你快速找到题目；不得照抄其边界，"
                "必须重新核对原图、题号、作答框和作图区，并按本合同返回最终权威坐标：" +
                json.dumps(coarse, ensure_ascii=False, separators=(",", ":"))
            )
        solver_prompt, transcription_prompt = one_three.build_prompts(hierarchy, education_stage=stage)
        transcription_prompt += _PARALLEL_TRANSCRIPTION_NOTE
        if coarse:
            transcription_prompt += (
                "\n下面是按量 Flash 的非权威题目粗定位，仅用于在完整原图中快速跳转；正文仍以印刷原图为准，"
                "不要转录这些框或其中的模型说明：" + json.dumps(coarse, ensure_ascii=False, separators=(",", ":"))
            )
        # The full page is always first and the padded Flash question crops are
        # additional visual evidence for the Max solver and Flash transcription.
        # Max remains the sole owner of final geometry and answer binding.
        detailed_images = [image_path] + coarse_paths
        # Tiles are fixed from the coarse boxes before any Max call; without
        # a plan the single page request stays exactly as it was.
        locator_plan = locator_tiles.plan_tiles(
            one_three.question_contract(hierarchy), coarse, columns, width=width, height=height,
            max_questions=locator_tiles.configured_tile_questions(hierarchy))
        locator_report = []
        locator_call = (locator_tiles.locate_in_tiles(
            _request, page, temp, one_three.question_contract(hierarchy), coarse, locator_plan,
            phase="template-directory-v1", max_tokens=one_three.MAX_TOKENS, report=locator_report,
        ) if locator_plan else _request(
            image_paths, locator_prompt, "template-directory-v1",
            lambda raw: (one_three.max_owned.receive_locator_candidates(raw, one_three.question_contract(hierarchy))
                         if hierarchy.get("question_identity_version") == 4
                         else _validate_one_three_locator(raw, hierarchy)),
            max_tokens=one_three.MAX_TOKENS, reasoning_effort="high",
            expected_ids=tuple(hierarchy["leaf_ids"]),
        ))
        locator_task = asyncio.create_task(locator_call, name="template-qwen-max-locator")
        # One owner per question, fixed before any Max call; one shard keeps
        # the single full-solve request unchanged.
        solver_plan = solver_shards.plan_shards(one_three.question_contract(hierarchy),
                                                solver_shards.configured_shard_count(hierarchy))
        solver_report = []
        solver_call = (solver_shards.solve_in_shards(
            _request, detailed_images, solver_prompt, one_three.question_contract(hierarchy), solver_plan,
            phase=one_three.SOLVE_PHASE, max_tokens=one_three.MAX_TOKENS, report=solver_report,
        ) if len(solver_plan) > 1 else _request(
            detailed_images, solver_prompt, one_three.SOLVE_PHASE,
            lambda raw: (one_three.max_owned.receive_solver_candidates(raw, one_three.question_contract(hierarchy))
                         if hierarchy.get("question_identity_version") == 4
                         else one_three.receive_solver(raw, hierarchy)),
            max_tokens=one_three.MAX_TOKENS, reasoning_effort="high",
            expected_ids=tuple("Q" + number for number in hierarchy["leaf_ids"]),
        ))
        solver_task = asyncio.create_task(solver_call, name="template-qwen-max-fullsolve")
        # Transcription starts with the same immutable hierarchy and coarse
        # evidence instead of waiting for Max geometry. Its result can be
        # cached while Max is still solving; merge_branches later binds it to
        # Max's authoritative question types.
        async def transcribe_and_notify():
            transcription = await _request(
                detailed_images, transcription_prompt, one_three.TRANSCRIBE_PHASE,
                lambda raw: one_three.receive_transcription(raw, hierarchy),
                max_tokens=one_three.FLASH_TOKENS, reasoning_effort="high", pool_route="payg",
                output_adapter=lambda payload: one_three.adapt_transcription_payload(payload, hierarchy),
                expected_ids=tuple("Q" + number for number in hierarchy["leaf_ids"]),
            )
            _notify_text_ready(on_text_ready, transcription)
            return transcription

        transcription_task = asyncio.create_task(transcribe_and_notify(), name="template-flash-text-transcription")
        tasks = (locator_task, solver_task, transcription_task)
        try:
            locator, solver, transcription = await asyncio.gather(*tasks)
            locator, solver = one_three.max_owned.reconcile_slot_candidates(
                locator, solver, one_three.question_contract(hierarchy))
            solver = one_three.merge_branches(locator, solver, transcription)
        except asyncio.CancelledError:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        except Exception as exc:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            return {"error": f"模板定位/解题阶段失败：{exc}", "error_info": report_failure(exc, stage="template"),
                    "questions": [], "layout_result": layout_result,
                    "template_ocr_diagnostics": {"strategy": one_three.STRATEGY, "failed": True, "stage": "locate_transcribe_solve", **_stage_diagnostics()}}
    directory = _directory_from_hierarchy(hierarchy, locator)
    answers_by_id = {row["q_id"]: row for row in solver["answers"]}
    questions = []
    anchors, answer_regions, handwriting_regions = [], [], []
    pending = []
    for leaf in directory["leaves"]:
        answer = answers_by_id[leaf["q_id"]]
        review = bool(directory["needs_review"] or solver.get("directory_issues") or answer["answer_status"] == "needs_review")
        question = merge_directory_leaf_score_layer({**copy.deepcopy(leaf), **answer, "question_no": leaf["question_number"], "directory_node_id": leaf["node_id"],
                    "directory_parent_id": leaf["parent_id"],
                    # The frozen directory leaf remains one printed question;
                    # scoring expands it later from the persisted answer boxes.
                    "blank_count": int(leaf.get("answer_slot_count") or len(leaf.get("regions") or []) or 1), "subquestion_count": 1,
                    "answer_area_count": int(leaf["answer_area_count"]),
                    "drawing_area_count": int(leaf.get("drawing_area_count") or 0),
                    "region_kinds": list(leaf.get("region_kinds") or []),
                    "answer_slots": list(answer.get("answer_slots") or []),
                    "slot_ids": list(leaf.get("slot_ids") or []),
                    "answer_bboxes": [[round(v) for v in box] for box in leaf.get("regions", [])],
                    "directory_version": directory["version"], "directory_metadata": copy.deepcopy(leaf),
                    "binding_source": "flash_hierarchy_qwen_parallel", "answer_status": "needs_review" if review else "ready",
                    "needs_review": review, "directory_issues": copy.deepcopy(directory["needs_review"] + solver.get("directory_issues", [])
                                                                                 + answer.get("answer_review_reasons", []))})
        # Explicit Max extra/refused reports are draft-only blocking reviews;
        # confirmation requires the teacher to delete or keep each position.
        if locator.get("max_flags", {}).get(leaf["question_number"]):
            question["max_flags"] = copy.deepcopy(locator["max_flags"][leaf["question_number"]])
        questions.append(question)
        anchors.append({"question_number": leaf["question_number"], **({"question_number_bbox": [round(v) for v in leaf["question_number_bbox"]]} if leaf.get("question_number_bbox") else {})})
        # A solver-only slot has no trustworthy box. Keep its answer position
        # in the draft, and expose only real boxes as editable candidates.
        physical = [(sid, kind, box) for sid, kind, box in zip(
            leaf["slot_ids"], leaf["region_kinds"], leaf["regions"]) if box]
        answer_regions.append({"question_number": leaf["question_number"],
                               "regions": [[round(v) for v in box] for _, _, box in physical],
                               **({"region_kinds": [kind for _, kind, _ in physical],
                                   "slot_ids": [sid for sid, _, _ in physical]} if physical else {})})
        handwriting = [box for box in leaf.get("regions", []) if box]
        handwriting += [box for box in leaf.get("drawing_regions", []) if box not in handwriting]
        handwriting_regions.append({"question_number": leaf["question_number"],
                                    "regions": [[round(v) for v in box] for box in handwriting]})
        if review:
            pending.append(leaf["question_number"])
    diagnostics = {"strategy": one_three.STRATEGY, "education_stage": stage,
                   "model": "qwen3.8-max", "hierarchy_model": "qwen3.8-flash",
                   "coarse_locator_model": "qwen3.8-flash", "request_count": 3 + len(solver_plan) + (sum(tile["attempts"] for tile in locator_report) or 1),
                   "locator_tiles": len(locator_plan), "locator_tile_report": locator_report,
                   "solver_shards": len(solver_plan), "solver_shard_plan": solver_plan,
                   "solver_shard_report": solver_report,
                   "hierarchy_contract_version": hierarchy.get("question_identity_version"),
                   "slot_source": locator.get("slot_source", "legacy"),
                   "transcription_model": "qwen3.8-flash", "solver_model": "qwen3.8-max",
                   "coarse_locator_tokens": _COARSE_LOCATOR_MAX_TOKENS,
                   "max_locator_tokens": one_three.MAX_TOKENS, "max_transcription_tokens": one_three.FLASH_TOKENS,
                   "max_solver_tokens": one_three.MAX_TOKENS,
                   "max_reasoning_effort": "high", "transcription_reasoning_effort": "high", "solver_reasoning_effort": "high",
                   "transcription_thinking": True, "transcription_route": "payg",
                   "full_page_baseline": True, "separate_transcription_request": True,
                   "hierarchy_thinking": True, "hierarchy_reasoning_effort": "low", "hierarchy_route": "payg",
                   "coarse_locator_thinking": True, "coarse_locator_reasoning_effort": "low", "coarse_locator_route": "payg",
                   "coarse_locator_available": bool(coarse), "coarse_crop_count": len(coarse_paths),
                   "column_count": len(image_paths),
                   "elapsed_s": round(asyncio.get_running_loop().time() - started, 3),
                   "needs_review": bool(pending), "review_reasons": pending,
                   **({"coarse_locator_error_info": coarse_failure_info} if coarse_failure_info else {}),
                   **_stage_diagnostics()}
    return {"questions": questions, "question_directory": directory, "template_mode": template_mode,
            "layout_result": layout_result, "question_anchors": anchors,
            "template_answer_regions": answer_regions, "template_handwriting_regions": handwriting_regions,
            "requires_teacher_confirmation": pending, "template_ocr_diagnostics": diagnostics,
            "anchor_diagnostics": {**diagnostics, "model_reasoning_effort": "high", "needs_review": bool(pending)}}
