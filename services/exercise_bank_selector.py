"""Deterministic exercise selection from the teacher-visible question bank.

This module deliberately contains no model calls.  It converts persisted bank
rows into the same ``ExerciseQuestion`` contract used by preview and DOCX export,
and rejects malformed rows instead of asking an LLM to repair or replace them.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from services.exercise_schema import (
    ExerciseQuestion,
    ExerciseTypeCounts,
    normalize_exercise_choice_answer,
)


_TYPE_ALIASES = {
    "choice": "choice",
    "选择": "choice",
    "选择题": "choice",
    "fill": "fill",
    "blank": "fill",
    "填空": "fill",
    "填空题": "fill",
    "solution": "solution",
    "solve": "solution",
    "解答": "solution",
    "解答题": "solution",
    "大题": "solution",
}
_TYPE_ORDER = ("choice", "fill", "solution")
_TYPE_LABELS = {"choice": "选择题", "fill": "填空题", "solution": "解答题"}
_OPTION_MARKER_RE = re.compile(
    r"(?<![A-Za-z0-9_])([A-D])(?:\s*([.．、:：])\s*|\s+(?=\S))",
    re.IGNORECASE,
)
_CHOICE_BLANK_RE = re.compile(r"(?:\(\s*\)|（\s*）)")
_EXPLICIT_FILL_RE = re.compile(r"_{3,}|＿{3,}|—{3,}|…{3,}")
_SUBPART_RE = re.compile(r"(?:^|[\s；;。])(?:[（(](?:[1-9]|[IVXivx]+)[)）])")
_SCORE_RE = re.compile(r"[（(]?\s*(\d+)\s*分\s*[)）]?")
_NUMBER_RE = re.compile(r"(?:^|】)\s*(\d+)\s*[.．、]?")


def _restore_option_separators(value: str) -> str:
    """Restore only escaped separators immediately preceding option labels.

    A global ``\\t``/``\\n`` replacement would corrupt LaTeX commands such as
    ``\\triangle``.  Restricting the replacement to A-D markers keeps maths intact.
    """

    text = str(value or "")
    return re.sub(
        r"\\[tn](?=\s*[A-D](?:\s*[.．、:：]|\s+))",
        "\n",
        text,
        flags=re.IGNORECASE,
    )


def _clean_option(value: str) -> str:
    return value.strip().strip("\t\r\n ").rstrip("；;")


def parse_choice_question(value: str) -> tuple[str, dict[str, str]] | None:
    """Split an embedded A-D choice question into stem and four options.

    OCR output varies between ``A. value``, ``A value`` and literal ``\\tB.``
    separators.  A valid result requires one ordered, non-empty A/B/C/D sequence;
    otherwise the row is not safe for a generated paper.
    """

    text = _restore_option_separators(value).strip()
    matches = list(_OPTION_MARKER_RE.finditer(text))
    by_label = {
        label: [match for match in matches if match.group(1).upper() == label]
        for label in "ABCD"
    }
    if any(not by_label[label] for label in "ABCD"):
        return None

    blank_positions = [match.end() for match in _CHOICE_BLANK_RE.finditer(text)]
    anchor = blank_positions[-1] if blank_positions else -1
    candidates: list[tuple[tuple[int, int, int, int], tuple[str, dict[str, str]]]] = []

    for a in by_label["A"]:
        for b in by_label["B"]:
            if b.start() <= a.end():
                continue
            for c in by_label["C"]:
                if c.start() <= b.end():
                    continue
                for d in by_label["D"]:
                    if d.start() <= c.end():
                        continue
                    stem = text[:a.start()].strip()
                    options = {
                        "A": _clean_option(text[a.end():b.start()]),
                        "B": _clean_option(text[b.end():c.start()]),
                        "C": _clean_option(text[c.end():d.start()]),
                        "D": _clean_option(text[d.end():]),
                    }
                    if not stem or any(not options[label] for label in "ABCD"):
                        continue
                    if any(len(options[label]) > 3000 for label in "ABCD"):
                        continue
                    strong_markers = sum(bool(match.group(2)) for match in (a, b, c, d))
                    after_blank = int(anchor >= 0 and a.start() >= anchor)
                    # Prefer a sequence after the printed answer blank, then the
                    # most explicit punctuation, then the earliest plausible A.
                    score = (after_blank, strong_markers, -a.start(), -(d.start() - a.start()))
                    candidates.append((score, (stem, options)))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _canonical_text(value: Any) -> str:
    text = str(value or "").strip()
    text = text.replace("−", "-").replace("－", "-").replace("–", "-")
    return re.sub(r"\s+", "", text).rstrip("。．、;；")


def _choice_answer(answer: Any, options: dict[str, str]) -> str:
    normalized = normalize_exercise_choice_answer(answer)
    if normalized:
        return normalized
    target = _canonical_text(answer)
    matches = [label for label, value in options.items() if _canonical_text(value) == target]
    return matches[0] if len(matches) == 1 else ""


def infer_question_type(question: str, answer: str = "") -> str:
    """Return a strictly supported type or an empty string when ambiguous."""

    text = _restore_option_separators(question).strip()
    parsed_choice = parse_choice_question(text)
    if parsed_choice and _choice_answer(answer, parsed_choice[1]):
        return "choice"

    if _EXPLICIT_FILL_RE.search(text):
        return "fill"

    subparts = len(_SUBPART_RE.findall(text))
    score_match = _SCORE_RE.search(text)
    score = int(score_match.group(1)) if score_match else None
    number_match = _NUMBER_RE.search(text)
    number = int(number_match.group(1)) if number_match else None

    if subparts >= 2 or (score is not None and score >= 8) or (number is not None and number >= 17):
        return "solution"
    if number is not None and 13 <= number <= 16 and (score is None or score <= 6):
        return "fill"
    return ""


def normalize_question_type(raw_type: Any, question: str = "", answer: str = "") -> str:
    normalized = _TYPE_ALIASES.get(str(raw_type or "").strip().lower(), "")
    if normalized == "choice":
        parsed = parse_choice_question(question)
        return "choice" if parsed and _choice_answer(answer, parsed[1]) else ""
    if normalized in {"fill", "solution"}:
        return normalized
    return infer_question_type(question, answer)


def _solution_text(row: Any) -> str:
    getter = getattr(row, "get_solution_steps", None)
    raw_steps = getter() if callable(getter) else getattr(row, "solution_steps", [])
    lines: list[str] = []
    if isinstance(raw_steps, list):
        for step in raw_steps:
            if isinstance(step, dict):
                value = (
                    step.get("description")
                    or step.get("content")
                    or step.get("step")
                    or step.get("text")
                    or ""
                )
            else:
                value = step
            cleaned = str(value or "").strip()
            if cleaned:
                lines.append(cleaned)
    elif raw_steps:
        lines.append(str(raw_steps).strip())
    return "\n".join(lines) or str(getattr(row, "answer", "") or "").strip()


def _build_exercise(row: Any) -> ExerciseQuestion | None:
    raw_question = str(getattr(row, "question", "") or "").strip()
    raw_answer = str(getattr(row, "answer", "") or "").strip()
    kind = normalize_question_type(
        getattr(row, "question_type", ""),
        raw_question,
        raw_answer,
    )
    if not kind or not raw_question or not raw_answer:
        return None

    options: dict[str, str] = {}
    question = raw_question
    answer = raw_answer
    if kind == "choice":
        parsed = parse_choice_question(raw_question)
        if not parsed:
            return None
        question, options = parsed
        answer = _choice_answer(raw_answer, options)
        if not answer:
            return None

    try:
        difficulty = max(1, min(5, int(getattr(row, "difficulty", 2) or 2)))
    except (TypeError, ValueError):
        difficulty = 2
    knowledge_point = str(
        getattr(row, "topic", "")
        or getattr(row, "chapter", "")
        or "综合"
    ).strip()

    try:
        return ExerciseQuestion(
            type=kind,
            question=question,
            options=options,
            answer=answer,
            solution=_solution_text(row),
            knowledge_point=knowledge_point,
            difficulty=difficulty,
        )
    except ValueError:
        return None


@dataclass(frozen=True)
class SelectedExercise:
    row: Any
    question: ExerciseQuestion


class QuestionBankShortage(ValueError):
    def __init__(self, requested: dict[str, int], available: dict[str, int]):
        self.requested = dict(requested)
        self.available = dict(available)
        missing = []
        for kind in _TYPE_ORDER:
            amount = max(0, self.requested.get(kind, 0) - self.available.get(kind, 0))
            if amount:
                missing.append(
                    f"{_TYPE_LABELS[kind]}缺 {amount} 道"
                    f"（可用 {self.available.get(kind, 0)} / 需要 {self.requested.get(kind, 0)}）"
                )
        super().__init__("题库可用题量不足：" + "；".join(missing))


def _priority_score(row: Any, priority_terms: Iterable[tuple[str, int]]) -> int:
    haystack = f"{getattr(row, 'chapter', '')}\n{getattr(row, 'topic', '')}".lower()
    return max(
        (int(weight) for term, weight in priority_terms if str(term or "").strip().lower() in haystack),
        default=0,
    )


def _quality_score(row: Any) -> int:
    getter = getattr(row, "get_solution_steps", None)
    raw_steps = getter() if callable(getter) else getattr(row, "solution_steps", [])
    return int(bool(raw_steps))


def select_exercises_from_candidates(
    candidates: Sequence[Any],
    counts: ExerciseTypeCounts,
    *,
    priority_terms: Sequence[tuple[str, int]] = (),
    rng: random.Random | random.SystemRandom | None = None,
) -> list[SelectedExercise]:
    """Select exact per-type counts from already scoped visible bank rows."""

    rng = rng or random.SystemRandom()
    grouped: dict[str, list[tuple[Any, ExerciseQuestion, int, int, float]]] = {
        kind: [] for kind in _TYPE_ORDER
    }
    seen_questions: set[str] = set()
    for row in candidates:
        question = _build_exercise(row)
        if question is None:
            continue
        normalized_stem = re.sub(r"\s+", "", question.question).lower()
        if normalized_stem in seen_questions:
            continue
        seen_questions.add(normalized_stem)
        grouped[question.type].append(
            (
                row,
                question,
                _priority_score(row, priority_terms),
                _quality_score(row),
                rng.random(),
            )
        )

    available = {kind: len(grouped[kind]) for kind in _TYPE_ORDER}
    requested = counts.as_dict()
    if any(available[kind] < requested[kind] for kind in _TYPE_ORDER):
        raise QuestionBankShortage(requested, available)

    selected: list[SelectedExercise] = []
    for kind in _TYPE_ORDER:
        ranked = sorted(
            grouped[kind],
            key=lambda item: (
                -item[2],
                -item[3],
                int(getattr(item[0], "usage_count", 0) or 0),
                item[4],
            ),
        )[:requested[kind]]
        ranked.sort(key=lambda item: item[1].difficulty)
        selected.extend(
            SelectedExercise(row=row, question=question)
            for row, question, _, _, _ in ranked
        )
    return selected


def filter_curriculum_scope(rows, stage, *, include_extension=False, explicit_topics=()):
    """New automatic selections need catalog classification.

    Core points and currently taught supplement points are drawn by default;
    other extension points need the opt-in or the teacher picking that topic.
    """
    if not stage:
        return rows
    from services.math_curriculum import get_knowledge_point, practice_eligible
    explicit = {str(topic or "").strip() for topic in explicit_topics} - {""}
    eligible = []
    for row in rows:
        point = get_knowledge_point(stage, row.chapter or "", row.topic or "")
        if point is not None and (
            point["topic"] in explicit
            or practice_eligible(point, include_extension=include_extension)
        ):
            eligible.append(row)
    return eligible


def select_exercises_from_bank(
    db: Any,
    *,
    user_id: int,
    counts: ExerciseTypeCounts,
    scope_terms: Sequence[str] = (),
    scope_term_groups: Sequence[Sequence[str]] = (),
    priority_terms: Sequence[tuple[str, int]] = (),
    rng: random.Random | random.SystemRandom | None = None,
    education_stage: str | None = None,
    include_extension: bool = False,
    explicit_topics: Sequence[str] = (),
) -> list[SelectedExercise]:
    """Query the visible bank, select a paper, and record successful usage."""
    from sqlalchemy import or_

    from models import QuestionBank
    from services.bank_scope import visible_filter
    from services.security import escape_like

    raw_groups = list(scope_term_groups) or [scope_terms]
    normalized_groups: list[list[str]] = []
    for raw_group in raw_groups:
        normalized_terms: list[str] = []
        for raw_term in raw_group:
            term = str(raw_term or "").strip()
            if term and term not in normalized_terms:
                normalized_terms.append(term)
        if normalized_terms:
            normalized_groups.append(normalized_terms)

    from services.practice import filter_bank_stage

    query = filter_bank_stage(
        db.query(QuestionBank).filter(visible_filter(user_id)), education_stage
    )
    for normalized_terms in normalized_groups:
        filters = []
        for term in normalized_terms:
            pattern = f"%{escape_like(term)}%"
            filters.extend(
                (
                    QuestionBank.chapter.like(pattern, escape="\\"),
                    QuestionBank.topic.like(pattern, escape="\\"),
                )
            )
        query = query.filter(or_(*filters))

    from services.teaching_context import current_teaching_context
    stage = education_stage or current_teaching_context().get("education_stage")
    rows = filter_curriculum_scope(
        query.all(), stage,
        include_extension=include_extension, explicit_topics=explicit_topics,
    )
    selected = select_exercises_from_candidates(
        rows,
        counts,
        priority_terms=priority_terms,
        rng=rng,
    )
    try:
        for item in selected:
            item.row.usage_count = int(item.row.usage_count or 0) + 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    return selected
