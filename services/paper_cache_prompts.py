"""What the OCR model is asked about a student page, built from the frozen template contract.

The prompt templates: ``RECOGNIZE_PAGE_AND_ANSWERS_PROMPT`` (match the page type
and transcribe in one request, with ``PAGE_IDENTITY_ROUTING_INSTRUCTION``),
``RECOGNIZE_ANSWERS_ONLY_PROMPT`` (transcribe an already-routed page), and
``RECOGNIZE_EVIDENCE_ONLY_PROMPT``, ``RECOGNIZE_ANCHOR_CHUNKS_PROMPT`` and
``RECOGNIZE_FULL_HEIGHT_TRANSCRIPTION_PROMPT`` (transcribe locally generated
evidence images).  ``_compact_transcription_kinds`` keeps only the per-question
hints transcription needs.

``_PaperCachePromptsMixin`` holds these ``PaperCache`` methods:
``_question_schema_for_page`` and ``_expected_question_numbers`` give one page's
answer-shape contract, ``_frozen_page_types_contract`` the deterministic page
metadata, and ``_build_page_and_answers_prompt``, ``_build_answer_only_prompt``
and ``_build_evidence_only_prompt`` fill the templates.

Moved unchanged from services/paper_cache.py, which still exposes these names.
"""

import copy
import json
import logging

from services.paper_cache_base import (
    _VISUAL_GRADING_EVIDENCE_STRATEGIES,
    _normalize_question_number,
    _question_number_sort_key,
)

# services.paper_cache's logger, so these records keep their logger name.
logger = logging.getLogger("services.paper_cache")


# ── 冻结模板后的页型匹配与学生答案转录 prompt ──
# Production transcription prompts are intentionally short.  The frozen
# template and the JSON contract already carry the routing information.
RECOGNIZE_PAGE_AND_ANSWERS_PROMPT = """[task=student_transcription_only]
你是中国高中数学试卷 OCR。
输入是已经矫正的完整试卷页面；如果本次请求包含多张图，它们按从左到右合起来仍是同一整页，不是多张不同试卷。请先根据图片中实际可见的印刷题号匹配教师已确认的页型，再按该页型顺序抄录学生笔迹。
只抄图片中学生实际写下的内容。题干、标准答案和数学知识不能作为转录依据；不要解题、判分、纠正、补全或把内容移动到相邻题。
根号、分数、正负号、括号、数字和字母按图原样抄写。空白返回 blank 和空字符串，看不清返回 unreadable 和 [无法识别]。不要把数字或公式主动转换成选项字母；选择题的明确归一化由服务端处理。
忽略试卷上栏和侧边栏中的姓名、学号、班级等身份信息，不要识别或输出这些内容。

frozen_page_types={frozen_page_types}

严格返回 JSON，不要 Markdown 或其他文字：
{
  "page_type_id": "page_1 或空字符串",
  "observed_question_numbers": ["1"],
  "questions": [
    {"question_number": "1", "student_answer": "", "answer_state": "blank|visible|unreadable"}
  ]
}"""

RECOGNIZE_ANSWERS_ONLY_PROMPT = """[task=student_transcription_only]
你是中国高中数学试卷 OCR。
页型和分栏已经由教师确认并冻结：{page_type_id}。当前图片是按已确认分栏规则处理的一张完整页面（可由 1 至 3 张按从左到右排列的全高栏图组成）。请按 question_numbers 和蓝色 Q 题号顺序，只抄录每道题对应的学生笔迹。
只抄图片中可见笔迹，按原样返回。题干、标准答案和数学知识不能作为转录依据；不解题、不判分、不串题。
根号、分数、正负号和括号、数字和字母按图原样抄写。空白为 blank，无法辨认为 unreadable。不要把数字或公式主动转换成选项字母；选择题的明确归一化由服务端处理。
同一题若有多个独立作答框（例如一道计算题含 8 个计算式），student_answer 必须按作答框从左到右、从上到下用中文分号“；”分隔，空框也要保留分隔位置，不得把整题合成一个答案。
忽略试卷上栏和侧边栏中的姓名、学号、班级等身份信息，不要识别或输出这些内容。

题号清单={question_numbers}
作答类型={answer_kinds}

严格只返回 JSON，不要 Markdown 或其他文字：
{
  "questions": [
    {"question_number": "1", "student_answer": "", "answer_state": "blank|visible|unreadable"}
  ]
}"""


# Appended only for templates that carry page roles or printed page identity.
PAGE_IDENTITY_ROUTING_INSTRUCTION = """
不同页型可能是不同的试卷或同一试卷的不同页，印刷题号可能重复（例如两份试卷都从第1题开始）；question_numbers 是整卷统一编号，printed_path 才是图中印刷的原题号。必须先按试卷内容（卷名/页眉、题干、版式）判断本页属于哪个页型，再按该页型的 question_numbers 返回题号和答案，不能只凭印刷题号选页型。
页型可带 page_role：answer_page 与 answer_sheet 需要抄录；question_only（题目页）与 non_gradable（不计分页）的 question_numbers 为空，不抄录。若本页是不计分页型，返回该 page_type_id、observed_question_numbers=[] 和 questions=[]。
另外在 JSON 顶层返回页面身份证据，仅供服务端核对页型，不是学生答案："observed_page_label"（图中实际印刷的卷名、页眉或页码原文，如“答题卡”，看不到则为空字符串）；"observed_printed_labels"（图中实际可见的印刷题号原文数组）；"observed_stem_snippets"（图中最多 5 道印刷题干各自开头的原文，每道不超过 20 字，只抄印刷文字，不含学生笔迹，看不到则为空数组）。"""


RECOGNIZE_EVIDENCE_ONLY_PROMPT = """[task=student_transcription_only]
你是中国高中数学试卷 OCR。
一、这是什么
这是已矫正、按已确认且冻结的模板分栏规则生成的模板差分后的学生卷图。1 至 3 张图片从左到右组成同一页。图中保留学生笔迹、蓝色 Q 题号标签和定位引线；可能残留不完整题干或印刷线条，作图区已涂白，图形另行识别。

页型：{page_type_id}
题号清单={question_numbers}
作答类型={answer_kinds}

二、该干什么，注意什么
按题号清单、作答类型和蓝色 Q 标签转录。带短引线的标签对应引线连接的作答区域；重复的 Q 属于同一题。分栏逐栏阅读，栏内从上到下、同行从左到右，同题各区域按此顺序返回。保留清单题号，不拆分、合并或改号。标签可能在原栏图外的新增白色边栏，按同一行的引线定位作答起点；引线遇到笔迹会断开以保护笔迹，不是答案下边界。标签、引线和印刷残留不是答案。只抄可见笔迹，按原样返回；不解题、不判分。作图题只转录文字，图形另行识别。没有文字返回 blank，无法辨认或无法确定归属返回 unreadable。

三、该返回什么
严格只返回 JSON，不要 Markdown 或其他文字：
{
  "questions": [
    {"question_number": "1", "student_answer": "", "answer_state": "blank|visible|unreadable"}
  ]
}"""

RECOGNIZE_ANCHOR_CHUNKS_PROMPT = """[task=student_transcription_only]
你是中国高中数学试卷 OCR。
当前请求中的 1 至 3 张图片按从左到右排列后是同一张完整页面。这是模板差分后的学生卷图；蓝色 Q 题号标签位于对应作答区域左侧，仅用于题号定位，不是学生答案，也不是答题框；图中不提供红色题号框或蓝色作答区域框。
请按题号清单、作答类型和蓝色 Q 题号顺序逐题转录。Q 标签是题号依据，只抄对应题号的学生实际手写痕迹，不要把残留印刷内容写入 student_answer。
学生笔迹按图原样转录，不解题、不判分、不串题。
同一题若有多个独立作答框，student_answer 按作答框顺序用“；”分隔，空框保留空位；不要把多个框合并成一个字符串。
根号、分数、正负号和括号、数字和字母按图原样抄写。无可见笔迹时返回 answer_state="blank"、student_answer=""；有痕迹但无法辨认或无法可靠归属时返回 answer_state="unreadable"、student_answer="[无法识别]"。不要把数字或公式主动转换成选项字母；选择题的明确归一化由服务端处理。

页型：{page_type_id}
题号清单={question_numbers}
作答类型={answer_kinds}

严格只返回 JSON，不要 Markdown 或其他文字：
{
  "questions": [
    {"question_number": "1", "student_answer": "", "answer_state": "blank|visible|unreadable"}
  ]
}"""

RECOGNIZE_FULL_HEIGHT_TRANSCRIPTION_PROMPT = """[task=student_transcription_only]
你是中国高中数学试卷 OCR。
一、这是什么
这是已矫正、按已确认且冻结的模板分栏规则生成的模板差分后的学生卷图。1 至 3 张图片从左到右组成同一页。图中保留学生笔迹、蓝色 Q 题号标签和定位引线；可能残留不完整题干或印刷线条，作图区已涂白，图形另行识别。

题号清单={question_numbers}
作答类型={answer_kinds}

二、该干什么，注意什么
按题号清单、作答类型和蓝色 Q 标签转录。带短引线的标签对应引线连接的作答区域；重复的 Q 属于同一题。分栏逐栏阅读，栏内从上到下、同行从左到右，同题各区域按此顺序返回。保留清单题号，不拆分、合并或改号。标签可能在原栏图外的新增白色边栏，按同一行的引线定位作答起点；引线遇到笔迹会断开以保护笔迹，不是答案下边界。标签、引线和印刷残留不是答案。只抄可见笔迹，按原样返回；不解题、不判分、不串题。根号、分数、正负号、括号、数字和字母按图抄写。作图题只转录文字作答，图形另行识别。没有文字返回 blank，无法辨认或无法确定归属返回 unreadable。
同一题若有多个独立作答框，student_answer 按作答框顺序用“；”分隔，空框保留空位；不要把多个框合并成一个字符串。

三、该返回什么
严格只返回 JSON，不要 Markdown 或其他文字：
{
  "questions": [
    {"question_number": "1", "student_answer": "", "answer_state": "blank|visible|unreadable", "confidence": "high|medium|low"}
  ]
}"""


def _compact_transcription_kinds(questions: list[dict]) -> dict[str, str]:
    """Return only the per-question routing hints needed by transcription OCR."""
    kinds: dict[str, str] = {}
    for question in questions:
        number = str(question.get("question_number") or question.get("question_no") or "").strip()
        if not number:
            continue
        question_type = str(question.get("question_type") or "solve").strip().lower()
        if question_type not in {"choice", "fill", "solve"}:
            question_type = "solve"
        answer_kind = str(question.get("answer_kind") or "non_graphic").strip().lower()
        if answer_kind in {"drawing", "mixed"}:
            kinds[number] = answer_kind
        elif question_type == "choice":
            choice_mode = str(question.get("choice_mode") or "unknown").strip().lower()
            kinds[number] = f"choice:{choice_mode}"
        else:
            kinds[number] = question_type
    return kinds


class _PaperCachePromptsMixin:
    """PaperCache's methods for the frozen page contract and the OCR prompts built from it."""

    def _question_schema_for_page(self, label: str) -> list[dict]:
        """Return the compact, reusable answer-shape contract for one page."""
        baseline = self._baselines.get(label) or {}
        questions = baseline.get("ocr_result", {}).get("questions", [])
        schema = []
        for question in questions:
            question_number = _normalize_question_number(
                str(question.get("question_number") or "")
            )
            if not question_number:
                continue
            question_type = str(
                question.get("question_type") or "solve"
            ).strip().lower()
            if question_type not in {"choice", "fill", "solve"}:
                question_type = "solve"
            item = {
                "question_number": question_number,
                "question_type": question_type,
            }
            if question_type == "choice":
                choice_mode = str(
                    question.get("choice_mode") or "unknown"
                ).strip().lower()
                item["choice_mode"] = (
                    choice_mode if choice_mode in {"single", "multiple"}
                    else "unknown"
                )
            for key in ("blank_count", "subquestion_count"):
                try:
                    count = int(question.get(key) or 1)
                except (TypeError, ValueError):
                    count = 1
                if count > 1:
                    item[key] = count
            answer_slots = question.get("answer_slots")
            if isinstance(answer_slots, list) and answer_slots:
                # Slots contain reference answers, not visual shape hints.
                # OCR receives their count only, never teacher answer values.
                item["blank_count"] = len(answer_slots)
            for key in ("directory_node_id", "directory_parent_id", "printed_path", "answer_kind"):
                if key in question:
                    item[key] = copy.deepcopy(question[key])
            schema.append(item)
        return schema

    def _frozen_page_types_contract(self) -> list[dict]:
        """Build deterministic page metadata used by the joint OCR prompt."""
        contract = []
        labels = sorted(
            set(self._baselines) | set(self._non_graded_pages),
            key=lambda value: _question_number_sort_key(str(value)),
        )
        for label in labels:
            schema = self._question_schema_for_page(label)
            if not schema and label not in self._non_graded_pages:
                continue
            entry = {
                "page_type_id": label,
                "question_numbers": [
                    item["question_number"] for item in schema
                ],
                "question_schema": schema,
            }
            # Role and printed labels are added only for templates that carry
            # them, so a legacy template keeps its exact routing prompt.
            if label in self._page_roles:
                entry["page_role"] = self._page_roles[label]
            identity = self._page_identities.get(label) or {}
            for key in ("printed_page_label", "printed_question_labels"):
                if identity.get(key):
                    entry[key] = copy.deepcopy(identity[key])
            contract.append(entry)
        return contract

    def _build_page_and_answers_prompt(self) -> str:
        """Build the stable one-request page matcher/transcription prompt."""
        contract = self._frozen_page_types_contract()
        if not contract:
            return ""
        contract_text = json.dumps(
            contract,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        from services.teaching_context import current_teaching_context, stage_prompt
        context_key = json.dumps(current_teaching_context(), sort_keys=True)
        contract_key = f"stage={context_key}:{contract_text}"
        if contract_key == self._joint_prompt_contract_key and self._joint_prompt:
            return self._joint_prompt
        self._joint_prompt_contract_key = contract_key
        self._joint_prompt = stage_prompt(RECOGNIZE_PAGE_AND_ANSWERS_PROMPT).replace(
            "{frozen_page_types}", contract_text,
        )
        if self._page_roles or self._page_identities:
            self._joint_prompt += PAGE_IDENTITY_ROUTING_INSTRUCTION
        return self._joint_prompt

    def _expected_question_numbers(self, label: str) -> list[str]:
        return [
            item["question_number"]
            for item in self._question_schema_for_page(label)
        ]

    def _build_answer_only_prompt(
        self,
        type_label: str | None = None,
        *,
        alignment_verification: int | bool = False,
    ) -> str:
        """Build a transcription-only prompt for one already-routed page."""
        label = str(type_label or "").strip()
        if not label and len(self._baselines) == 1:
            label = next(iter(self._baselines))
        if label not in self._baselines:
            return ""
        questions = self._baselines[label]["ocr_result"].get("questions", [])
        question_numbers = [
            str(q.get("question_number") or "").strip()
            for q in questions
            if str(q.get("question_number") or "").strip()
        ]
        if not question_numbers:
            return ""
        answer_kinds = _compact_transcription_kinds(questions)
        from services.teaching_context import stage_prompt
        prompt = (
            f"[page_type_id={label}]\n"
            + stage_prompt(RECOGNIZE_ANSWERS_ONLY_PROMPT)
            .replace("{page_type_id}", label)
            .replace(
                "{question_numbers}",
                json.dumps(question_numbers, ensure_ascii=False),
            )
            .replace(
                "{answer_kinds}",
                json.dumps(
                    answer_kinds,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        )
        if alignment_verification:
            try:
                verification_round = max(1, int(alignment_verification))
            except (TypeError, ValueError):
                verification_round = 1
            prompt += f"""

这是第 {verification_round} 次独立的题号归属复核，不得沿用任何先前识别结果。逐题执行：先在原图中找到该题印刷题号，再沿同一行或同一题块确认答题区域，最后转录笔迹。特别检查空题边界，禁止因某题空白而把后续非空答案依次前移或后移。若题号与笔迹无法可靠对应，将该题标为 unreadable，不得猜测归属。"""
        return prompt

    def _build_evidence_only_prompt(
        self,
        type_label: str | None = None,
        *,
        evidence_strategy: str = "",
        question_numbers: list[str] | None = None,
        split_transcription: bool | None = None,
    ) -> str:
        """Build the prompt that matches the locally generated evidence shape."""
        from services.teaching_context import stage_prompt
        label = str(type_label or "").strip()
        if not label and len(self._baselines) == 1:
            label = next(iter(self._baselines))
        if label not in self._baselines:
            return ""
        questions = self._baselines[label]["ocr_result"].get("questions", [])
        requested_numbers = (
            {
                _normalize_question_number(str(question_number))
                for question_number in question_numbers
                if _normalize_question_number(str(question_number))
            }
            if question_numbers is not None
            else None
        )
        schema: dict[str, list[str]] = {}
        question_order: list[str] = []
        answer_kinds = _compact_transcription_kinds(questions)
        for position, question in enumerate(questions, 1):
            question_number = str(question.get("question_number") or "").strip()
            if not question_number or (
                requested_numbers is not None
                and _normalize_question_number(question_number) not in requested_numbers
            ):
                continue
            question_order.append(question_number)
            question_type = str(
                question.get("question_type") or "solve"
            ).strip().lower()
            if question_type not in {"choice", "fill", "solve"}:
                question_type = "solve"
            schema.setdefault(question_type, []).append(question_number)
        if not schema:
            return ""
        if evidence_strategy in _VISUAL_GRADING_EVIDENCE_STRATEGIES:
            return (
                stage_prompt(RECOGNIZE_FULL_HEIGHT_TRANSCRIPTION_PROMPT)
                .replace("{page_type_id}", label)
                .replace(
                    "{question_numbers}",
                    json.dumps(
                        question_order,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
                .replace(
                    "{answer_kinds}",
                    json.dumps(
                        answer_kinds,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
            )
        if evidence_strategy in {
            "continuous_question_crop_plan",
            "full_column_difference",
        }:
            prompt_template = stage_prompt(RECOGNIZE_ANCHOR_CHUNKS_PROMPT)
        elif evidence_strategy in {
            "",
            "confirmed_bbox_neighbor_clamped",
            "confirmed_bbox_focus_sheets",
        }:
            prompt_template = stage_prompt(RECOGNIZE_EVIDENCE_ONLY_PROMPT)
        else:
            logger.warning("Unsupported local evidence strategy: %s", evidence_strategy)
            return ""
        rendered = prompt_template.replace("{page_type_id}", label)
        if evidence_strategy in {
            "continuous_question_crop_plan",
            "full_column_difference",
        }:
            rendered = rendered.replace(
                "{question_numbers}",
                json.dumps(question_order, ensure_ascii=False, separators=(",", ":")),
            ).replace(
                "{answer_kinds}",
                json.dumps(
                    answer_kinds,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        else:
            rendered = rendered.replace(
                "{question_numbers}",
                json.dumps(question_order, ensure_ascii=False, separators=(",", ":")),
            ).replace(
                "{answer_kinds}",
                json.dumps(answer_kinds, ensure_ascii=False, separators=(",", ":")),
            )
        return rendered
