"""自动批改服务 — 试卷批改主流程

并发架构：
    1. 预处理所有题目（机械匹配）
    2. 非选择题提交独立的 qwen3.8-flash 文字数学判分请求
    3. 收集结果后逐题commit（保持崩溃安全性）
"""

import logging
import copy
import hashlib
import json
import re
from typing import Optional

from sqlalchemy.orm import Session

from models import QuestionBank
from services.analyzer import judge_paper, reset_grading_teacher_id, set_grading_teacher_id
from services.matchers.hash_matcher import HashMatcher
from services.matchers.trie_matcher import TrieMatcher
from services.numeric_answers import compare_final_numbers
from services.quick_grader import _compare_one
from services.task_queue import get_grading_queue, make_idempotency_key
from services.grading_persistence import image_content_hash
from services.scoring_engine import (
    aggregate_grading_result,
    build_grading_units,
    llm_payload_for_unit,
)
from services.grading_contract import VALID_GRADING_CATEGORIES
from services.teaching_context import current_teaching_context, await_teaching
from services.text_grading_context import build_teacher_material, validate_teacher_material

logger = logging.getLogger(__name__)


def get_grading_provider() -> str:
    """Return the configured grading route; config admits only qwen3.8-flash."""
    import config

    return str(config.GRADING_PROVIDER).strip().lower()


def validate_transcription_integrity(unit: dict, judgment: dict) -> bool:
    """Reject a text grader result that rewrites an explicitly echoed answer.

    The current structured response does not echo ``student_answer``. The
    absent field is therefore valid. If a future prompt/provider echoes it, it
    must exactly match the frozen OCR text instead of silently replacing a
    radical, fraction, sign, or other handwritten structure.
    """
    if not isinstance(judgment, dict) or "student_answer" not in judgment:
        return True
    return str(judgment.get("student_answer") or "").strip() == str(
        unit.get("student_answer") or ""
    ).strip()


NUMERIC_FINAL_ANSWER_METHOD = "numeric_final_answer"
NUMERIC_OCR_TOLERANCE_METHOD = "numeric_ocr_tolerance"
# OCR doubt keeps the teacher review even when the text looks numeric.
_UNCERTAIN_TRANSCRIPTION_MARKERS = ("无法识别", "?", "？")


def resolve_numeric_review(unit: dict, judgment: dict) -> dict | None:
    """Decide a text-model abstention whose final answers are plain numbers.

    The model owns every correct/partial/wrong verdict; this runs only when it
    returned needs_review on readable text.  The OCR tolerance decides first:
    a conclusion value that is a one-character misread of the student's own
    derived correct result (derived 25 4/5, transcribed 25 2/5) is correct.
    Otherwise the readable final values are graded as written against the
    confirmed reference (25 2/5 = 25.4 vs 25.8 is wrong).  Unreadable or
    doubtful OCR, slot identity errors, drawing sides and anything code cannot
    parse stay with the teacher.
    """
    if judgment.get("result") != "needs_review" or unit.get("slot_identity_error"):
        return None
    if unit["entry"].get("answer_kind") in {"drawing", "mixed"}:
        return None
    answer = str(unit.get("student_answer") or "")
    if (
        str(unit.get("answer_state") or "").strip().lower() in {"unreadable", "blank"}
        or any(marker in answer for marker in _UNCERTAIN_TRANSCRIPTION_MARKERS)
    ):
        return None
    comparison = compare_final_numbers(
        answer, str(unit.get("standard_answer") or ""), unit.get("answer_variants") or [],
    )
    if comparison is None:
        return None
    correct = comparison.result == "correct"
    return {
        **judgment,
        "result": comparison.result,
        "explanation": "" if correct else (
            "判分模型未给出结论，系统按最终数值判定：" + "；".join(comparison.mismatches)
        ),
        "hint": "",
        "model_answer": "",
        "standard_answer": "" if correct else unit["standard_answer"],
        "match_method": (
            NUMERIC_OCR_TOLERANCE_METHOD if comparison.ocr_tolerance else NUMERIC_FINAL_ANSWER_METHOD
        ),
    }


class GradingModelUnavailable(RuntimeError):
    """The mathematical judgment provider did not produce a valid result."""


class RetryableGradingFailure(RuntimeError):
    """A whole-paper model result was absent and may be tried one more time."""

    retryable = True


class GraderService:
    def __init__(self, db: Session, teacher_id: int | None = None):
        self.db = db
        self.teacher_id = teacher_id  # P0-4: 题库匹配/入库限定到该老师可见范围
        self.hash_index: Optional[HashMatcher] = None
        self.trie_index: Optional[TrieMatcher] = None
        self._build_indices()

    def _build_indices(self):
        self.hash_index = HashMatcher()
        self.trie_index = TrieMatcher()
        q = self.db.query(QuestionBank)
        if self.teacher_id is not None:
            from services.bank_scope import visible_filter
            q = q.filter(visible_filter(self.teacher_id))
        questions = q.all()
        for q in questions:
            key = q.question
            # Only confirmed/manual entries participate in hash (exact) matching.
            # Auto entries are only trie-indexed for fuzzy hints, not authoritative matching.
            if q.source != "auto":
                self.hash_index.put(key, q)
            self.trie_index.insert(key, q)


_ALIGNMENT_OFFSETS = (-2, -1, 1, 2)


def _measure_alignment_candidate(
    items: list[dict],
    offset: int,
    *,
    start: int = 0,
    end: int | None = None,
) -> dict:
    """Measure one fixed offset inside a contiguous objective-question window."""
    window_end = len(items) if end is None else end
    current_matches = 0
    shifted_matches = 0
    comparable = 0
    informative = 0
    disputed_question_numbers: list[str] = []
    for source_index in range(start, window_end):
        target_index = source_index + offset
        if target_index < start or target_index >= window_end:
            continue
        source = items[source_index]
        target = items[target_index]
        student_answer = source["student_answer"]
        if (
            not student_answer
            or student_answer == "[无法识别]"
            or source["question_type"] != target["question_type"]
            or (
                source["question_type"] == "choice"
                and source["choice_mode"] != target["choice_mode"]
            )
        ):
            continue
        comparable += 1
        if (
            source["standard_answer"].replace(" ", "").upper()
            != target["standard_answer"].replace(" ", "").upper()
        ):
            informative += 1
        current_correct = _compare_one(
            student_answer,
            source["standard_answer"],
            source["answer_variants"],
            question_type=source["question_type"],
            choice_mode=source["choice_mode"],
        ) == "correct"
        shifted_correct = _compare_one(
            student_answer,
            target["standard_answer"],
            target["answer_variants"],
            question_type=target["question_type"],
            choice_mode=target["choice_mode"],
        ) == "correct"
        if current_correct:
            current_matches += 1
        if shifted_correct:
            shifted_matches += 1
        if current_correct != shifted_correct:
            disputed_question_numbers.append(source["question_number"])

    return {
        "offset": offset,
        "window_start": start,
        "window_end": window_end,
        "comparable": comparable,
        "informative": informative,
        "current_matches": current_matches,
        "shifted_matches": shifted_matches,
        "improvement": shifted_matches - current_matches,
        "disputed_question_numbers": disputed_question_numbers,
    }


def _is_strong_alignment_candidate(
    candidate: dict,
    *,
    min_comparable: int = 8,
    min_informative: int = 6,
    min_improvement: int = 4,
    improvement_percent: int = 25,
    shifted_percent: int = 65,
) -> bool:
    comparable = candidate["comparable"]
    required_improvement = max(
        min_improvement,
        (comparable * improvement_percent + 99) // 100,
    )
    required_shifted = (comparable * shifted_percent + 99) // 100
    return bool(
        comparable >= min_comparable
        and candidate["informative"] >= min_informative
        and candidate["improvement"] >= required_improvement
        and candidate["shifted_matches"] >= required_shifted
    )


def _best_alignment_candidate(candidates: list[dict]) -> dict | None:
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda candidate: (
            candidate["improvement"],
            candidate["shifted_matches"] / candidate["comparable"],
            candidate["shifted_matches"],
            -abs(candidate["offset"]),
        ),
    )


def _detect_answer_alignment_shift(
    questions: list[dict],
    answer_map: dict,
) -> dict | None:
    """Detect a strong neighboring-answer pattern without remapping answers."""
    items: list[dict] = []
    for question in questions:
        question_no = str(question.get("question_no") or "").strip()
        entry = answer_map.get(question_no, {}) or {}
        question_type = str(
            entry.get("question_type") or question.get("question_type") or ""
        ).strip().lower()
        standard_answer = str(entry.get("standard_answer") or "").strip()
        if (
            question_type not in {"choice", "fill"}
            or not entry.get("matched")
            or not standard_answer
        ):
            continue
        items.append({
            "question_number": question_no,
            "question_type": question_type,
            "choice_mode": str(entry.get("choice_mode") or "unknown").strip().lower(),
            "student_answer": str(question.get("student_answer") or "").strip(),
            "standard_answer": standard_answer,
            "answer_variants": entry.get("answer_variants", []) or [],
        })

    whole_candidates = []
    for offset in _ALIGNMENT_OFFSETS:
        candidate = _measure_alignment_candidate(items, offset)
        if _is_strong_alignment_candidate(candidate):
            candidate["scope"] = "whole"
            whole_candidates.append(candidate)
    best = _best_alignment_candidate(whole_candidates)
    if best is not None:
        return best

    # A blank answer can make a model shift every later answer while the prefix
    # remains correctly aligned.  Scan only long suffixes and require stronger
    # evidence than the whole-page gate to avoid reacting to short local errors.
    suffix_candidates = []
    for start in range(1, len(items)):
        if len(items) - start < 9:
            continue
        for offset in _ALIGNMENT_OFFSETS:
            candidate = _measure_alignment_candidate(
                items,
                offset,
                start=start,
            )
            if _is_strong_alignment_candidate(
                candidate,
                min_improvement=5,
                improvement_percent=40,
                shifted_percent=75,
            ):
                candidate["scope"] = "suffix"
                suffix_candidates.append(candidate)
    return _best_alignment_candidate(suffix_candidates)


async def grade_paper_unified(
    paper: dict, answer_map: dict, student_name: str, teacher_id: int,
    detail_level: str = "detailed",
    category: str = "daily",
) -> dict:
    """统一批阅：选择题本地比较，非选择题按 provider 消费判分。

    （从 routers/grade.py 的 _grade_paper 迁入，逻辑不变。属业务逻辑，应在 service 层。）

    - choice 题：机械比对（_compare_one），不调用判分模型。
    - 独立文字判分：识别完成后调用 judge_paper（qwen3.8-flash）。

    参考答案取老师确认后的 answer_map（题库匹配或手填），无答案时让 LLM 独立判断。
    返回 dict：{student_name, score, correct/partial/wrong/needs_review_count, total, details}；
    details 仅在需要教师复核时携带技术诊断。
    """
    category = str(category or "daily").strip().lower()
    if category not in VALID_GRADING_CATEGORIES:
        raise ValueError("category must be daily or exam")
    questions = paper.get("questions", [])
    units = build_grading_units(questions, answer_map, category)
    judgments: dict[str, dict] = {}

    alignment_shift = None
    alignment_review_qns: set[str] = set()
    if paper.get("ocr_answers_only") and not paper.get("ocr_alignment_verified"):
        alignment_shift = _detect_answer_alignment_shift(questions, answer_map)
    if alignment_shift:
        logger.warning(
            "answer alignment shift suspected student=%s offset=%s current=%s shifted=%s",
            student_name,
            alignment_shift["offset"],
            alignment_shift["current_matches"],
            alignment_shift["shifted_matches"],
        )
        alignment_review_qns = {
            str(question_no).strip()
            for question_no in alignment_shift.get("disputed_question_numbers", [])
            if str(question_no).strip()
        }
        for unit in units:
            if str(unit.get("parent_question_no") or "").strip() not in alignment_review_qns:
                continue
            judgments[unit["key"]] = {
                "result": "needs_review",
                "explanation": "本题答案与相邻题号的归属仍有冲突，请老师对照原卷确认",
                "hint": "",
                "model_answer": "",
                "standard_answer": unit["standard_answer"],
                "error_type": "ocr_answer_alignment_suspected",
            }

    # A page outside every confirmed page type has no trustworthy question-to-
    # rubric ownership. Preserve it as unresolved evidence; never reinterpret a
    # routing failure as student mistakes. Other papers are graded independently.
    if paper.get("unexpected_page_type"):
        reason = paper.get("page_type_error") or "未匹配本批次已确认页型"
        for unit in units:
            judgments[unit["key"]] = {
                "result": "needs_review", "explanation": reason, "hint": "请老师确认页型后再判定本题",
                "model_answer": "", "standard_answer": unit["standard_answer"],
                "error_type": "unexpected_page_type",
            }
        result = aggregate_grading_result(units, judgments, category, student_name)
        for detail in result["details"]:
            detail["match_method"] = "page_type_gate"
        # A page the teacher marked question_only/non_gradable is expected and
        # carries no questions: it is not graded, which is not a routing error.
        result["page_type_status"] = "not_graded" if paper.get("page_not_graded") else "needs_review"
        result["page_type_error"] = reason
        return result

    llm_questions = []
    llm_units: list[dict] = []
    model_question_no_by_key: dict[str, str] = {}
    drawing_by_key: dict[str, dict] = {}
    numeric_resolved: dict[str, str] = {}
    # A failed page carries the diagnostic id of its OCR failure; show it where
    # the teacher reviews the placeholder so the failure stays traceable.
    ocr_error_info = paper.get("error_info") if isinstance(paper.get("error_info"), dict) else {}
    ocr_correlation_id = str(ocr_error_info.get("correlation_id") or "")
    ocr_failure_reference = (
        f"（诊断编号 {ocr_correlation_id}）"
        if re.fullmatch(r"[a-f0-9]{32}", ocr_correlation_id) else ""
    )
    # Keep a content-free event even when a later resume replaces the review.
    # A technical evidence gate is not a Flash verdict or proof of illegible ink.
    rejected_sources: dict[str, int] = {}
    for unit in units:
        identity_error = str(unit.get("slot_identity_error") or "").strip()
        source = (
            "ocr_incomplete" if identity_error == "ocr_failed"
            else "slot_contract" if identity_error
            else "transcription_unreadable" if (
                str(unit.get("answer_state") or "").lower() == "unreadable"
                or str(unit.get("student_answer") or "").strip() == "[无法识别]"
            ) and unit["entry"].get("answer_kind") != "drawing"
            else ""
        )
        if source:
            rejected_sources[source] = rejected_sources.get(source, 0) + 1
    if rejected_sources:
        paper_id = str(paper.get("paper_id") or "")
        logger.warning(json.dumps({
            "event": "grading.evidence_rejected",
            "stage": "grading_input",
            "paper_id": paper_id if re.fullmatch(r"[a-f0-9]{12,32}", paper_id) else "",
            "source_counts": rejected_sources,
            "rejected_units_sent_to_grader": False,
        }, ensure_ascii=False, sort_keys=True))
    for unit in units:
        if unit["key"] in judgments:
            continue
        if unit.get("slot_identity_error"):
            identity_error = str(unit["slot_identity_error"]).strip()
            judgments[unit["key"]] = {
                "result": "needs_review",
                "explanation": (
                    "本题所在页 OCR 未完成，未计分；请老师对照原卷补录该题" + ocr_failure_reference
                    if identity_error == "ocr_failed"
                    else "作答位身份无法确认，请老师对照原卷确认"
                ),
                "standard_answer": unit["standard_answer"],
            }
            continue
        answer_kind = unit["entry"].get("answer_kind")
        if answer_kind in {"drawing", "mixed"}:
            from services.drawing_grading import bound_assessment
            drawing = bound_assessment(paper, unit)
            drawing_by_key[unit["key"]] = drawing
            # Graphic-only blank transcription says nothing about the drawing.
            if answer_kind == "drawing":
                judgments[unit["key"]] = {
                    "result": drawing["decision"], "explanation": drawing["reason"],
                    "error_type": ("" if drawing["decision"] == "correct" else
                                   "drawing_pixel_mismatch" if drawing["decision"] == "wrong" else "drawing_review"),
                    "standard_answer": unit["standard_answer"], "drawing_assessment": drawing,
                }
                continue
        # OCR can signal unreadable handwriting through answer_state even when
        # the transcription is empty. Treat that text side as unresolved so a
        # mixed drawing/text item cannot be downgraded to "wrong" by the LLM.
        if (
            answer_kind != "drawing"
            and (
                str(unit.get("student_answer") or "").strip() == "[无法识别]"
                or str(unit.get("answer_state") or "").strip().lower() == "unreadable"
            )
        ):
            judgments[unit["key"]] = {
                "result": "needs_review",
                "explanation": "学生答案笔迹无法可靠识别，请老师确认",
                "hint": "",
                "model_answer": "",
                "standard_answer": unit["standard_answer"],
                "error_type": "ocr_unreadable",
            }
            continue
        if unit.get("slot_id") and unit.get("answer_state") == "blank":
            judgments[unit["key"]] = {
                "result": "wrong", "explanation": "本作答位未作答",
                "standard_answer": unit["standard_answer"],
            }
            continue
        if unit["question_type"] == "choice":
            entry = unit["entry"]
            standard = entry.get("standard_answer") if entry.get("matched") else None
            verdict = _compare_one(
                unit["student_answer"], standard, entry.get("answer_variants", []) or [],
                question_type="choice",
                choice_mode=str(entry.get("choice_mode") or "unknown").strip().lower(),
            )
            judgments[unit["key"]] = {
                "result": verdict, "explanation": "", "hint": "",
                "model_answer": "", "standard_answer": standard or "", "error_type": "",
            }
        else:
            payload = llm_payload_for_unit(unit)
            if (
                unit["key"] in model_question_no_by_key
                or payload["question_no"] in model_question_no_by_key.values()
            ):
                raise GradingModelUnavailable("批改题号与冻结作答位不能唯一对应")
            model_question_no_by_key[unit["key"]] = payload["question_no"]
            llm_units.append(unit)
            if answer_kind == "mixed":
                payload["scope_hint"] = "text_only"
            llm_questions.append(payload)

    # Non-choice units go to the independent qwen3.8-flash text grader.
    if llm_questions:
        provider = get_grading_provider()
        # Scoring prompt changes participate in idempotency so an old verdict
        # cannot be replayed after the grading contract changes.
        queue = get_grading_queue()
        queue_teacher_id = f"teacher_{teacher_id or 0}"
        # A retention-expired or missing image must not collapse the key to
        # a display name that other pages share (anonymous "匿名页 N").
        image_key = (
            image_content_hash(paper.get("image_path", ""))
            or str(paper.get("working_sha256") or "").strip()
            or (f"paper:{paper['paper_id']}" if paper.get("paper_id") else "")
            or f"{student_name}:{paper.get('image_path', '')}"
        )
        teacher_material = validate_teacher_material(
            build_teacher_material(answer_map, units), llm_questions,
        )
        rubric_payload = {"questions": llm_questions, "teacher_material": teacher_material}
        teaching_context = current_teaching_context()
        if teaching_context:
            rubric_payload["teaching_context"] = teaching_context
        from services.flash_grading import (
            FLASH_PARAMETERS,
            FLASH_TEXT_PROMPT_VERSION,
            FLASH_TEXT_REASONING_EFFORT,
        )
        # A new effort/model must not reuse a verdict from the old run.
        rubric_payload["model_parameters"] = {
            **FLASH_PARAMETERS, "reasoning_effort": FLASH_TEXT_REASONING_EFFORT,
        }
        rubric_json = json.dumps(rubric_payload, ensure_ascii=False, sort_keys=True)
        rubric_hash = hashlib.sha256(rubric_json.encode("utf-8")).hexdigest()[:16]
        prompt_version = FLASH_TEXT_PROMPT_VERSION
        # Carry the same frozen references/policy through review and archive.
        # Do not reuse the queue hash: it also binds student OCR and subsets.
        context_payload = {
            "teacher_material": teacher_material,
            "teaching_context": teaching_context,
            "provider": provider,
            "prompt_version": prompt_version,
            "model_parameters": rubric_payload.get("model_parameters"),
        }
        context_hash = hashlib.sha256(json.dumps(
            context_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        for unit in units:
            unit["text_grading_context_hash"] = context_hash
        rubric_hash = f"{prompt_version}:{rubric_hash}"
        idem_key = make_idempotency_key(
            f"{image_key}:{category}:{rubric_hash}", detail_level,
        )

        async def _judge_for_teacher() -> dict:
            token = set_grading_teacher_id(queue_teacher_id)
            try:
                judged = await await_teaching(teaching_context, judge_paper(
                    llm_questions,
                    detail_level=detail_level,
                    teacher_material=teacher_material,
                ))
                if getattr(judged, "__task_queue_cacheable__", True) is False:
                    original_error = getattr(judged, "processing_error", None)
                    if original_error is not None:
                        raise RetryableGradingFailure("whole-paper grading failed") from original_error
                    raise RetryableGradingFailure(
                        "whole-paper grading did not produce a usable result"
                    )
                return judged
            finally:
                reset_grading_teacher_id(token)

        try:
            judged = await queue.submit(
                coro_factory=_judge_for_teacher,
                idempotency_key=idem_key,
                teacher_id=queue_teacher_id,
            )
        except Exception as exc:
            raise GradingModelUnavailable("数学批改模型暂不可用") from exc
        if getattr(judged, "__task_queue_cacheable__", True) is False:
            logger.error(
                "整卷 judge_paper 未产生有效模型结果 student=%s",
                student_name,
            )
            raise GradingModelUnavailable("数学批改模型暂不可用")
        for unit in llm_units:
            # Map the exact frozen wire ID back, never by position or by
            # asking a model to infer that 5[1] means legacy score key 5.
            judgment = judged.get(model_question_no_by_key[unit["key"]])
            if not isinstance(judgment, dict):
                judgments[unit["key"]] = {
                    "result": "needs_review",
                    "explanation": "数学判分结果缺少本题，请老师确认",
                    "hint": "",
                    "model_answer": "",
                    "standard_answer": unit["standard_answer"],
                    "error_type": "",
                }
                continue
            if not validate_transcription_integrity(unit, judgment):
                logger.warning(
                    "text grader changed OCR transcription student=%s question=%s",
                    student_name,
                    unit["key"],
                )
                judgments[unit["key"]] = {
                    "result": "needs_review",
                    "explanation": "判分模型返回的学生答案与冻结OCR转录不一致，请老师确认原卷",
                    "hint": "",
                    "model_answer": "",
                    "standard_answer": unit["standard_answer"],
                    "error_type": "",
                }
                continue
            # The queue owns its cached text result. Graphic merging must
            # not mutate it or poison a later teacher-adjusted rerun.
            judgments[unit["key"]] = copy.deepcopy(judgment)
            resolved = resolve_numeric_review(unit, judgments[unit["key"]])
            if resolved is not None:
                logger.info(
                    "numeric final answer decided model abstention student=%s question=%s result=%s method=%s",
                    student_name,
                    unit["key"],
                    resolved["result"],
                    resolved["match_method"],
                )
                judgments[unit["key"]] = resolved
                numeric_resolved[unit["key"]] = resolved["match_method"]

    for unit in units:
        drawing = drawing_by_key.get(unit["key"])
        if drawing is None:
            continue
        judgment = judgments[unit["key"]]
        judgment["drawing_assessment"] = drawing
        if unit["entry"].get("answer_kind") == "mixed":
            judgment["text_assessment"] = {key: judgment.get(key) for key in ("result", "explanation", "error_type")}
            # One verdict per side. Full pass is a pass, full miss is a miss, and
            # a determinate one-of-a-kind conflict is half credit -- the same fold
            # the multi-blank path applies, without inventing per-aspect weights.
            # Valid all-candidate pixel rejection stays a definite wrong for the
            # drawing side itself; only a side that cannot be decided at all
            # reaches the teacher.
            drawing_side = str(drawing["decision"])
            text_side = str(judgment.get("result") or "needs_review")
            if "needs_review" in (drawing_side, text_side):
                judgment["result"] = "needs_review"
                judgment["explanation"] = "图文混合题尚有分项无法确认，请老师结合分项结果核定分数"
                judgment["error_type"] = "drawing_mixed_review"
            elif drawing_side == "correct" and text_side == "correct":
                judgment["result"] = "correct"
            elif drawing_side == "wrong" and text_side == "wrong":
                judgment["result"] = "wrong"
                judgment["explanation"] = drawing["reason"]
                judgment["error_type"] = "drawing_pixel_mismatch"
            else:
                judgment["result"] = "partial"
                text_label = {"correct": "正确", "partial": "半对", "wrong": "错误"}.get(text_side, text_side)
                judgment["explanation"] = (
                    f"作图{'正确' if drawing_side == 'correct' else '错误'}；"
                    f"文字/计算部分{text_label}，整题按半对处理")
                judgment["error_type"] = ("drawing_pixel_mismatch" if drawing_side == "wrong"
                                          else str(judgment.get("error_type") or ""))

    result = aggregate_grading_result(units, judgments, category, student_name)
    # Audit which verdicts came from the numeric backstop, not the model.
    for detail in result["details"] if numeric_resolved else []:
        if str(detail.get("question_no") or "") in numeric_resolved:
            detail["match_method"] = numeric_resolved[str(detail["question_no"])]
        for graded in detail.get("grading_units") or []:
            if graded.get("question_no") in numeric_resolved:
                graded["match_method"] = numeric_resolved[graded["question_no"]]
    if alignment_review_qns:
        for detail in result["details"]:
            question_no = str(
                detail.get("parent_question_no") or detail.get("question_no") or ""
            ).strip()
            if question_no in alignment_review_qns:
                detail["error_type"] = "ocr_answer_alignment_suspected"
                detail["match_method"] = "alignment_gate"
        result["alignment_review_count"] = len(alignment_review_qns)
    return result
