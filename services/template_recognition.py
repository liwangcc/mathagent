"""模板识别服务 - services/template_recognition.py

功能：
1. 普通模板导入委托 template_directory_pipeline（Flash 层级/转录 + Max 定位/解题）
2. 答题卡源卷：qwen3.8-flash 以官方关闭思考组合忠实转录，qwen3.8-max（Token Plan）生成标准答案
3. 模板定位共用的临时坐标网格
"""

import json
import logging
import os
import re
from typing import List, Dict

from services.teaching_context import stage_prompt
from services.ocr_service import call_template_max_text, call_vision, call_vision_images
from services.analyzer import parse_llm_json
from services.grading_contract import normalize_choice_answer
from config import OCR_TEMPLATE_GPT_MAX_OUTPUT_TOKENS


logger = logging.getLogger(__name__)

_TEMPLATE_OCR_CONTRACT_ATTEMPTS = 2
_TEMPLATE_GPT_MAX_OUTPUT_TOKENS = OCR_TEMPLATE_GPT_MAX_OUTPUT_TOKENS
_COORDINATE_GRID_STEP_1000 = 100
_COORDINATE_GRID_ALPHA = 72


def _grid_font(width: int, height: int):
    """Load a readable, optional font for temporary coordinate labels."""
    from PIL import ImageFont

    font_size = max(12, min(28, int(round(min(width, height) / 220))))
    candidates = (
        r"C:\Windows\Fonts\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    for candidate in candidates:
        try:
            if os.path.isfile(candidate):
                return ImageFont.truetype(candidate, font_size)
        except (OSError, ValueError):
            continue
    return ImageFont.load_default()


def _render_coordinate_grid(image):
    """Return a copy with a light local normalized-1000 coordinate grid."""
    from PIL import Image, ImageDraw

    if image is None or not hasattr(image, "convert"):
        raise ValueError("coordinate-grid source image is invalid")
    canvas = image.convert("RGBA")
    width, height = canvas.size
    if width < 1 or height < 1:
        raise ValueError("coordinate-grid source image is empty")

    # Draw onto a transparent overlay and composite it. Drawing semi-transparent
    # fills directly onto an RGBA image would replace the source alpha and turn
    # the grid into opaque red pixels after conversion to RGB.
    overlay = canvas.copy()
    overlay.putalpha(0)
    draw = ImageDraw.Draw(overlay, "RGBA")
    font = _grid_font(width, height)
    line_width = max(1, int(round(min(width, height) / 1800)))
    line_color = (220, 38, 38, _COORDINATE_GRID_ALPHA)
    label_color = (150, 0, 0, 220)
    label_background = (255, 255, 255, 180)

    def draw_label(text: str, x: int, y: int) -> None:
        try:
            left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        except (AttributeError, TypeError):
            left, top, right, bottom = 0, 0, len(text) * 7, 12
        label_width = right - left
        label_height = bottom - top
        x = max(0, min(int(x), width - label_width - 2))
        y = max(0, min(int(y), height - label_height - 2))
        draw.rectangle(
            (x, y, x + label_width + 2, y + label_height + 2),
            fill=label_background,
        )
        draw.text((x + 1, y + 1), text, font=font, fill=label_color)

    for tick in range(0, 1001, _COORDINATE_GRID_STEP_1000):
        x = round(tick * width / 1000)
        y = round(tick * height / 1000)
        draw.line((x, 0, x, height - 1), fill=line_color, width=line_width)
        draw.line((0, y, width - 1, y), fill=line_color, width=line_width)
        draw_label(f"x={tick:03d}", x + 2, 2)
        draw_label(f"y={tick:03d}", 2, y + 2)
    return Image.alpha_composite(canvas, overlay).convert("RGB")


def _coordinate_grid_instruction(scope: str) -> str:
    """Describe the debug-only grid without changing the coordinate contract."""
    return (
        f"输入图叠加了半透明红色坐标网格，网格刻度每 {_COORDINATE_GRID_STEP_1000} "
        f"个 normalized_1000_v1 单位；网格仅辅助 {scope}，不要把红线或刻度文字当成手写。"
    )


class _TemplateReviewError(ValueError):
    """A semantic identity/order conflict that must reach teacher review.

    The scheduler treats ``failure_category="semantic"`` as non-eligible for a
    paid Qwen replacement: a model that answered for the wrong column/image is
    not fixed by asking another model the same question.
    """

    failure_category = "semantic"


def _template_anchor_review_attr(
    exc: BaseException | None,
    name: str,
    default: object = None,
) -> object:
    """Read review metadata through an ``OcrPoolError`` cause chain."""
    current = exc
    seen: set[int] = set()
    while isinstance(current, BaseException) and id(current) not in seen:
        seen.add(id(current))
        value = getattr(current, name, None)
        if value is not None:
            return value
        current = current.__cause__ or current.__context__
    return default


def _is_template_review_failure(exc: BaseException | None) -> bool:
    """Whether a failure is a semantic review condition rather than an outage.

    The production scheduler re-wraps validator failures as
    ``OcrPoolError(failure_category="semantic")``; injected adapters surface
    the review error directly.  Both must keep the review classification so
    the caller marks ``needs_review`` instead of a generic provider failure.
    """
    if exc is None:
        return False
    if isinstance(exc, _TemplateReviewError):
        return True
    category = str(getattr(exc, "failure_category", "") or "").strip().lower()
    return category == "semantic"


# ── 提示词定义 ──

TEMPLATE_WITH_ANSWER_PROMPT = """你是模板注册阶段的题目转录模型。
输入是一张已经完成矫正的中国高中数学试卷完整页面；该页的分栏规则已经确认，输入可能是按该规则排列的连续栏图，合起来仍是一整页。只读取模板中的印刷题干和印刷标准答案，不读取学生手写内容。

要求：
1. 题目：完整题干（必须包含已知条件）
2. 答案：印刷的标准答案（非手写）
3. 题型 question_type，只能三选一：
   - choice：题干中实际给出了 A/B/C/D 选项的选择题；同时返回 choice_mode：single（单选）、multiple（多选）或 unknown（相关说明缺失、模糊或矛盾）
   - fill：直接填写数值、式子、集合、区间等最终结果的填空题
   - solve：需要书写解题过程的解答题/大题
   对 choice：如果印刷答案区给的是数值、公式、集合或区间，必须先与 A/B/C/D 四个选项逐一比对，再输出匹配的选项字母。例如 A.3、B.1、C.-3、D.-1 且印刷答案为 -3 时，standard_answer 必须输出 C。
   不要因为 choice 的印刷答案是数值、公式或集合就改成 fill；是否为 choice 由题干是否实际有 A/B/C/D 选项决定。
4. 数学符号用标准写法（⊥ ∥ ∩ ∪ ∵ ∝ √ ∫ ∑ 等）
5. 忠实照抄原题，不要化简或改写
6. 数值必须逐位核对
7. 必须主动判断每道 choice 的 choice_mode，不要把 unknown 当默认值：
   - 先通读本页可见的大题标题、分区说明和题干；分区说明适用于该分区内后续连续题目，直到出现新的分区标题或相反规则。
   - 出现“单项选择题”“只有一项符合题目要求”“有且只有一个正确选项”等规则时返回 single。
   - 出现“多项选择题”“有一项或多项符合题目要求”“有多个正确选项”“不定项选择题”“全部选对得分，选对但不全得部分分”等规则时返回 multiple。
   - 仅当相关说明未拍入、文字模糊/遮挡或规则互相矛盾，无法从原图确定时返回 unknown，并保留可见答案交教师确认。
   - 禁止根据答案字母数量（包括 standard_answer 的字母数量）反推 choice_mode。single 的 standard_answer 恰为一个字母；multiple 为 2–4 个不重复字母。

返回严格 JSON 格式：
{
  "questions": [
    {
      "question_number": "1",
      "question_text": "完整题目（含所有已知条件+问题）",
      "question_type": "choice|fill|solve",
      "choice_mode": "single|multiple|unknown",
      "standard_answer": "印刷的标准答案"
    }
  ]
}

重要：
- 必须识别图片中的每一道题，每个题号恰好出现一次，不得遗漏或重复
- 每道题的 question_text 必须包含已知条件和问题
- standard_answer 只写最终答案，不含解题过程
- 多小问用分号分隔，如 "cosα=-2√6/5; (23-4√6)/49"
"""

TEMPLATE_BLANK_PROMPT = """你是模板注册阶段的题目转录模型。
输入是一张已经完成矫正的中国高中数学试卷完整页面；该页的分栏规则已经确认，输入可能是按该规则排列的连续栏图，合起来仍是一整页。模板可能空白，也可能含有手写答案、演算、批注、红笔或涂改；这些都不是标准答案，全部忽略。只忠实转录印刷题目，不要转录印刷答案、答案栏、解析、分数或批改信息。
不要解题或生成标准答案。

数学口径：本任务范围限定为中国高中数学。严格按照中国高中数学教材、课程标准和高中考试命题语境作答，
不得套用大学数学、形式逻辑或超出高中课程的特殊解释。

要求：
1. 完整识别每道题的题干（必须包含已知条件）
2. 题型 question_type 只能三选一；choice 还必须返回 choice_mode=single|multiple|unknown。必须先通读本页可见的大题标题、分区说明和题干，主动判断该分区内每道连续选择题的作答规则：
   - “单项选择题”“只有一项符合题目要求”“有且只有一个正确选项”判为 single；
   - “多项选择题”“有一项或多项符合题目要求”“有多个正确选项”“不定项选择题”或“全部选对得分，选对但不全得部分分”判为 multiple；
   - 分区说明持续适用于后续连续题目，直到新标题或相反规则出现；只有说明未拍入、模糊/遮挡或互相矛盾时才返回 unknown；
   - 不能根据候选答案字母数量猜测；禁止根据答案字母数量反推 choice_mode。
3. choice 的 question_text 必须完整保留 A/B/C/D 四个选项及其选项内容，不能只保留题干或遗漏某个选项
4. 数学符号用标准写法（⊥ ∥ ∩ ∪ ∵ ∝ √ ∫ ∑ 等）
5. 忠实照抄原题，不要化简或改写
6. question_text 只允许忠实照抄图片，不得因为求解结果而纠错、补全、化简或改写题干；standard_answer 和 solution_steps 才是求解字段。
7. 不要根据题目内容推导、补写或猜测任何标准答案、解题过程或学生答案；
   这些字段由后续独立推理阶段生成，视觉转录结果中的答案字段一律不作为权威依据。
返回严格 JSON 格式：
{
  "questions": [
    {
      "question_number": "1",
      "question_text": "完整题目（含所有已知条件+问题，不含答案栏或解析）",
      "question_type": "choice|fill|solve",
      "choice_mode": "single|multiple|unknown"
    }
  ]
}

重要：
- 必须识别图片中的每一道题，不得遗漏
- question_text 必须包含已知条件和问题
- choice 必须完整保留 A/B/C/D 及其选项内容；choice_mode 只依据题目分区说明和题干判断，无法确定时返回 unknown
- 只忠实转录题目，不要解题或生成标准答案，不得把答案或推导写回 question_text
"""


TEMPLATE_CHOICE_REPAIR_PROMPT = """这是模板原图。请只复核以下题目的题型和印刷标准答案；候选结果仅供定位，不是图片依据：

{suspects}

请直接查看原始图片，只复核上面列出的题目。不要根据上一轮的题型猜测。

硬规则：
1. 若原题实际有 A/B/C/D 选项，question_type 必须是 choice。先通读原图中的大题标题、分区说明和题干，主动判断 choice_mode：
   - “单项选择题”“只有一项符合题目要求”“有且只有一个正确选项”返回 single；
   - “多项选择题”“有一项或多项符合题目要求”“有多个正确选项”“不定项选择题”或部分得分规则返回 multiple；
   - 清晰的分区说明适用于该分区内后续连续题目，直到新标题或相反规则出现；仅在说明缺失、模糊/遮挡或矛盾时返回 unknown。
2. 若印刷答案区是数值、公式、集合或区间，先逐一比较 A/B/C/D 选项，返回匹配的字母；例如 A.3、B.1、C.-3、D.-1 且答案为 -3，必须返回 C。
3. 只有原题没有 A/B/C/D 选项时，才能按作答结构改为 fill 或 solve；不要因为答案值像数值/公式就把选择题改成填空。
4. 忠实读取原图，答案必须逐位核对；禁止根据答案字母数量反推 choice_mode，不确定时不要编造。

严格返回 JSON，不要其他文字：
{{
  "questions": [
    {{
      "question_number": "原题号",
      "question_type": "choice|fill|solve",
      "choice_mode": "single|multiple|unknown",
      "standard_answer": "复核后的标准答案"
    }}
  ]
}}
"""


_CHOICE_OPTION_MARKER_RE = re.compile(r"([A-D])\s*[.．、:：]\s*", re.IGNORECASE)


def _normalize_template_text(value: object) -> str:
    """Restore escaped option-row newlines without touching LaTex commands."""
    text = str(value or "")
    for label in "ABCD":
        for punctuation in ".．、:：":
            marker = f"{label}{punctuation}"
            text = text.replace(r"\\n" + marker, "\n" + marker)
            text = text.replace(r"\n" + marker, "\n" + marker)
    return text


def _normalize_template_question_texts(questions: List[Dict]) -> None:
    for question in questions:
        question["question_text"] = _normalize_template_text(
            question.get("question_text") or question.get("content") or ""
        )


def _validate_template_questions(
    questions: object,
    *,
    require_standard_answer: bool,
) -> List[Dict]:
    """Fail closed on malformed or ambiguous template OCR output."""
    if not isinstance(questions, list) or not questions:
        raise ValueError("模板 OCR 未返回有效题目列表")

    validated: List[Dict] = []
    seen_numbers: set[str] = set()
    for index, raw in enumerate(questions, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"模板 OCR 第 {index} 项不是题目对象")
        question = dict(raw)
        number = str(
            question.get("question_number") or question.get("question_no") or ""
        ).strip()
        if not number:
            raise ValueError(f"模板 OCR 第 {index} 项缺少题号")
        if number in seen_numbers:
            raise ValueError(f"模板 OCR 出现重复题号：{number}")
        seen_numbers.add(number)

        text = str(question.get("question_text") or "").strip()
        if not text:
            raise ValueError(f"模板 OCR 第 {number} 题缺少题干")
        question_type = str(question.get("question_type") or "").strip().lower()
        if question_type not in {"choice", "fill", "solve"}:
            raise ValueError(f"模板 OCR 第 {number} 题题型无效")
        if require_standard_answer and not str(
            question.get("standard_answer") or ""
        ).strip():
            raise ValueError(f"模板 OCR 第 {number} 题缺少标准答案")

        question["question_number"] = number
        question["question_type"] = question_type
        # Student answer geometry is deliberately not part of the template
        # OCR contract.  Question-number anchors are attached in a separate
        # pass; template handwriting is already exempted upstream.
        for legacy_key in (
            "answer_bbox",
            "answer_regions",
            "answer_box",
            "student_answer_bbox",
        ):
            question.pop(legacy_key, None)
        choice_mode = str(question.get("choice_mode") or "unknown").strip().lower()
        question["choice_mode"] = (
            choice_mode
            if question_type == "choice" and choice_mode in {"single", "multiple"}
            else "unknown"
        )
        validated.append(question)
    return validated


def _clear_transcription_answers(questions: List[Dict]) -> List[Dict]:
    """Keep template OCR text-only; standard answers come from the math model."""
    for question in questions:
        question.pop("choice_result", None)
        question["standard_answer"] = ""
        question["solution_steps"] = ""
    return questions


def _canonical_choice_option_value(value: object) -> str:
    """Normalize harmless presentation differences without evaluating maths."""
    text = _normalize_template_text(value).strip()
    text = text.replace("−", "-").replace("－", "-").replace("–", "-")
    text = re.sub(r"\s+", "", text)
    return text.rstrip("。．、;；")


def _choice_answer_from_visible_options(question: Dict) -> str:
    """Map a raw answer value back to exactly one visible A/B/C/D option.

    For example, an answer key may print ``-3`` while the question options are
    ``A. 3 B. 1 C. -3 D. -1``.  In that case the frozen choice answer is ``C``.
    We never evaluate equations or guess semantic equivalence here; ambiguous
    formatting falls through to the vision repair pass.
    """
    raw_answer = _canonical_choice_option_value(question.get("standard_answer", ""))
    if not raw_answer:
        return ""
    text = _normalize_template_text(question.get("question_text") or question.get("content") or "")
    matches = list(_CHOICE_OPTION_MARKER_RE.finditer(text))
    if len(matches) < 4:
        return ""

    options: dict[str, str] = {}
    for index, marker in enumerate(matches):
        label = marker.group(1).upper()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = _canonical_choice_option_value(text[marker.end():end])
        if label not in options and value:
            options[label] = value

    matched_labels = [label for label, value in options.items() if value == raw_answer]
    return "".join(sorted(set(matched_labels)))


def _normalize_valid_choice_answers(questions: List[Dict]) -> None:
    """Canonicalize option letters or map raw answer values to visible options."""
    for question in questions:
        if str(question.get("question_type") or "").strip().lower() != "choice":
            continue
        normalized = normalize_choice_answer(question.get("standard_answer", ""))
        if not normalized:
            normalized = _choice_answer_from_visible_options(question)
        if normalized:
            question["standard_answer"] = normalized


def _choice_answer_conflicts(questions: List[Dict]) -> List[Dict]:
    return [
        question for question in questions
        if str(question.get("question_type") or "").strip().lower() == "choice"
        and (
            not normalize_choice_answer(question.get("standard_answer", ""))
            or str(question.get("choice_mode") or "unknown") == "unknown"
        )
    ]


async def _repair_choice_answer_conflicts(
    image_path: str,
    questions: List[Dict],
) -> List[Dict]:
    """Re-read only OCR choice/type conflicts from the original template image.

    A repair is accepted only if it gives a valid question number, type and
    answer. Failed/ambiguous repairs deliberately leave the conflict visible for
    teacher correction instead of silently rewriting its type.
    """
    _normalize_valid_choice_answers(questions)
    suspects = _choice_answer_conflicts(questions)
    if not suspects:
        return questions

    suspect_numbers = {
        str(question.get("question_number") or question.get("question_no") or "").strip()
        for question in suspects
    }
    suspect_numbers.discard("")
    if not suspect_numbers:
        return questions

    suspect_text = "\n\n".join(
        "第{number}题\n题干：{text}\n上次识别的答案：{answer}".format(
            number=str(question.get("question_number") or question.get("question_no") or "").strip(),
            text=str(question.get("question_text") or "").strip(),
            answer=str(question.get("standard_answer") or "").strip(),
        )
        for question in suspects
    )
    try:
        raw = await call_vision(
            image_path,
            stage_prompt(TEMPLATE_CHOICE_REPAIR_PROMPT).format(suspects=suspect_text),
            max_attempts=1,
            max_tokens=_TEMPLATE_GPT_MAX_OUTPUT_TOKENS,
        )
        repaired = parse_llm_json(raw)
    except Exception as exc:
        logger.warning("template choice/type repair failed: %s", exc)
        return questions

    candidates = {}
    for item in (repaired.get("questions") or []):
        number = str(item.get("question_number") or item.get("question_no") or "").strip()
        question_type = str(item.get("question_type") or "").strip().lower()
        answer = str(item.get("standard_answer") or "").strip()
        choice_mode = str(item.get("choice_mode") or "unknown").strip().lower()
        if number not in suspect_numbers or question_type not in {"choice", "fill", "solve"} or not answer:
            continue
        if question_type == "choice":
            answer = normalize_choice_answer(answer)
            if not answer or choice_mode not in {"single", "multiple"}:
                continue
        candidates[number] = {
            "question_type": question_type,
            "choice_mode": choice_mode if question_type == "choice" else "unknown",
            "standard_answer": answer,
        }

    for question in questions:
        number = str(question.get("question_number") or question.get("question_no") or "").strip()
        repair = candidates.get(number)
        if repair:
            question.update(repair)

    _normalize_valid_choice_answers(questions)
    return questions


# ── 核心函数 ──

async def recognize_template_with_answers(
    image_path: str,
) -> Dict:
    """兼容旧答题卡流程的“带答案模板”识别。

    普通模板注册不会调用此函数；它统一使用
    :func:`recognize_template_pipeline`，只转录印刷题干并独立生成答案。
    
    Returns:
        {
            "questions": [
                {
                    "question_number": "1",
                    "question_text": "...",
                    "standard_answer": "..."
                }
            ]
        }
    """
    last_error: Exception | None = None
    for attempt in range(_TEMPLATE_OCR_CONTRACT_ATTEMPTS):
        try:
            # The outer contract loop is the single retry for this template
            # operation. Avoid multiplying it with the Qwen transport retry.
            raw_result = await call_vision(
                image_path,
                stage_prompt(TEMPLATE_WITH_ANSWER_PROMPT),
                max_tokens=8192,
                max_attempts=1,
            )
            data = parse_llm_json(raw_result)
            questions = data.get("questions")
            if not isinstance(questions, list) or not questions:
                raise ValueError("未能识别到题目")

            _normalize_template_question_texts(questions)
            questions = await _repair_choice_answer_conflicts(
                image_path,
                questions,
            )
            questions = _validate_template_questions(
                questions,
                require_standard_answer=True,
            )
            if attempt:
                logger.info("Template with answers OCR contract recovered on retry")
            logger.info("Template with answers recognized: %d questions", len(questions))
            return {"questions": questions}
        except Exception as exc:
            last_error = exc
            if attempt + 1 < _TEMPLATE_OCR_CONTRACT_ATTEMPTS:
                logger.warning(
                    "Template with answers OCR contract rejected (%s); retrying",
                    type(exc).__name__,
                )

    logger.error(
        "recognize_template_with_answers failed: %s",
        last_error,
        exc_info=last_error,
    )
    return {"error": str(last_error or "未能识别到题目"), "questions": []}


def _verify_multi_image_association(
    questions: list[dict],
    image_count: int,
) -> list[int]:
    """Require every question of a multi-image request to name its image.

    The one-request compatibility path sends N ordered column images.  Each
    returned question must explicitly carry a 1-based ``image_index`` inside
    that range, and the indexes must follow the image order.  A legacy
    ``image_id`` may accompany it only as a matching numeric echo; it cannot
    replace ``image_index``.  A missing or conflicting association is a review
    condition, and the server never guesses which image a question came from.
    """
    association: list[int] = []
    # Validate the complete sequence before stripping request-only metadata.
    # If a later item is missing/out of range, callers still need the earlier
    # validated questions as a reviewable transcription draft.
    previous = 1

    def review_failure(
        message: str,
        *,
        status: str,
        question_number: str,
        raw_value: object = None,
        parsed_index: int | None = None,
        raw_image_id: object = None,
        parsed_image_id: int | None = None,
        cause: BaseException | None = None,
    ):
        error = _TemplateReviewError(message)
        # Expose only the valid prefix; the offending item is deliberately not
        # treated as an association fact.  The caller stores this diagnostic
        # separately from the text-only question contract.
        error.image_association = list(association)
        conflict = {
            "question_number": question_number,
            "status": status,
            "reported_value": raw_value,
            "parsed_index": parsed_index,
        }
        # Keep the legacy diagnostic shape for ordinary index failures.  Add
        # the second field only when the model actually supplied it, so a
        # reviewer can distinguish an omitted echo from an explicit conflict.
        if raw_image_id is not None:
            conflict.update({
                "reported_image_id": raw_image_id,
                "parsed_image_id": parsed_image_id,
            })
        error.image_association_diagnostics = {
            "version": "template-image-association-v1",
            "status": "needs_review",
            "needs_review": True,
            "image_count": image_count,
            "accepted_prefix": list(association),
            "conflict": conflict,
        }
        if cause is not None:
            raise error from cause
        raise error

    for question in questions:
        number = str(question.get("question_number") or "").strip()
        raw_index = question.get("image_index")
        raw_image_id = question.get("image_id")
        if raw_index is None:
            review_failure(
                f"多图转录第 {number} 题缺少 image_index，需人工复核（needs_review）",
                status="missing",
                question_number=number,
                raw_image_id=raw_image_id,
            )
        try:
            index = int(str(raw_index).strip())
        except (TypeError, ValueError) as exc:
            review_failure(
                f"多图转录第 {number} 题的 image_index 无效，需人工复核（needs_review）",
                status="invalid",
                question_number=number,
                raw_value=raw_index,
                raw_image_id=raw_image_id,
                cause=exc,
            )
        parsed_image_id: int | None = None
        if "image_id" in question:
            try:
                parsed_image_id = int(str(raw_image_id).strip())
            except (TypeError, ValueError) as exc:
                review_failure(
                    f"多图转录第 {number} 题的 image_id 无效，与 image_index "
                    "无法确认一致，需人工复核（needs_review）",
                    status="conflicting_fields",
                    question_number=number,
                    raw_value=raw_index,
                    parsed_index=index,
                    raw_image_id=raw_image_id,
                    cause=exc,
                )
            if parsed_image_id != index:
                review_failure(
                    f"多图转录第 {number} 题的 image_index={index} 与 "
                    f"image_id={parsed_image_id} 冲突，需人工复核（needs_review）",
                    status="conflicting_fields",
                    question_number=number,
                    raw_value=raw_index,
                    parsed_index=index,
                    raw_image_id=raw_image_id,
                    parsed_image_id=parsed_image_id,
                )
        if not 1 <= index <= image_count:
            review_failure(
                f"多图转录第 {number} 题的 image_index={index} 超出 1..{image_count}，"
                "需人工复核（needs_review）",
                status="out_of_range",
                question_number=number,
                raw_value=raw_index,
                parsed_index=index,
            )
        if index < previous:
            review_failure(
                f"多图转录第 {number} 题的 image_index 与阅读顺序颠倒，需人工复核（needs_review）",
                status="reversed",
                question_number=number,
                raw_value=raw_index,
                parsed_index=index,
            )
        previous = index
        association.append(index)
    for question in questions:
        # The association is reported separately; keep the persisted question
        # contract free of request-shaped metadata.
        question.pop("image_index", None)
        question.pop("image_id", None)
    return association


async def _recognize_template_blank_request(
    image_input: str | list[str],
    *,
    prompt: str,
    expected_numbers: list[str] | None = None,
    require_image_association: bool = False,
) -> Dict:
    """Run one Qwen printed-text request and enforce its transcription contract.

    ``image_input`` may be one image or an ordered list of column images.  The
    latter is deliberately a single HTTP request for legacy layouts whose
    column identity cannot be trusted.  With ``require_image_association`` the
    response must associate every question with one input image; the result
    then carries ``image_association`` in image order.  Template transcription
    always stays on the Qwen route; the YS/Qwen route selector belongs
    exclusively to the later question-anchor/exemption pass.
    """
    last_error: Exception | None = None
    review_questions: list[dict] = []
    review_association: list[int] = []
    review_association_diagnostics: dict = {}
    image_count = len(image_input) if isinstance(image_input, list) else 1
    if require_image_association and not isinstance(image_input, list):
        raise ValueError("逐图关联只适用于多图请求")

    def validate_response(content: str) -> None:
        nonlocal review_questions, review_association, review_association_diagnostics
        payload = parse_llm_json(content)
        if not isinstance(payload, dict):
            raise ValueError("模板 GPT 返回值必须是 JSON 对象")
        candidate_questions = payload.get("questions")
        _normalize_template_question_texts(candidate_questions or [])
        validated = _validate_template_questions(
            candidate_questions,
            require_standard_answer=False,
        )
        _clear_transcription_answers(validated)
        # Keep a text-only copy before association validation can fail.  This
        # is the recoverable draft shown to the teacher; it is never an
        # approved geometry or a reason to guess an image binding.
        review_questions = [
            {
                key: value
                for key, value in question.items()
                if key not in {"image_index", "image_id"}
            }
            for question in validated
        ]
        if require_image_association:
            try:
                review_association = _verify_multi_image_association(
                    validated,
                    image_count,
                )
            except _TemplateReviewError as exc:
                review_association = list(
                    getattr(exc, "image_association", []) or []
                )
                review_association_diagnostics = dict(
                    getattr(exc, "image_association_diagnostics", {}) or {}
                )
                raise
        if expected_numbers is not None:
            actual_numbers = [
                str(question.get("question_number") or "").strip()
                for question in validated
            ]
            if actual_numbers != expected_numbers:
                raise ValueError("模板分栏题目顺序或覆盖范围不一致")

    for attempt in range(_TEMPLATE_OCR_CONTRACT_ATTEMPTS):
        try:
            call_kwargs = {
                "max_tokens": _TEMPLATE_GPT_MAX_OUTPUT_TOKENS,
                "response_validator": validate_response,
                # The outer contract loop is the single retry for this
                # operation; keep each provider attempt atomic.
                "max_attempts": 1,
            }
            if isinstance(image_input, list):
                raw_result = await call_vision_images(
                    image_input,
                    prompt,
                    **call_kwargs,
                )
            else:
                raw_result = await call_vision(
                    image_input,
                    prompt,
                    **call_kwargs,
                )
            data = parse_llm_json(raw_result)
            questions = data.get("questions")
            if not isinstance(questions, list) or not questions:
                raise ValueError("未能识别到题目")

            _normalize_template_question_texts(questions)
            questions = _validate_template_questions(
                questions,
                require_standard_answer=False,
            )
            questions = _clear_transcription_answers(questions)
            review_questions = [
                {
                    key: value
                    for key, value in question.items()
                    if key not in {"image_index", "image_id"}
                }
                for question in questions
            ]
            association: list[int] | None = None
            if require_image_association:
                try:
                    association = _verify_multi_image_association(
                        questions,
                        image_count,
                    )
                    review_association = list(association)
                except _TemplateReviewError as exc:
                    review_association = list(
                        getattr(exc, "image_association", []) or []
                    )
                    review_association_diagnostics = dict(
                        getattr(exc, "image_association_diagnostics", {}) or {}
                    )
                    raise
            if expected_numbers is not None:
                actual_numbers = [
                    str(question.get("question_number") or "").strip()
                    for question in questions
                ]
                if actual_numbers != expected_numbers:
                    raise ValueError("模板分栏题目顺序或覆盖范围不一致")
            if attempt:
                logger.info("Template blank OCR contract recovered on retry")
            logger.info("Template blank recognized: %d questions", len(questions))
            result: Dict = {"questions": questions}
            if association is not None:
                result["image_association"] = association
            return result
        except Exception as exc:
            last_error = exc
            if _is_template_review_failure(exc):
                # An association conflict is a review condition, not a
                # transient contract slip; a second paid attempt would only
                # pick a guess.
                break
            if attempt + 1 < _TEMPLATE_OCR_CONTRACT_ATTEMPTS:
                logger.warning(
                    "Template blank OCR contract rejected (%s); retrying",
                    type(exc).__name__,
                )

    logger.error(
        "recognize_template_blank failed: %s",
        last_error,
        exc_info=last_error,
    )
    failure: Dict = {
        "error": str(last_error or "未能识别到题目"),
        "questions": review_questions,
    }
    if _is_template_review_failure(last_error):
        failure["needs_review"] = True
        if require_image_association:
            wrapped_association = _template_anchor_review_attr(
                last_error,
                "image_association",
                review_association,
            )
            failure["image_association"] = (
                list(wrapped_association)
                if isinstance(wrapped_association, list)
                else []
            )
            failure["image_association_diagnostics"] = dict(
                _template_anchor_review_attr(
                    last_error,
                    "image_association_diagnostics",
                    review_association_diagnostics,
                )
                or {}
            )
            failure["image_association_error"] = str(last_error)
    return failure


async def recognize_template_blank(
    image_path: str,
) -> Dict:
    """Transcribe printed questions from a template without reading answers.

    Template printed-text OCR is a fixed Qwen route; only the anchor stage may
    select YS or its persistent Qwen fallback.
    """
    return await _recognize_template_blank_request(
        image_path,
        prompt=stage_prompt(TEMPLATE_BLANK_PROMPT),
    )


TEMPLATE_BATCH_ANSWER_SYSTEM_PROMPT = """你是模板注册阶段的中国高中数学标准答案生成器。
页面矫正、分栏、题号识别和题干转录已经完成；当前输入只包含已经确认的填空题和解答题文本，不是学生作答图片。只为这些题生成标准答案，不能修改题号或题干。
输入只包含填空题和解答题；请一次完成这些题目。

数学口径：本任务范围限定为中国高中数学。严格按照中国高中数学教材、课程标准和高中考试命题语境作答，
不得套用大学数学、形式逻辑或超出高中课程的特殊解释。

输出合同：
1. 只能返回 JSON 对象，不要 Markdown 或其他文字。
2. answers 必须与输入题目一一对应，question_number 原样返回，不得遗漏、重复或新增。
3. 禁止返回 choice；选择题已经由视觉模型识别并由后端机械比对，不进入本次调用。
4. fill：standard_answer 只写最终结果；多空按题干顺序用分号分隔，model_answer 为空字符串。
5. solve：standard_answer 写最终答案，model_answer 只写必要步骤，单题不超过 300 个汉字。

严格 JSON：
{"answers":[{"question_number":"原题号","standard_answer":"答案","model_answer":"必要解题过程"}]}
"""


async def generate_template_answers_batch(questions: List[Dict]) -> Dict:
    """Generate one complete answer set with one qwen3.8-max (Token Plan) request."""
    normalized_questions = []
    expected_numbers = []
    for question in questions:
        number = str(question.get("question_number") or question.get("question_no") or "").strip()
        text = _normalize_template_text(question.get("question_text") or question.get("content") or "").strip()
        question_type = str(question.get("question_type") or "solve").strip().lower()
        if not number or not text or question_type not in {"choice", "fill", "solve"}:
            return {"error": "模板题目结构不完整，无法整卷生成答案", "answers": {}}
        expected_numbers.append(number)
        normalized_questions.append({
            "question_number": number,
            "question_type": question_type,
            "question_text": text,
        })

    base_messages = [
        {"role": "system", "content": stage_prompt(TEMPLATE_BATCH_ANSWER_SYSTEM_PROMPT)},
        {"role": "user", "content": json.dumps({"questions": normalized_questions}, ensure_ascii=False)},
    ]
    last_error: Exception | None = None
    for attempt in range(2):
        messages = list(base_messages)
        if attempt:
            messages.append({
                "role": "user",
                "content": (
                    "上一次输出不是完整可解析的 JSON。请重新计算并从头返回完整 JSON；"
                    "填空题 model_answer 必须为空字符串，解答题步骤保持精简，"
                    "不得输出 Markdown、前后说明或截断内容。"
                ),
            })
        try:
            content = await call_template_max_text(messages)
            parsed = parse_llm_json(content)
            raw_answers = parsed.get("answers") if isinstance(parsed, dict) else None
            if not isinstance(raw_answers, list):
                raise ValueError("整卷答案缺少 answers 数组")

            answers: dict[str, dict] = {}
            for item in raw_answers:
                if not isinstance(item, dict):
                    continue
                number = str(item.get("question_number") or item.get("question_no") or "").strip()
                if number not in expected_numbers or number in answers:
                    continue
                standard_answer = str(item.get("standard_answer") or "").strip()
                if not standard_answer:
                    continue
                answers[number] = {
                    "standard_answer": standard_answer,
                    "model_answer": str(item.get("model_answer") or "").strip(),
                }

            missing = [number for number in expected_numbers if number not in answers]
            if missing:
                label = "、".join(f"第{number}题" for number in missing)
                raise ValueError(f"整卷答案不完整：{label}缺少有效答案")
            if attempt:
                logger.info("Template Max answer retry recovered all %d questions", len(answers))
            return {"answers": answers}
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Template Max answer attempt %d/2 invalid: %s",
                attempt + 1,
                exc,
            )

    logger.error("generate_template_answers_batch failed after one retry: %s", last_error)
    return {"error": str(last_error or "整卷答案生成失败"), "answers": {}}


async def recognize_template_pipeline(
    image_path: str,
    *,
    template_mode: str = "marked",
    layout_result: Dict | None = None,
    on_text_ready=None,
) -> Dict:
    """Build one authoritative Q directory, then locate and solve the template."""
    from services.template_directory_pipeline import recognize_directory_template
    return await recognize_directory_template(
        image_path, template_mode=template_mode, layout_result=layout_result,
        **({"on_text_ready": on_text_ready} if on_text_ready is not None else {}),
    )
