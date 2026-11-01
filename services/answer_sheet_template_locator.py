"""Qwen-backed rough localization for reusable blank answer sheets.

The model only proposes geometry.  Every proposed region remains unconfirmed
until a teacher reviews it in the answer-sheet editor.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import tempfile
from typing import Any

from services.analyzer import parse_llm_json
from services.ocr_service import (
    call_gpt_vision,
    reset_vision_teacher_id,
    set_vision_teacher_id,
)


LOCATOR_CONTRACT_VERSION = "answer-sheet-template-locator-v1"
LOCATOR_ROUTE = "qwen3.8max"
LOCATOR_REASONING_EFFORT = "high"
_IDENTITY_TYPES = {
    "name",
    "student_no",
    "exam_no_text",
    "qr",
    "barcode",
    "exam_no_marks",
    "page_number",
}


def _bounded_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}必须是数值")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label}必须是有限数值")
    rounded = round(number)
    if abs(number - rounded) > 0.001 or not 0 <= rounded <= 1000:
        raise ValueError(f"{label}必须是 0..1000 的整数")
    return int(rounded)


def _bbox(value: object, label: str) -> list[int]:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError(f"{label}坐标必须是四个整数")
    left, top, right, bottom = [
        _bounded_int(item, label) for item in value
    ]
    if not (left < right and top < bottom):
        raise ValueError(f"{label}坐标面积无效")
    return [left, top, right, bottom]


def _normalize_columns(payload: dict[str, Any]) -> dict[str, Any]:
    raw_columns = payload.get("columns")
    count = payload.get("column_count")
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or not 1 <= count <= 3
        or not isinstance(raw_columns, list)
        or len(raw_columns) != count
    ):
        raise ValueError("答题卡分栏必须包含一至三栏")
    columns: list[dict[str, int]] = []
    previous_right = 0
    for position, raw in enumerate(raw_columns, 1):
        if not isinstance(raw, dict):
            raise ValueError("答题卡分栏格式错误")
        index = raw.get("column_index")
        left = _bounded_int(raw.get("x1_1000"), "分栏")
        right = _bounded_int(raw.get("x2_1000"), "分栏")
        if index != position or left != previous_right or right <= left:
            raise ValueError("答题卡分栏必须从左到右连续覆盖整页")
        columns.append({
            "column_index": position,
            "x1_1000": left,
            "x2_1000": right,
        })
        previous_right = right
    if previous_right != 1000:
        raise ValueError("答题卡分栏必须完整覆盖整页")
    return {
        "column_count": count,
        "columns": columns,
    }


def validate_answer_sheet_locator_response(
    content: str,
    *,
    expected_numbers: list[str],
) -> dict[str, Any]:
    """Validate one page's rough localization without trusting model fields."""

    payload = parse_llm_json(content)
    if not isinstance(payload, dict):
        raise ValueError("答题卡粗定位必须返回 JSON 对象")
    coordinate_space = str(payload.get("coordinate_space") or "")
    if coordinate_space != "normalized_1000_v1":
        raise ValueError("答题卡粗定位坐标系无效")

    allowed_questions = set(expected_numbers)
    answer_regions: list[dict[str, Any]] = []
    seen_questions: set[str] = set()
    raw_answers = payload.get("answer_regions")
    if not isinstance(raw_answers, list):
        raise ValueError("答题卡粗定位缺少答案区域")
    for index, raw in enumerate(raw_answers, 1):
        if not isinstance(raw, dict):
            raise ValueError("答案区域格式错误")
        question_no = str(raw.get("question_no") or "").strip()
        if question_no not in allowed_questions:
            raise ValueError("答题卡粗定位返回了候选范围外的题号")
        if question_no in seen_questions:
            raise ValueError("同一页出现重复题号区域")
        seen_questions.add(question_no)
        answer_regions.append({
            "question_no": question_no,
            "bbox": _bbox(raw.get("bbox"), f"第{question_no}题"),
            "confidence": _confidence(raw.get("confidence")),
            "reading_order": index,
        })

    identity_regions: list[dict[str, Any]] = []
    seen_identity: set[str] = set()
    raw_identity = payload.get("identity_regions") or []
    if not isinstance(raw_identity, list):
        raise ValueError("身份区域格式错误")
    for raw in raw_identity:
        if not isinstance(raw, dict):
            raise ValueError("身份区域格式错误")
        identity_type = str(raw.get("identity_type") or "").strip()
        if identity_type not in _IDENTITY_TYPES:
            raise ValueError("身份区域类型无效")
        if identity_type in seen_identity:
            continue
        seen_identity.add(identity_type)
        config = {}
        if identity_type == "exam_no_marks":
            digits = raw.get("digits")
            if isinstance(digits, bool) or not isinstance(digits, int) or not 1 <= digits <= 32:
                raise ValueError("准考证号涂格位数无效")
            config["digits"] = digits
        identity_regions.append({
            "identity_type": identity_type,
            "bbox": _bbox(raw.get("bbox"), "身份区域"),
            "confidence": _confidence(raw.get("confidence")),
            "config": config,
        })

    return {
        "contract_version": LOCATOR_CONTRACT_VERSION,
        "coordinate_space": coordinate_space,
        "column_layout": _normalize_columns(payload),
        "answer_regions": answer_regions,
        "identity_regions": identity_regions,
    }


def _confidence(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("粗定位置信度无效")
    confidence = float(value)
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("粗定位置信度无效")
    return round(confidence, 4)


def _prompt(
    expected_questions: list[dict[str, str]],
    page_number: int,
    page_count: int,
    *,
    image_size: tuple[int, int] | None = None,
) -> str:
    candidates = [
        {
            "question_no": item["question_no"],
            "question_type": item["question_type"],
        }
        for item in expected_questions
    ]
    candidates_json = json.dumps(candidates, ensure_ascii=False, separators=(",", ":"))
    prompt = f"""你是空白答题卡模板的版面粗定位模型。输入是已经拉正的第 {page_number}/{page_count} 页空白答题卡。
只定位当前页实际印刷的作答区、身份区和纵向分栏。

全卷候选题号与题型：{candidates_json}

规则：
1. answer_regions 只返回当前页实际出现的顶层题号，每题最多一个框。选择题按实际版式定位：涂卡式框住该题全部选项涂卡格（如 [A][B][C][D]），填写式框住该题完整作答括号及内部书写空间；不要把选项涂卡格当成填写括号。填空题框住学生书写横线及其上方书写区；解答题框住该题完整书写框，不要只框题号。
2. identity_regions 只允许 name、student_no、exam_no_text、qr、barcode、exam_no_marks、page_number。没有的类型不要返回。exam_no_marks 还要返回 digits。
3. column_count 只能为 1、2、3。columns 从左到右连续、无空隙、无重叠并覆盖 0..1000；跨栏标题不单独算一栏。
4. 所有 bbox 和分栏坐标均为整页 normalized_1000_v1 整数坐标，左上角 (0,0)，右下角 (1000,1000)。
5. 这是供教师复核的粗定位。看不清的题不要编造；可以遗漏，不能返回候选列表之外的题号。

只返回严格 JSON，不要 Markdown 或解释，格式：
{{"coordinate_space":"normalized_1000_v1","column_count":2,"columns":[{{"column_index":1,"x1_1000":0,"x2_1000":500}},{{"column_index":2,"x1_1000":500,"x2_1000":1000}}],"answer_regions":[{{"question_no":"1","bbox":[60,400,470,520],"confidence":0.92}}],"identity_regions":[{{"identity_type":"name","bbox":[60,80,350,150],"confidence":0.95}}]}}"""
    if image_size is not None:
        from services.template_recognition import _coordinate_grid_instruction

        prompt += (
            f"\n本次输入图像实际像素尺寸：宽={image_size[0]}，高={image_size[1]}。"
            "横纵坐标分别按 x1000=round(x像素/图片宽度*1000)、"
            "y1000=round(y像素/图片高度*1000) 换算；不要使用同一个边长作分母。"
            "图像底边始终为 y=1000，中间横线为 y=500，不能将图像宽高比再次乘入坐标。"
            f"\n{_coordinate_grid_instruction('整页坐标定位')}"
        )
    return prompt


def _prepare_locator_image(image_path: str, output_path: str) -> tuple[int, int]:
    """Add the existing coordinate ruler without resizing the teacher's image."""
    from PIL import Image
    from services.template_recognition import _render_coordinate_grid

    with Image.open(image_path) as source:
        size = source.size
        grid = _render_coordinate_grid(source)
    try:
        if grid.size != size:
            raise ValueError("定位辅助图尺寸与模板不一致")
        grid.save(output_path, format="PNG")
    finally:
        grid.close()
    return size


async def locate_answer_sheet_template(
    pages: list[dict[str, Any]],
    question_specs: list[dict[str, Any]],
    *,
    teacher_id: int | str,
) -> dict[str, Any]:
    """Run qwen3.8max/high once per canonical page and merge rough proposals."""

    if not pages:
        raise ValueError("请先上传空白答题卡")
    expected_questions = [
        {
            "question_no": str(item.get("question_no") or "").strip(),
            "question_type": str(item.get("question_type") or "").strip(),
        }
        for item in question_specs
    ]
    if any(not item["question_no"] for item in expected_questions):
        raise ValueError("题目编号无效")
    expected_numbers = [item["question_no"] for item in expected_questions]

    async def locate_page(page: dict[str, Any], page_number: int) -> dict[str, Any]:
        image_path = str(page.get("working_path") or page.get("path") or "")
        if not image_path or not os.path.isfile(image_path):
            raise ValueError("空白答题卡标准页不存在")
        validated: dict[str, Any] | None = None

        def response_validator(content: str) -> None:
            nonlocal validated
            validated = validate_answer_sheet_locator_response(
                content,
                expected_numbers=expected_numbers,
            )

        with tempfile.TemporaryDirectory(prefix="mathagent-sheet-locator-") as directory:
            request_path = os.path.join(directory, "page-grid.png")
            # Keep the temporary image alive until the provider has consumed it.
            image_size = _prepare_locator_image(image_path, request_path)
            prompt = _prompt(
                expected_questions, page_number, len(pages), image_size=image_size,
            )
            content = await call_gpt_vision(
                request_path,
                prompt,
                max_tokens=4096,
                pool_route="fallback",
                reasoning_effort=LOCATOR_REASONING_EFFORT,
                response_validator=response_validator,
                prompt_first=True,
            )
        if validated is None:
            validated = validate_answer_sheet_locator_response(
                content,
                expected_numbers=expected_numbers,
            )
        return {
            "page_id": str(page.get("page_id") or f"page_{page_number}"),
            "page_index": page_number - 1,
            **validated,
        }

    teacher_token = set_vision_teacher_id(str(teacher_id))
    tasks = [
        asyncio.create_task(locate_page(page, index))
        for index, page in enumerate(pages, 1)
    ]
    try:
        results = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    finally:
        reset_vision_teacher_id(teacher_token)

    seen: set[str] = set()
    for page in results:
        for region in page["answer_regions"]:
            question_no = region["question_no"]
            if question_no in seen:
                raise ValueError(f"粗定位在多页重复返回第{question_no}题")
            seen.add(question_no)
    return {
        "contract_version": LOCATOR_CONTRACT_VERSION,
        "provider_route": LOCATOR_ROUTE,
        "reasoning_effort": LOCATOR_REASONING_EFFORT,
        "pages": results,
        "located_questions": [number for number in expected_numbers if number in seen],
        "missing_questions": [number for number in expected_numbers if number not in seen],
    }
