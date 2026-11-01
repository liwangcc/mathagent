"""Client-node evidence planning and Vision transcription for answer sheets."""

from __future__ import annotations

import json
import math
from typing import Any

from services.template_anchor_crops import validate_anchor_crop_plan
from services.teaching_context import stage_prompt


_ANSWER_KIND_TO_QUESTION_TYPE = {
    "choice_mark": "choice",
    "choice_handwritten": "choice",
    "fill": "fill",
    "solve": "solve",
}


def build_answer_sheet_acceleration_plan(
    layout_artifacts: dict[str, dict],
    identity_regions: list[dict],
    answer_regions: list[dict],
) -> tuple[dict[str, dict], dict[str, list[dict[str, Any]]]]:
    """Build node crop layouts without opening any template or student image."""

    if not isinstance(layout_artifacts, dict) or not layout_artifacts:
        raise ValueError("answer-sheet layout artifacts are required")

    accelerated: dict[str, dict] = {}
    region_plans: dict[str, list[dict[str, Any]]] = {}
    for page_id, artifact in layout_artifacts.items():
        if not isinstance(page_id, str) or not page_id or not isinstance(artifact, dict):
            raise ValueError("answer-sheet layout artifact is invalid")

        plan: list[dict[str, Any]] = []
        crop_regions: list[dict[str, Any]] = []
        page_identity = [
            item for item in identity_regions
            if isinstance(item, dict) and item.get("page_id") == page_id
        ]
        page_answers = [
            item for item in answer_regions
            if isinstance(item, dict) and item.get("page_id") == page_id
        ]

        for index, region in enumerate(page_identity, 1):
            key = f"I{index:03d}"
            descriptor = {
                "question_number": key,
                "kind": "identity",
                "region_id": str(region.get("region_id") or ""),
                "identity_type": str(region.get("identity_type") or ""),
            }
            plan.append(descriptor)
            crop_regions.append({
                "question_number": key,
                "question_type": "solve",
                "answer_bbox": list(region.get("bbox") or []),
                "answer_source_required": False,
            })

        for index, region in enumerate(page_answers, 1):
            key = f"A{index:03d}"
            region_type = str(region.get("region_type") or "")
            question_type = _ANSWER_KIND_TO_QUESTION_TYPE.get(region_type)
            if question_type is None:
                raise ValueError("answer-sheet region type is unsupported")
            descriptor = {
                "question_number": key,
                "kind": "answer",
                "region_id": str(region.get("region_id") or ""),
                "question_no": str(region.get("question_no") or ""),
                "region_type": region_type,
                "question_type": question_type,
                "choice_mode": (
                    "multiple" if bool(region.get("allow_multiple")) else "single"
                ),
                "choice_options": [
                    str(value).strip().upper()
                    for value in (region.get("choice_options") or ["A", "B", "C", "D"])
                    if str(value).strip()
                ],
            }
            plan.append(descriptor)
            crop_regions.append({
                "question_number": key,
                "question_type": question_type,
                "answer_bbox": list(region.get("bbox") or []),
            })

        if not plan:
            raise ValueError(f"answer-sheet page {page_id} has no frozen regions")

        raw_question_crop_plan = artifact.get("question_crop_plan")
        if not isinstance(raw_question_crop_plan, dict):
            raise ValueError(
                f"answer-sheet page {page_id} is missing question_crop_plan"
            )
        try:
            question_crop_plan = validate_anchor_crop_plan(
                raw_question_crop_plan,
                artifact_sha256=str(artifact.get("sha256") or ""),
            )
        except ValueError as exc:
            raise ValueError(
                f"answer-sheet page {page_id} has invalid question_crop_plan"
            ) from exc

        blocks = question_crop_plan["blocks"]
        for descriptor, crop_region in zip(plan, crop_regions, strict=True):
            left, top, right, bottom = crop_region["answer_bbox"]
            containing = [
                (position, block["bbox_1000"])
                for position, block in enumerate(blocks, 1)
                if block["bbox_1000"][0] <= left
                and block["bbox_1000"][1] <= top
                and right <= block["bbox_1000"][2]
                and bottom <= block["bbox_1000"][3]
            ]
            if len(containing) != 1:
                # Focus-sheet v3 remaps every region after the worker artifact
                # is validated. Keep a legacy column location only when one is
                # available so historical v2 artifacts remain readable.
                continue
            image_index, (block_left, block_top, block_right, block_bottom) = (
                containing[0]
            )
            block_width = block_right - block_left
            block_height = block_bottom - block_top
            descriptor["image_index"] = image_index
            descriptor["answer_bbox_1000"] = [
                round((left - block_left) * 1000 / block_width),
                round((top - block_top) * 1000 / block_height),
                round((right - block_left) * 1000 / block_width),
                round((bottom - block_top) * 1000 / block_height),
            ]

        accelerated_artifact = dict(artifact)
        accelerated_artifact["answer_regions"] = crop_regions
        # The Vision model remains the blank/unreadable authority. Disabling
        # pixel subtraction also avoids treating printed identity fields as ink.
        accelerated_artifact["blank_detection_enabled"] = False
        accelerated_artifact["question_crop_plan"] = question_crop_plan
        accelerated[page_id] = accelerated_artifact
        region_plans[page_id] = plan

    return accelerated, region_plans


def bind_answer_sheet_evidence_locations(
    region_plan: list[dict[str, Any]],
    evidence_diagnostics: dict[str, Any],
    evidence_count: int,
) -> list[dict[str, Any]]:
    """Bind validated artifact locations to the frozen answer-sheet schema."""

    if (
        not isinstance(region_plan, list)
        or not region_plan
        or not isinstance(evidence_diagnostics, dict)
        or isinstance(evidence_count, bool)
        or not isinstance(evidence_count, int)
        or not 1 <= evidence_count <= 12
    ):
        raise ValueError("answer-sheet evidence location contract is invalid")

    expected = [str(item.get("question_number") or "") for item in region_plan]
    if any(not value for value in expected) or len(set(expected)) != len(expected):
        raise ValueError("answer-sheet evidence region plan is invalid")

    ownership = str(evidence_diagnostics.get("ownership") or "")
    if ownership == "confirmed_bbox_focus_sheets":
        locations = evidence_diagnostics.get("evidence_locations")
        if not isinstance(locations, list) or [
            str(item.get("question_number") or "")
            for item in locations
            if isinstance(item, dict)
        ] != expected or len(locations) != len(expected):
            raise ValueError("answer-sheet focus evidence order is invalid")
        result = []
        for descriptor, location in zip(region_plan, locations, strict=True):
            if not isinstance(location, dict):
                raise ValueError("answer-sheet focus evidence location is invalid")
            image_index = location.get("image_index")
            bbox = location.get("answer_bbox_1000")
            if (
                isinstance(image_index, bool)
                or not isinstance(image_index, int)
                or not 1 <= image_index <= evidence_count
                or not isinstance(bbox, list)
                or len(bbox) != 4
                or any(
                    isinstance(value, bool) or not isinstance(value, int)
                    for value in bbox
                )
                or not (0 <= bbox[0] < bbox[2] <= 1000)
                or not (0 <= bbox[1] < bbox[3] <= 1000)
            ):
                raise ValueError("answer-sheet focus evidence location is invalid")
            result.append({
                **descriptor,
                "image_index": image_index,
                "answer_bbox_1000": list(bbox),
                "evidence_format": "focus_sheet",
            })
        return result

    result = []
    for descriptor in region_plan:
        image_index = descriptor.get("image_index")
        bbox = descriptor.get("answer_bbox_1000")
        if (
            isinstance(image_index, bool)
            or not isinstance(image_index, int)
            or not 1 <= image_index <= evidence_count
            or not isinstance(bbox, list)
            or len(bbox) != 4
        ):
            raise ValueError("answer-sheet legacy evidence location is unavailable")
        result.append({**descriptor, "evidence_format": "full_height_column"})
    return result


async def transcribe_answer_sheet_evidence(
    evidence_paths: str | list[str] | tuple[str, ...],
    region_plan: list[dict[str, Any]],
    *,
    teacher_id: int | str,
) -> list[dict[str, Any]]:
    """Use the configured Vision API on one or more node-built evidence images."""

    if isinstance(evidence_paths, str):
        paths = [evidence_paths]
    elif isinstance(evidence_paths, (list, tuple)):
        paths = list(evidence_paths)
    else:
        paths = []
    if (
        not paths
        or any(not isinstance(path, str) or not path for path in paths)
        or len(set(paths)) != len(paths)
        or not isinstance(region_plan, list)
        or not region_plan
    ):
        raise ValueError("answer-sheet evidence and region plan are required")
    expected = [str(item.get("question_number") or "") for item in region_plan]
    if any(not value for value in expected) or len(set(expected)) != len(expected):
        raise ValueError("answer-sheet evidence region plan is invalid")
    for item in region_plan:
        image_index = item.get("image_index")
        bbox = item.get("answer_bbox_1000")
        if (
            isinstance(image_index, bool)
            or not isinstance(image_index, int)
            or not 1 <= image_index <= len(paths)
            or not isinstance(bbox, list)
            or len(bbox) != 4
            or any(isinstance(value, bool) or not isinstance(value, int) for value in bbox)
            or not (0 <= bbox[0] < bbox[2] <= 1000)
            or not (0 <= bbox[1] < bbox[3] <= 1000)
        ):
            raise ValueError("answer-sheet evidence location plan is invalid")

    schema = []
    for item in region_plan:
        location = {
            "image_index": item["image_index"],
            "answer_bbox_1000": item["answer_bbox_1000"],
        }
        if item.get("kind") == "identity":
            schema.append({
                "question_number": item["question_number"],
                "kind": "identity",
                "identity_type": item.get("identity_type"),
                **location,
            })
        else:
            schema.append({
                "question_number": item["question_number"],
                "kind": "answer",
                "question_no": item.get("question_no"),
                "question_type": item.get("question_type"),
                "choice_mode": item.get("choice_mode"),
                "choice_options": item.get("choice_options"),
                **location,
            })

    prompt = stage_prompt("""你是中国高中数学答题区域 OCR。
region_schema 已给出题号、题型和坐标。只读取对应区域内学生实际写下的内容。
不解题、不判分、不纠正、不补全；数学符号按图原样抄写。
空白返回 blank 和空字符串，看不清返回 unreadable 和 [无法识别]。
严格按 question_numbers 顺序返回，每个题号只返回一次。

严格只返回 JSON，不要 Markdown 或其他文字：
{"questions":[{"question_number":"I001","student_answer":"","answer_state":"blank|visible|unreadable","confidence":0.0}]}

""") + "\nquestion_numbers=" + json.dumps(expected, ensure_ascii=False, separators=(",", ":")) + """
region_schema=""" + json.dumps(
        schema,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    from config import OCR_PER_TEACHER_LIMIT
    from services.analyzer import parse_llm_json
    from services.key_pool import get_ocr_teacher_semaphore
    from services.ocr_service import (
        call_vision,
        call_vision_images,
        reset_vision_teacher_id,
        set_vision_teacher_id,
        validate_visual_transcriptions,
    )

    owner = str(teacher_id or "")
    teacher_token = set_vision_teacher_id(owner)
    try:
        semaphore = await get_ocr_teacher_semaphore(owner, OCR_PER_TEACHER_LIMIT)
        max_tokens = max(512, min(8192, len(region_plan) * 96))
        async with semaphore:
            if len(paths) == 1:
                raw = await call_vision(paths[0], prompt, max_tokens=max_tokens)
            else:
                raw = await call_vision_images(paths, prompt, max_tokens=max_tokens)
        parsed = parse_llm_json(raw)
        transcriptions = validate_visual_transcriptions(parsed.get("questions"))
    finally:
        reset_vision_teacher_id(teacher_token)

    actual = [item["question_number"] for item in transcriptions]
    if actual != expected:
        raise ValueError("answer-sheet evidence region order is invalid")

    result: list[dict[str, Any]] = []
    for descriptor, transcription in zip(region_plan, transcriptions):
        raw_confidence = transcription.get("confidence", 0.0)
        try:
            confidence = float(raw_confidence)
        except (TypeError, ValueError) as exc:
            raise ValueError("answer-sheet evidence confidence is invalid") from exc
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError("answer-sheet evidence confidence is invalid")
        result.append({
            **descriptor,
            "text": str(transcription.get("student_answer") or "").strip(),
            "state": transcription["answer_state"],
            "confidence": confidence,
        })
    return result
