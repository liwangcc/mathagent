"""批次教学报告：从已完成的结构化批改结果生成轻量建议。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter

from services.analyzer import generate_report_text

logger = logging.getLogger(__name__)

_MAX_TOP_QUESTIONS = 5
_TEXT_LIMIT = 220


def _text(value, limit: int = _TEXT_LIMIT) -> str:
    value = " ".join(str(value or "").split())
    return value[:limit]


def _question_sort_key(question_no: str) -> tuple:
    chunks = re.split(r"(\d+)", str(question_no))
    return tuple(int(chunk) if chunk.isdigit() else chunk for chunk in chunks)


def _known(value) -> str:
    value = _text(value, 100)
    if not value or value in {"未知", "未指定", "未标注", "未分类", "综合"}:
        return ""
    return value


def _review_items(batch) -> list[dict]:
    """Return active results, excluding voided papers from teaching statistics."""
    items = batch.review_results or []
    if not items:
        items = batch.results or []
    return [item for item in items if str(item.get("disposition") or "active") != "voided"]


def _result_fallbacks(batch) -> dict[str, dict]:
    """Index the legacy row summaries used when a review has no details.

    A scribbled or otherwise unreadable answer can legitimately produce an
    aggregate result without per-question details.  The review table still
    has enough information to count that result, so the batch report should
    not collapse to an empty state in that case.
    """
    return {
        str(item.get("paper_id") or item.get("review_id") or ""): item
        for item in (getattr(batch, "results", None) or [])
        if isinstance(item, dict)
        and str(item.get("paper_id") or item.get("review_id") or "")
    }


def build_batch_report_payload(batch) -> dict:
    """Build deterministic statistics and a bounded model input payload.

    Only question text and already-produced explanations are retained as small
    examples. Student names, OCR images, raw answers and file paths are excluded.
    """
    papers = _review_items(batch)
    result_fallbacks = _result_fallbacks(batch)
    context = dict(getattr(batch, "teaching_context", None) or {})
    counts = Counter(correct=0, partial=0, wrong=0, needs_review=0)
    topic_attempts = Counter()
    topic_errors = Counter()
    question_attempts = Counter()
    question_judged = Counter()
    question_errors = Counter()
    question_texts: dict[str, str] = {}
    choice_total = choice_wrong = 0
    diagnostic_total = 0
    diagnostic_judged = 0
    diagnostic_errors = 0

    for review in papers:
        result = review.get("result") or {}
        details = [item for item in (result.get("details") or []) if isinstance(item, dict)]
        # Some failed visual reads have no safe answer box and therefore no
        # detail rows.  Preserve their already-determined aggregate counts so
        # scribbles and unreadable work remain visible in the batch statistics.
        if not details:
            paper_id = str(review.get("paper_id") or review.get("review_id") or "")
            fallback = result_fallbacks.get(paper_id) or {}
            result_count_sum = sum(
                max(0, int(result.get(f"{key}_count") or result.get(key) or 0))
                for key in counts
            )
            if not result.get("total") and result_count_sum <= 0 and fallback:
                result = fallback
            aggregate_total = int(result.get("total") or 0)
            aggregate_counts = {
                key: max(0, int(result.get(f"{key}_count") or result.get(key) or 0))
                for key in counts
            }
            aggregate_sum = sum(aggregate_counts.values())
            if aggregate_total and aggregate_sum < aggregate_total:
                aggregate_counts["needs_review"] += aggregate_total - aggregate_sum
            for key, value in aggregate_counts.items():
                counts[key] += value
            # Without detail rows the question type and per-question identity
            # are unknown; leave diagnostic/topic distributions empty rather
            # than inventing a classification.
            continue
        knowledge_items = review.get("knowledge_items") or []
        for index, detail in enumerate(details):
            if not isinstance(detail, dict):
                continue
            verdict = str(detail.get("result") or "needs_review").strip().lower()
            if verdict not in counts:
                verdict = "needs_review"
            counts[verdict] += 1
            question_no = str(
                detail.get("parent_question_no")
                or detail.get("question_no")
                or detail.get("question_number")
                or ""
            ).strip()
            question_no = question_no or "未标号题"
            question_type = str(detail.get("question_type") or "solve").strip().lower()
            is_choice = question_type == "choice"
            if is_choice:
                choice_total += 1
                if verdict in {"wrong", "partial"}:
                    choice_wrong += 1
            else:
                diagnostic_total += 1
                if verdict != "needs_review":
                    diagnostic_judged += 1
                if verdict in {"wrong", "partial"}:
                    diagnostic_errors += 1

            question_attempts[question_no] += 1
            question_judged[question_no] += verdict != "needs_review"
            question_text = _text(detail.get("question"))
            if question_text and not question_texts.get(question_no):
                question_texts[question_no] = question_text
            if verdict in {"wrong", "partial"}:
                question_errors[question_no] += 1

            meta = knowledge_items[index] if index < len(knowledge_items) else {}
            if not isinstance(meta, dict):
                meta = {}
            chapter = _known(meta.get("chapter"))
            topic = _known(meta.get("topic"))
            # Count every answer, labelled or not, so the unclassified error
            # rate is errors / answers like every other topic (not always 100%).
            topic_key = (chapter or "未分类", topic or "未分类")
            topic_attempts[topic_key] += 1
            if verdict in {"wrong", "partial"}:
                topic_errors[topic_key] += 1

    total = sum(counts.values())
    judged_total = total - counts["needs_review"]
    error_total = counts["wrong"] + counts["partial"]
    if judged_total <= 0:
        error_rate = None
    else:
        error_rate = round(error_total / judged_total, 4)
    diagnostic_error_rate = (
        round(diagnostic_errors / diagnostic_judged, 4)
        if diagnostic_judged
        else None
    )

    all_errors = []
    for question_no, error_count in question_errors.items():
        judged_count = question_judged[question_no]
        all_errors.append({
            "question_no": question_no,
            "error_count": error_count,
            "attempt_count": question_attempts[question_no],
            "judged_count": judged_count,
            "error_rate": round(error_count / max(judged_count, 1), 4),
            "question": question_texts.get(question_no, ""),
        })
    # Most errors first; ties by higher error rate, then question number, so
    # the order never depends on which paper happened to be read first.
    all_errors.sort(key=lambda item: (
        -item["error_count"], -item["error_rate"], _question_sort_key(item["question_no"]),
    ))
    top_errors = all_errors[:_MAX_TOP_QUESTIONS]

    knowledge_points = []
    for (chapter, topic), attempts in topic_attempts.items():
        errors = topic_errors[(chapter, topic)]
        knowledge_points.append({
            "name": topic,
            "chapter": chapter,
            "topic": topic,
            "attempt_count": attempts,
            "error_count": errors,
            "error_rate": round(errors / max(attempts, 1), 4),
        })
    # Rank by errors (what the list displays), not by how often a topic was
    # answered; on ties a labelled topic precedes the unclassified bucket,
    # then the higher error rate.
    knowledge_points.sort(key=lambda item: (
        -item["error_count"], item["topic"] == "未分类", -item["error_rate"],
        item["chapter"], item["topic"],
    ))

    mode = str(getattr(batch, "grade_mode", "precise") or "precise")
    identity_mode = str(getattr(batch, "identity_mode", "identified") or "identified")
    stats = {
        "teaching_context": context,
        "education_stage": context.get("education_stage", ""),
        "catalog_version": context.get("curriculum_version", ""),
        "mode": mode,
        "identity_mode": identity_mode,
        "paper_count": len(papers),
        "question_count": total,
        "correct_count": counts["correct"],
        "partial_count": counts["partial"],
        "wrong_count": counts["wrong"],
        "needs_review_count": counts["needs_review"],
        "error_rate": error_rate,
        "diagnostic_error_count": diagnostic_errors,
        "diagnostic_error_rate": diagnostic_error_rate,
        "choice_count": choice_total,
        "choice_wrong_count": choice_wrong,
        "diagnostic_question_count": diagnostic_total,
        "diagnostic_judged_count": diagnostic_judged,
        "choice_only": diagnostic_total == 0 and choice_total > 0,
        "all_errors": all_errors,
        "top_errors": top_errors,
        "knowledge_points": knowledge_points,
        "knowledge_distribution": knowledge_points,
        "unclassified_knowledge_count": sum(
            int(item["error_count"])
            for item in knowledge_points
            if item.get("topic") == "未分类"
        ),
    }
    encoded = json.dumps(stats, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "stats": stats,
        "input_hash": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    }


def _knowledge_label(item: dict) -> str:
    """Teacher-facing name: the missing-label bucket reads as a failed classification."""
    chapter = str(item.get("chapter") or "")
    topic = str(item.get("name") or item.get("topic") or "")
    if topic in {"", "未分类"}:
        return f"{chapter}·分类失败" if chapter not in {"", "未分类"} else "分类失败"
    return f"{chapter or '未分章'}·{topic}"


def _fallback_report(stats: dict) -> str:
    paper_count = stats.get("paper_count", 0)
    total = stats.get("question_count", 0)
    error_total = int(stats.get("wrong_count", 0)) + int(stats.get("partial_count", 0))
    needs_review = int(stats.get("needs_review_count", 0))
    unit = "页" if stats.get("identity_mode") == "anonymous" else "份试卷"
    lines = [f"本批次共 {paper_count} {unit}、{total} 道题，错题（含部分正确）{error_total} 道。"]
    points = [item for item in (stats.get("knowledge_points") or []) if item.get("error_count")]
    if points:
        lines.append("错题涉及知识点：" + "、".join(
            _knowledge_label(item) for item in points[:3]
        ) + "。")
    if stats.get("choice_only"):
        lines.append(
            f"其中选择题错 {stats.get('choice_wrong_count', 0)} 道。选择题结果仅作答题统计，"
            "不能单独作为知识点掌握情况的依据。"
        )
    elif not stats.get("diagnostic_judged_count"):
        lines.append("非选择题目前均待人工复核，暂不据此判断知识点掌握情况。")
    else:
        lines.append("建议先讲清高频知识点，再安排同类型变式题练习，并要求学生写出关键步骤。")
    if needs_review:
        lines.append(f"另有 {needs_review} 道题待人工复核，复核前不要把它们作为确定性结论。")
    return "\n".join(lines)


def _build_prompt(stats: dict) -> str:
    # The model needs representative evidence, not the full per-question report.
    # Keep the complete distribution in the durable report for the teacher while
    # bounding this separate narrative call's input size.
    from services.teaching_context import stage_prompt
    compact = {
        key: stats.get(key)
        for key in (
            "education_stage",
            "catalog_version",
            "mode",
            "identity_mode",
            "paper_count",
            "question_count",
            "correct_count",
            "partial_count",
            "wrong_count",
            "needs_review_count",
            "error_rate",
            "diagnostic_error_count",
            "diagnostic_error_rate",
            "choice_count",
            "choice_wrong_count",
            "diagnostic_question_count",
            "diagnostic_judged_count",
            "choice_only",
            "top_errors",
        )
    }
    compact["knowledge_points"] = (stats.get("knowledge_points") or [])[:8]
    instruction = (
        "你是一位中国高中数学老师。请根据下面已经确定的批改统计，生成一份给任课老师看的本批教学建议。\n"
        "只按知识点分析和展示，不输出、推测或展示错因、错误类型。\n"
        "只能使用统计中的数字和已标注知识点，不得修改数字，不得猜测未标注的知识点。\n"
        "选择题只能作为答题结果统计，不能据此断言学生掌握或不会某个知识点。\n"
        "报告控制在300字以内，使用四段：总体情况、主要问题、优先复习、课堂练习建议。\n"
        "如果没有非选择题证据，明确说明无法进行知识点诊断。\n\n"
        "批改统计：\n"
    )
    return stage_prompt(instruction, context=stats.get("teaching_context") or {}) + "\n" + json.dumps(compact, ensure_ascii=False, separators=(",", ":"))


async def generate_batch_report(stats: dict, *, teacher_id: int | str) -> tuple[str, str]:
    """Return ``(text, provider_note)``; always provide a usable report."""
    fallback = _fallback_report(stats)
    if stats.get("choice_only") or not stats.get("diagnostic_judged_count"):
        note = (
            "仅有选择题，已生成统计版报告"
            if stats.get("choice_only")
            else "非选择题均待复核，已生成统计版报告"
        )
        return fallback, note
    try:
        text = await generate_report_text(
            [{"role": "user", "content": _build_prompt(stats)}],
            teacher_id=f"teacher_{teacher_id}",
        )
        text = str(text or "").strip()
        if text:
            return text, ""
    except Exception as exc:
        logger.warning("batch teaching report model call failed: %s", exc)
    return fallback, "建议模型暂不可用，已显示统计版报告"
