"""Immutable question-attempt projection and correction state machine."""

from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from typing import Iterable

from sqlalchemy import case, func
from sqlalchemy.orm import Session
from services.teaching_context import current_teaching_context, make_teaching_context, validate_teaching_context
from services.math_curriculum import get_knowledge_point

from models import GradingResult, KnowledgeEvent, Student, sanitize_error_type
from models_correction import (
    CorrectionEvidence,
    QuestionAttemptEvent,
    QuestionCorrectionRelation,
    StudentObservationPeriod,
    TemplateQuestionAuthorization,
    WrongQuestionCycle,
)


_WRONG_RESULTS = {"wrong", "partial", "blank"}
_VALID_RESULTS = _WRONG_RESULTS | {"correct", "needs_review"}
_UNKNOWN_KNOWLEDGE = {"", "未知", "未标注", "综合"}


def resolve_grading_teaching_context(details, teaching_context=None) -> dict:
    """Use only frozen task inputs, never a teacher's mutable profile/context."""
    snapshots = [validate_teaching_context(row["teaching_context"])
                 for row in (details or [])
                 if isinstance(row, dict) and "teaching_context" in row]
    if teaching_context is not None:
        snapshots.append(validate_teaching_context(teaching_context))
    if not snapshots:
        return {}
    if any(value != snapshots[0] for value in snapshots[1:]):
        raise ValueError("批阅详情包含冲突的学段快照")
    return dict(snapshots[0])


def grading_result_teaching_context(result: GradingResult) -> dict:
    """Revisions/repairs inherit the archived result, not the operator profile."""
    stage = str(getattr(result, "education_stage", "") or "")
    version = str(getattr(result, "catalog_version", "") or "")
    if not stage:
        if version:
            raise ValueError("旧记录包含无法归属的知识点目录版本")
        return {}
    context = make_teaching_context(stage)
    if context["curriculum_version"] != version:
        raise ValueError("归档知识点目录版本不可用，请人工迁移后重试")
    return context


def attempt_teaching_context(db: Session, attempt: QuestionAttemptEvent | None) -> dict:
    """Read adopted archive metadata without rewriting a frozen attempt/hash.

    Explicit nonempty snapshots remain authoritative. Only legacy snapshots
    may fall back to their own archived result, never the current teacher.
    """
    if attempt is None:
        return {}
    snapshot = attempt.question_snapshot()
    frozen = validate_teaching_context(snapshot.get("teaching_context"))
    if frozen:
        return frozen
    if snapshot.get("education_stage"):
        context = make_teaching_context(snapshot["education_stage"])
        if snapshot.get("catalog_version") != context["curriculum_version"]:
            raise ValueError("归档题目知识点目录版本不可用")
        return context
    if snapshot.get("catalog_version"):
        raise ValueError("旧题目包含无法归属的知识点目录版本")
    result = db.get(GradingResult, attempt.grading_result_id)
    return grading_result_teaching_context(result) if result is not None else {}


def resolve_archived_teaching_context(details, result: GradingResult) -> dict:
    """Allow adopted empty details while rejecting real frozen-stage conflicts."""
    context = grading_result_teaching_context(result)
    explicit = [row for row in (details or [])
                if isinstance(row, dict) and validate_teaching_context(row.get("teaching_context"))]
    return resolve_grading_teaching_context(explicit, context)


def stamp_teaching_snapshot(mapping: dict, context: dict) -> None:
    if not context:
        return
    mapping["teaching_context"] = dict(context)
    mapping["education_stage"] = context["education_stage"]
    mapping["catalog_version"] = context["curriculum_version"]
    point = get_knowledge_point(context["education_stage"], mapping.get("chapter"),
                               mapping.get("topic") or mapping.get("knowledge_point"))
    mapping["knowledge_point_id"] = point["id"] if point else ""

# Historical grading paths used both English contract values and Chinese UI
# labels.  Keep the conversion in this module so every projection uses one
# state vocabulary without changing the original grading-result JSON.
_RESULT_ALIASES = {
    "wrong": "wrong",
    "incorrect": "wrong",
    "error": "wrong",
    "错误": "wrong",
    "错": "wrong",
    "答错": "wrong",
    "不正确": "wrong",
    "partial": "partial",
    "partiallycorrect": "partial",
    "partialcorrect": "partial",
    "部分对错": "partial",
    "部分正确": "partial",
    "完全正确": "correct",
    "答对": "correct",
    "部分对": "partial",
    "半对": "partial",
    "blank": "blank",
    "empty": "blank",
    "空白": "blank",
    "未作答": "blank",
    "未答": "blank",
    "未填写": "blank",
    "漏答": "blank",
    "notanswered": "blank",
    "unanswered": "blank",
    "correct": "correct",
    "right": "correct",
    "正确": "correct",
    "对": "correct",
    "needsreview": "needs_review",
    "review": "needs_review",
    "待复核": "needs_review",
    "待审核": "needs_review",
    "无法判断": "needs_review",
    "void": "void",
    "作废": "void",
    "已作废": "void",
}


def normalize_attempt_result(value) -> str:
    """Return the persisted question-level result vocabulary.

    Unknown or missing labels deliberately become ``needs_review``.  A repair
    job must never infer a wrong answer from an unrecognised legacy string.
    """
    if isinstance(value, dict):
        for key in ("result", "status", "judgment", "verdict"):
            if value.get(key) not in (None, ""):
                value = value[key]
                break
    token = str(value or "").strip().lower()
    token = re.sub(r"[\s_\-]+", "", token)
    return _RESULT_ALIASES.get(token, "needs_review")


def _detail_result(detail: dict) -> str:
    """Read a legacy detail verdict without treating an absent value as wrong."""
    for key in ("result", "status", "judgment", "verdict"):
        if detail.get(key) not in (None, ""):
            return normalize_attempt_result(detail[key])
    return "needs_review"


def _normalize_math_text(value) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("−", "-").replace("–", "-").replace("—", "-")
    text = text.replace("≤", "<=").replace("≥", ">=").replace("≠", "!=")
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[，。；：、,.!?！？;:]", "", text)
    return text


def _normalized_string_values(value) -> list[str]:
    if isinstance(value, dict):
        items = [value[key] for key in sorted(value, key=lambda key: str(key))]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    elif value in (None, ""):
        items = []
    else:
        items = [value]
    return [_normalize_math_text(item) for item in items if _normalize_math_text(item)]


def _answer_slots(detail: dict) -> list[str]:
    explicit = _normalized_string_values(detail.get("answer_slots"))
    if explicit:
        return explicit
    blank_results = detail.get("blank_results") or []
    if isinstance(blank_results, list):
        return [
            normalized
            for item in blank_results
            if isinstance(item, dict)
            for normalized in [_normalize_math_text(item.get("standard_answer"))]
            if normalized
        ]
    return []


def question_fingerprint(detail: dict) -> str:
    """Strict content fingerprint; excludes numbering/layout but keeps math changes."""
    payload = {
        "question": _normalize_math_text(detail.get("question") or detail.get("question_text")),
        "question_type": str(detail.get("question_type") or "solve").strip().lower(),
        "standard_answer": _normalize_math_text(
            detail.get("standard_answer") or detail.get("correct_answer")
        ),
        "answer_variants": sorted(
            _normalize_math_text(value) for value in (detail.get("answer_variants") or [])
        ),
        "solution_steps": [
            _normalize_math_text(value) for value in (detail.get("solution_steps") or [])
        ],
        "options": _normalized_string_values(detail.get("options")),
        "subquestion_count": int(detail.get("subquestion_count") or 1),
        "blank_count": int(detail.get("blank_count") or 1),
        "answer_slots": _answer_slots(detail),
        "figure_identity": _normalize_math_text(
            detail.get("figure_fingerprint")
            or detail.get("diagram_fingerprint")
            or detail.get("figure_description")
            or detail.get("diagram_description")
        ),
    }
    context = resolve_grading_teaching_context([detail])
    if context:
        payload["teaching_context"] = context
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def question_identity(detail: dict, meta: dict | None = None) -> tuple[str, str, str]:
    meta = meta or {}
    bank_id = meta.get("question_bank_id") or detail.get("question_bank_id")
    fingerprint = question_fingerprint(detail)
    context = resolve_grading_teaching_context([detail])
    prefix = (f"{context['education_stage']}:{context['curriculum_version']}:" if context else "")
    if bank_id:
        return f"{prefix}bank:{int(bank_id)}", "question_bank_id", fingerprint
    template_key = str(meta.get("template_question_id") or detail.get("template_question_id") or "").strip()
    if template_key:
        return f"{prefix}template:{template_key}", "template_question_id", fingerprint
    return f"fingerprint:{fingerprint}", "fingerprint", fingerprint


def _aggregate_result(rows: list[dict]) -> str:
    results = {_detail_result(row) for row in rows}
    if "needs_review" in results or any(result not in _VALID_RESULTS for result in results):
        return "needs_review"
    if results and results <= {"correct"}:
        return "correct"
    if results == {"blank"}:
        return "blank"
    if results <= _WRONG_RESULTS:
        return "wrong" if results <= {"wrong", "blank"} else "partial"
    return "partial"


def _combined_snapshot(parent_no: str, rows: list[dict]) -> tuple[dict, dict]:
    first = dict(rows[0])
    question_snapshot = {
        "question_no": parent_no,
        "question_text": first.get("question") or first.get("question_text") or "",
        "question_type": first.get("question_type") or "solve",
        "standard_answer": first.get("standard_answer") or first.get("correct_answer") or "",
        "answer_variants": first.get("answer_variants") or [],
        "solution_steps": first.get("solution_steps") or [],
        "answer_slots": first.get("answer_slots") or [
            item.get("standard_answer")
            for item in (first.get("blank_results") or [])
            if isinstance(item, dict) and item.get("standard_answer") not in (None, "")
        ],
        "options": first.get("options") or [],
        "figure_fingerprint": first.get("figure_fingerprint") or first.get("diagram_fingerprint") or "",
        "chapter": first.get("chapter") or "",
        "topic": first.get("topic") or first.get("knowledge_point") or "",
        "knowledge_point": first.get("knowledge_point") or first.get("topic") or "",
        "difficulty": first.get("difficulty") or 3,
        "error_type": sanitize_error_type(first.get("error_type")),
        "subquestion_count": len(rows) if any(row.get("subquestion_no") for row in rows) else int(first.get("subquestion_count") or 1),
        "blank_count": int(first.get("blank_count") or 1),
        "parts": [
            {
                "question_no": row.get("question_no") or row.get("question_number"),
                "subquestion_no": row.get("subquestion_no") or "",
                "result": row.get("result"),
            }
            for row in rows
        ],
    }
    answer_snapshot = {
        "student_answer": "；".join(
            str(row.get("student_answer") or "").strip() for row in rows
        ),
        "parts": [
            {
                "subquestion_no": row.get("subquestion_no") or "",
                "student_answer": row.get("student_answer") or "",
                "result": row.get("result") or "needs_review",
            }
            for row in rows
        ],
    }
    return question_snapshot, answer_snapshot


def _stable_question_time(grading_result: GradingResult) -> datetime:
    return (
        getattr(grading_result, "created_at", None)
        or getattr(grading_result, "updated_at", None)
        or datetime.now()
    )


def _projection_groups(
    details: Iterable[dict] | None,
    *,
    strict: bool = False,
) -> tuple[list[tuple[str, list[dict]]], str | None]:
    """Return canonical top-level question groups.

    ``strict`` is used by the historical repair job.  It refuses malformed or
    incomplete rows instead of guessing a question, while the normal archive
    path keeps its long-standing behaviour of ignoring non-dict rows.
    """
    if not isinstance(details, list) or not details:
        return [], "missing_details"

    normalized: list[dict] = []
    for index, detail in enumerate(details):
        if not isinstance(detail, dict):
            if strict:
                return [], "invalid_detail"
            continue
        has_result = any(detail.get(key) not in (None, "") for key in (
            "result", "status", "judgment", "verdict",
        ))
        if strict and not has_result:
            return [], "missing_result"
        row = dict(detail)
        row["result"] = _detail_result(detail)
        explicit_parent = (
            detail.get("parent_question_no")
            or detail.get("question_no")
            or detail.get("question_number")
        )
        # The live archive path historically tolerated an omitted number by
        # using the row position.  A repair job must never do that: the row
        # position is not evidence of the printed question number.
        if strict and explicit_parent in (None, ""):
            return [], "missing_question_number"
        parent = str(explicit_parent if explicit_parent not in (None, "") else index + 1).strip()
        if not parent:
            if strict:
                return [], "missing_question_number"
            continue
        row.setdefault("question_no", parent)
        row.setdefault("parent_question_no", detail.get("parent_question_no") or parent)
        normalized.append(row)

    if strict and not normalized:
        return [], "missing_details"

    groups: OrderedDict[str, list[dict]] = OrderedDict()
    for row in normalized:
        parent = str(row.get("parent_question_no") or row.get("question_no") or "").strip()
        if strict and row["result"] == "void":
            # A fully voided question has no analytics projection.  If another
            # subquestion remains active, retain that active part below.
            continue
        groups.setdefault(parent, []).append(row)
    if strict and normalized and not groups:
        return [], "no_active_questions"
    return list(groups.items()), None


def active_observation_period(
    db: Session, student_id: int, at: datetime | None = None,
) -> StudentObservationPeriod | None:
    at = at or datetime.now()
    return (
        db.query(StudentObservationPeriod)
        .filter(
            StudentObservationPeriod.student_id == student_id,
            StudentObservationPeriod.started_at <= at,
            (StudentObservationPeriod.ended_at.is_(None) | (StudentObservationPeriod.ended_at > at)),
        )
        .order_by(StudentObservationPeriod.started_at.desc(), StudentObservationPeriod.id.desc())
        .first()
    )


def _latest_cycle(db: Session, student_id: int, identity: str) -> WrongQuestionCycle | None:
    return (
        db.query(WrongQuestionCycle)
        .filter(
            WrongQuestionCycle.student_id == student_id,
            WrongQuestionCycle.question_identity == identity,
        )
        .order_by(WrongQuestionCycle.cycle_no.desc(), WrongQuestionCycle.id.desc())
        .first()
    )


def _metadata_by_parent(
    details: list[dict],
    knowledge_items: list[dict] | None,
) -> dict[str, dict]:
    """Merge per-row knowledge metadata without changing the source details."""
    metadata_by_parent: dict[str, dict] = {}
    for index, detail in enumerate(details or []):
        if not isinstance(detail, dict):
            continue
        parent = str(
            detail.get("parent_question_no")
            or detail.get("question_no")
            or detail.get("question_number")
            or index + 1
        ).strip()
        metadata = metadata_by_parent.setdefault(parent, {})
        if knowledge_items and index < len(knowledge_items) and isinstance(knowledge_items[index], dict):
            source = knowledge_items[index]
        else:
            source = detail
        for key in ("question_bank_id", "template_question_id", "chapter", "topic", "knowledge_point", "difficulty", "error_type"):
            if metadata.get(key) in (None, "") and source.get(key) not in (None, ""):
                metadata[key] = (
                    sanitize_error_type(source.get(key))
                    if key == "error_type" else source.get(key)
                )
    return metadata_by_parent


def _build_attempt_event(
    *,
    student: Student,
    teacher_id: int,
    grading_result: GradingResult,
    parent_no: str,
    rows: list[dict],
    metadata: dict,
    occurred_at: datetime,
    analytics_included: bool,
    inclusion_reason: str,
    observation_period_id: int | None,
    assessment_purpose: str,
) -> QuestionAttemptEvent:
    """Build one stable top-level attempt from canonical detail rows."""
    snapshot, answer_snapshot = _combined_snapshot(parent_no, rows)
    merged = {**rows[0], **snapshot, **metadata}
    topic = str(
        metadata.get("topic")
        or metadata.get("knowledge_point")
        or snapshot.get("topic")
        or snapshot.get("knowledge_point")
        or ""
    ).strip()
    snapshot.update({
        "chapter": str(metadata.get("chapter") or snapshot.get("chapter") or "").strip(),
        "topic": topic,
        "knowledge_point": topic,
        "difficulty": metadata.get("difficulty") or snapshot.get("difficulty") or 3,
        "error_type": sanitize_error_type(
            metadata.get("error_type") or snapshot.get("error_type")
        ),
    })
    context = grading_result_teaching_context(grading_result)
    stamp_teaching_snapshot(snapshot, context)
    merged.update(snapshot)
    if not context:
        merged.pop("teaching_context", None)
    identity, method, fingerprint = question_identity(merged, metadata)
    return QuestionAttemptEvent(
        student_id=student.id,
        class_id=student.class_id,
        teacher_id=teacher_id,
        grading_result_id=grading_result.id,
        question_bank_id=metadata.get("question_bank_id"),
        question_no=parent_no,
        question_identity=identity,
        identity_method=method,
        question_fingerprint=fingerprint,
        question_type=snapshot["question_type"],
        question_snapshot_json=json.dumps(snapshot, ensure_ascii=False),
        student_answer_snapshot_json=json.dumps(answer_snapshot, ensure_ascii=False),
        final_result=_aggregate_result(rows),
        analytics_included=analytics_included,
        inclusion_reason=inclusion_reason,
        observation_period_id=observation_period_id,
        source_category=grading_result.category or "daily",
        assessment_purpose=assessment_purpose,
        occurred_at=occurred_at,
    )


def _attempt_projection_applied(db: Session, attempt: QuestionAttemptEvent) -> bool:
    """Best-effort idempotency check for a pre-existing event.

    A wrong event may be counted in a cycle without being stored as the cycle's
    opener.  Compare the cycle's recorded count with all wrong attempts in its
    time window so repairing a partial legacy projection does not increment it
    twice.
    """
    result = normalize_attempt_result(attempt.final_result)
    if result not in _WRONG_RESULTS and result != "correct":
        return True
    if result == "correct":
        return bool(db.query(WrongQuestionCycle.id).filter(
            WrongQuestionCycle.student_id == attempt.student_id,
            WrongQuestionCycle.closed_attempt_id == attempt.id,
        ).first())

    all_cycles = db.query(WrongQuestionCycle).filter(
        WrongQuestionCycle.student_id == attempt.student_id,
        WrongQuestionCycle.question_identity == attempt.question_identity,
    ).order_by(WrongQuestionCycle.cycle_no.asc(), WrongQuestionCycle.id.asc()).all()
    # A teacher may have deliberately voided the cycle opened by this exact
    # event.  Do not resurrect that decision during a later repair run.
    if any(cycle.opened_attempt_id == attempt.id for cycle in all_cycles):
        return True
    cycles = [cycle for cycle in all_cycles if cycle.status != "voided"]
    for cycle in cycles:
        if cycle.opened_attempt_id == attempt.id:
            return True
        if cycle.opened_at and attempt.occurred_at and attempt.occurred_at < cycle.opened_at:
            continue
        if cycle.closed_at and attempt.occurred_at and attempt.occurred_at > cycle.closed_at:
            continue
        wrong_events = db.query(QuestionAttemptEvent).filter(
            QuestionAttemptEvent.student_id == attempt.student_id,
            QuestionAttemptEvent.question_identity == attempt.question_identity,
            QuestionAttemptEvent.analytics_included.is_(True),
            QuestionAttemptEvent.final_result.in_(tuple(_WRONG_RESULTS)),
            QuestionAttemptEvent.occurred_at >= cycle.opened_at,
        ).order_by(QuestionAttemptEvent.occurred_at.asc(), QuestionAttemptEvent.id.asc()).all()
        if cycle.closed_at:
            wrong_events = [row for row in wrong_events if row.occurred_at <= cycle.closed_at]
        if any(row.id == attempt.id for row in wrong_events) and cycle.wrong_count >= len(wrong_events):
            return True
    return False


def audit_grading_result_projection(
    db: Session,
    *,
    grading_result: GradingResult,
) -> dict:
    """Return a metadata-only projection audit for one grading result.

    The return value intentionally contains counts and state names only; no
    question text, image path, student name, or answer is exposed.
    """
    details = grading_result.get_details()
    groups, detail_error = _projection_groups(details, strict=True)
    declared_total = int(getattr(grading_result, "total_questions", 0) or 0)
    if detail_error is None and declared_total and len(details) < declared_total:
        detail_error = "incomplete_details"
    attempts = db.query(QuestionAttemptEvent).filter(
        QuestionAttemptEvent.grading_result_id == grading_result.id,
    ).order_by(QuestionAttemptEvent.id.asc()).all()
    attempt_ids = [row.id for row in attempts]
    cycles = (
        db.query(WrongQuestionCycle).filter(
            WrongQuestionCycle.opened_attempt_id.in_(attempt_ids),
        ).all()
        if attempt_ids else []
    )
    expected = len(groups)
    existing_numbers = {str(row.question_no or "").strip() for row in attempts}
    expected_numbers = {parent for parent, _rows in groups}
    expected_results = {parent: _aggregate_result(rows) for parent, rows in groups}
    cycle_statuses = sorted({str(row.status or "").strip() or "unknown" for row in cycles})
    inconsistent_attempts = [
        row for row in attempts
        if str(row.question_no or "").strip() in expected_numbers
        and not row.analytics_included
        and row.inclusion_reason != "teacher_void"
    ]
    invalid_attempts = [
        row for row in attempts
        if str(row.final_result or "").strip().lower() not in _VALID_RESULTS
        and normalize_attempt_result(row.final_result) != "void"
    ]
    result_mismatches = [
        row for row in attempts
        if str(row.question_no or "").strip() in expected_results
        and row.inclusion_reason != "teacher_void"
        and normalize_attempt_result(row.final_result)
        != expected_results[str(row.question_no or "").strip()]
    ]
    missing_cycle_attempts = [
        row for row in attempts
        if row.analytics_included
        and normalize_attempt_result(row.final_result) in _WRONG_RESULTS
        and not _attempt_projection_applied(db, row)
    ]
    complete = (
        detail_error is None
        and expected_numbers.issubset(existing_numbers)
        and not missing_cycle_attempts
        and not inconsistent_attempts
        and not invalid_attempts
        and not result_mismatches
    )
    statuses = sorted({normalize_attempt_result(row.final_result) for row in attempts})
    return {
        "grading_result_id": grading_result.id,
        "student_id": grading_result.student_id,
        "analytics_included": bool(grading_result.analytics_included),
        "inclusion_reason": grading_result.inclusion_reason or "included",
        "declared_total_questions": declared_total,
        "declared_wrong_count": int(getattr(grading_result, "wrong_count", 0) or 0),
        "detail_count": len(details) if isinstance(details, list) else 0,
        "expected_question_count": expected,
        "attempt_count": len(attempts),
        "cycle_count": len(cycles),
        "cycle_statuses": cycle_statuses,
        "wrong_attempts_missing_cycle": len(missing_cycle_attempts),
        "inconsistent_attempts": len(inconsistent_attempts),
        "invalid_attempts": len(invalid_attempts),
        "result_mismatches": len(result_mismatches),
        "attempt_statuses": statuses,
        "projection_complete": complete,
        "repairable": bool(grading_result.analytics_included) and detail_error is None and bool(groups),
        "skip_reason": detail_error,
    }


def _legacy_knowledge_items(db: Session, grading_result: GradingResult, details: list[dict]) -> list[dict] | None:
    """Recover safe topic metadata from the old KnowledgeEvent projection."""
    rows = db.query(KnowledgeEvent).filter(
        KnowledgeEvent.grading_result_id == grading_result.id,
    ).order_by(KnowledgeEvent.id.asc()).all()
    if not rows or len(rows) != len(details):
        return None
    return [
        {
            "question_bank_id": row.question_bank_id,
            "chapter": row.chapter,
            "topic": row.topic,
            "difficulty": row.difficulty,
            "error_type": sanitize_error_type(row.error_type),
        }
        for row in rows
    ]


def _normalize_legacy_knowledge_events(
    db: Session,
    grading_result: GradingResult,
) -> None:
    """Bring the older analytics projection onto the same result vocabulary."""
    rows = db.query(KnowledgeEvent).filter(
        KnowledgeEvent.grading_result_id == grading_result.id,
    ).all()
    for row in rows:
        context = grading_result_teaching_context(grading_result)
        row.education_stage = context.get("education_stage", "")
        row.catalog_version = context.get("curriculum_version", "")
        if context:
            point = get_knowledge_point(context["education_stage"], row.chapter, row.topic)
            row.knowledge_point_id = point["id"] if point else ""
        row.result = normalize_attempt_result(row.result)
        # Do not repopulate the retired semantic taxonomy while repairing
        # legacy projections. Preserve only technical review diagnostics.
        row.error_type = sanitize_error_type(row.error_type)


def repair_grading_result_projection(
    db: Session,
    *,
    grading_result: GradingResult,
    student: Student | None = None,
    teacher_id: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Idempotently repair missing question events/cycles for one old result.

    This function never calls a model and never fabricates rows from summary
    counters.  The caller owns the transaction; ``dry_run`` performs no writes.
    """
    audit = audit_grading_result_projection(db, grading_result=grading_result)
    if getattr(grading_result, "deleted_at", None) is not None:
        audit.update({"status": "skipped", "skip_reason": "deleted"})
        return audit
    if not audit["analytics_included"]:
        audit.update({"status": "skipped", "skip_reason": "analytics_excluded"})
        return audit
    if not audit["repairable"]:
        audit.update({"status": "skipped", "skip_reason": audit.get("skip_reason") or "missing_details"})
        return audit
    if audit["projection_complete"]:
        audit.update({"status": "already_complete", "created_attempts": 0})
        return audit
    student = student or db.get(Student, grading_result.student_id)
    if student is None:
        audit.update({"status": "skipped", "skip_reason": "student_missing"})
        return audit
    teacher = int(teacher_id or getattr(student, "teacher_id", 0) or 0)
    if teacher <= 0:
        audit.update({"status": "skipped", "skip_reason": "teacher_missing"})
        return audit
    details = grading_result.get_details()
    if dry_run:
        expected_numbers = {parent for parent, _rows in _projection_groups(details, strict=True)[0]}
        existing_numbers = {
            str(row.question_no or "").strip()
            for row in db.query(QuestionAttemptEvent).filter(
                QuestionAttemptEvent.grading_result_id == grading_result.id,
            ).all()
        }
        audit.update({
            "status": "would_repair",
            "would_create_attempts": len(expected_numbers - existing_numbers),
            "would_repair_cycles": int(audit.get("wrong_attempts_missing_cycle") or 0),
        })
        return audit

    before_attempt_ids = {
        row.id for row in db.query(QuestionAttemptEvent.id).filter(
            QuestionAttemptEvent.grading_result_id == grading_result.id,
        ).all()
    }
    _normalize_legacy_knowledge_events(db, grading_result)
    legacy_items = _legacy_knowledge_items(db, grading_result, details)
    project_question_attempts(
        db,
        student=student,
        teacher_id=teacher,
        grading_result=grading_result,
        details=details,
        knowledge_items=legacy_items,
        analytics_included=True,
        inclusion_reason=grading_result.inclusion_reason or "included",
        observation_period_id=grading_result.observation_period_id,
        assessment_purpose=grading_result.assessment_purpose or "standard",
        apply_correction=True,
        repair_existing=True,
    )
    groups, _detail_error = _projection_groups(details, strict=True)
    expected_results = {parent: _aggregate_result(rows) for parent, rows in groups}
    first_index_by_parent: dict[str, int] = {}
    for index, detail in enumerate(details):
        if not isinstance(detail, dict):
            continue
        parent = str(
            detail.get("parent_question_no")
            or detail.get("question_no")
            or detail.get("question_number")
            or ""
        ).strip()
        if parent:
            first_index_by_parent.setdefault(parent, index)
    existing_attempts = db.query(QuestionAttemptEvent).filter(
        QuestionAttemptEvent.grading_result_id == grading_result.id,
    ).all()
    for attempt in existing_attempts:
        parent = str(attempt.question_no or "").strip()
        expected_result = expected_results.get(parent)
        if (
            expected_result is None
            or attempt.inclusion_reason == "teacher_void"
            or normalize_attempt_result(attempt.final_result) == expected_result
        ):
            continue
        detail_index = first_index_by_parent.get(parent)
        if detail_index is None:
            continue
        revise_question_attempt_projection(
            db,
            grading_result=grading_result,
            previous_details=details,
            details=details,
            detail_index=detail_index,
            mark_teacher_regraded=False,
        )
    db.flush()
    after = audit_grading_result_projection(db, grading_result=grading_result)
    after["created_attempts"] = len({
        row.id for row in db.query(QuestionAttemptEvent.id).filter(
            QuestionAttemptEvent.grading_result_id == grading_result.id,
        ).all()
    } - before_attempt_ids)
    if after.get("projection_complete"):
        # A complete result returned above before any write.  Reaching this
        # branch means the initial audit found a real inconsistency that this
        # call repaired, even when the only write was status normalization.
        after["status"] = "repaired"
    else:
        after["status"] = "incomplete"
        after["skip_reason"] = after.get("skip_reason") or "projection_incomplete"
    return after


def repair_missing_question_projections(
    db: Session,
    *,
    student_id: int | None = None,
    grading_result_id: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Repair eligible historical results in id order and return safe counts."""
    query = db.query(GradingResult).order_by(GradingResult.id.asc())
    if grading_result_id is not None:
        query = query.filter(GradingResult.id == int(grading_result_id))
    if student_id is not None:
        query = query.filter(GradingResult.student_id == int(student_id))
    reports = [repair_grading_result_projection(db, grading_result=row, dry_run=dry_run) for row in query.all()]
    return {
        "dry_run": bool(dry_run),
        "scanned": len(reports),
        "repaired": sum(report.get("status") == "repaired" for report in reports),
        "would_repair": sum(report.get("status") == "would_repair" for report in reports),
        "skipped": sum(report.get("status") == "skipped" for report in reports),
        "already_complete": sum(report.get("status") == "already_complete" for report in reports),
        "created_attempts": sum(int(report.get("created_attempts") or 0) for report in reports),
        "reports": reports,
    }


# Plural alias used by maintenance callers and runbooks.
audit_question_projections = audit_grading_result_projection


def _close_cycle(
    db: Session,
    cycle: WrongQuestionCycle,
    attempt: QuestionAttemptEvent,
    *,
    method: str,
    relation: QuestionCorrectionRelation | None = None,
) -> bool:
    if cycle.status != "pending":
        return False
    cycle.status = "corrected"
    cycle.closed_attempt_id = attempt.id
    cycle.close_method = method
    cycle.relation_id = relation.id if relation else None
    cycle.closed_at = attempt.occurred_at
    db.add(CorrectionEvidence(
        student_id=cycle.student_id,
        cycle_id=cycle.id,
        wrong_attempt_id=cycle.opened_attempt_id,
        correct_attempt_id=attempt.id,
        relation_id=relation.id if relation else None,
        match_method=method,
        match_confidence=1.0 if method in {"question_bank_id", "template_question_id", "fingerprint"} else relation.similarity_score,
        confirmed_by=relation.authorized_by if relation else None,
    ))
    return True


# A different question on the same knowledge point does not prove that one
# specific old mistake is corrected. Links of this type stay for audit but no
# longer close records.
_NON_CLOSING_RELATIONS = ("teacher_authorized_knowledge",)


def _apply_attempt(
    db: Session,
    attempt: QuestionAttemptEvent,
    adoption: "_AdoptionCandidates | None" = None,
) -> None:
    result = normalize_attempt_result(attempt.final_result)
    if not attempt.analytics_included or result in {"needs_review", "void"}:
        return
    # Keep the derived state machine independent of legacy labels on manually
    # inserted events.
    attempt.final_result = result
    latest = _latest_cycle(db, attempt.student_id, attempt.question_identity)
    if latest is None and result == "correct":
        latest = _adopted_original_cycle(db, attempt, adoption)
    if result in _WRONG_RESULTS:
        if latest is not None and latest.status == "pending":
            latest.wrong_count += 1
            latest.updated_at = attempt.occurred_at
            return
        next_cycle = (latest.cycle_no + 1) if latest else 1
        db.add(WrongQuestionCycle(
            student_id=attempt.student_id,
            class_id=attempt.class_id,
            teacher_id=attempt.teacher_id,
            question_identity=attempt.question_identity,
            question_fingerprint=attempt.question_fingerprint,
            cycle_no=next_cycle,
            status="pending",
            opened_attempt_id=attempt.id,
            wrong_count=1,
            opened_at=attempt.occurred_at,
        ))
        if adoption is not None:
            adoption.note_opened(attempt)
        return
    if result != "correct":
        return
    if latest is not None and latest.status == "pending":
        _close_cycle(db, latest, attempt, method=attempt.identity_method)
    # A link names one student's record; read only this student's links.
    links = (
        db.query(QuestionCorrectionRelation, WrongQuestionCycle)
        .join(WrongQuestionCycle, WrongQuestionCycle.id == QuestionCorrectionRelation.target_cycle_id)
        .filter(
            QuestionCorrectionRelation.teacher_id == attempt.teacher_id,
            QuestionCorrectionRelation.source_question_fingerprint == attempt.question_fingerprint,
            QuestionCorrectionRelation.revoked_at.is_(None),
            QuestionCorrectionRelation.relation_type.notin_(_NON_CLOSING_RELATIONS),
            WrongQuestionCycle.student_id == attempt.student_id,
        )
        .order_by(QuestionCorrectionRelation.id.asc())
        .all()
    )
    if not links:
        return
    context = attempt_teaching_context(db, attempt)
    for relation, target in links:
        opened = db.get(QuestionAttemptEvent, target.opened_attempt_id)
        if opened is None or attempt_teaching_context(db, opened) != context:
            continue
        _close_cycle(
            db,
            target,
            attempt,
            method=relation.relation_type or "teacher_authorized_similar",
            relation=relation,
        )


def _effective_attempt_fingerprint(db: Session, attempt: QuestionAttemptEvent) -> str:
    """Compare an adopted old original in memory; never replace its audit hash."""
    snapshot = attempt.question_snapshot()
    context = attempt_teaching_context(db, attempt)
    if snapshot.get("teaching_context"):
        return attempt.question_fingerprint
    if not context:
        return attempt.question_fingerprint
    return question_fingerprint({**snapshot, "teaching_context": context})


def _unstamped_open_cycles(db: Session, student_id: int, teacher_id: int) -> list[tuple]:
    """A student's open cycles whose opening snapshot has no frozen context.

    Only archives adopted from before stage stamping (or graded without a
    stage) lack one, so this reads a handful of rows instead of every open
    record. The SQL test admits every row that Python reads as
    ``not snapshot.get("teaching_context")``: a stamped context always has a
    stage, and unreadable JSON loads as ``{}``.
    """
    snapshot = QuestionAttemptEvent.question_snapshot_json
    frozen_stage = case(
        (func.json_valid(snapshot) == 1, func.json_extract(snapshot, "$.teaching_context.education_stage")),
        else_=None,
    )
    return db.query(WrongQuestionCycle, QuestionAttemptEvent).join(
        QuestionAttemptEvent, QuestionAttemptEvent.id == WrongQuestionCycle.opened_attempt_id,
    ).filter(
        WrongQuestionCycle.student_id == student_id,
        WrongQuestionCycle.teacher_id == teacher_id,
        WrongQuestionCycle.status == "pending",
        frozen_stage.is_(None),
    ).all()


class _AdoptionCandidates:
    """Unstamped open cycles per student, read once for a whole archived paper."""

    def __init__(self, db: Session):
        self._db = db
        self._rows: dict[tuple[int, int], list[tuple]] = {}

    def rows(self, attempt: QuestionAttemptEvent) -> list[tuple]:
        key = (attempt.student_id, attempt.teacher_id)
        if key not in self._rows:
            self._rows[key] = _unstamped_open_cycles(self._db, *key)
        return self._rows[key]

    def note_opened(self, attempt: QuestionAttemptEvent) -> None:
        """A cycle opened by an unstamped attempt is itself a later candidate."""
        if not attempt.question_snapshot().get("teaching_context"):
            self._rows.pop((attempt.student_id, attempt.teacher_id), None)


def _adopted_original_cycle(
    db: Session,
    attempt: QuestionAttemptEvent,
    adoption: _AdoptionCandidates | None = None,
) -> WrongQuestionCycle | None:
    context = attempt_teaching_context(db, attempt)
    if not context:
        return None
    candidates = (adoption or _AdoptionCandidates(db)).rows(attempt)
    if not candidates:
        return None
    fingerprint = _effective_attempt_fingerprint(db, attempt)
    matches = []
    for cycle, opened in candidates:
        # Closed by an earlier answer on the same paper.
        if cycle.status != "pending":
            continue
        if opened.question_snapshot().get("teaching_context"):
            continue
        if attempt_teaching_context(db, opened) != context:
            continue
        if _effective_attempt_fingerprint(db, opened) == fingerprint:
            matches.append(cycle)
    # Ambiguous historical cycles require teacher authorization, not merging.
    return matches[0] if len(matches) == 1 else None


def revise_question_attempt_projection(
    db: Session,
    *,
    grading_result: GradingResult,
    previous_details: list[dict],
    details: list[dict],
    detail_index: int,
    mark_teacher_regraded: bool = True,
) -> QuestionAttemptEvent | None:
    """Synchronize one archived attempt after an explicit teacher judgment.

    Pending-review events have not affected correction state yet, so the normal
    state machine can be applied once the whole top-level question is resolved.
    For a correction of an earlier verdict, derived cycle/evidence state is
    reversed first while the separate GradingCorrection audit remains intact.
    """
    if detail_index < 0 or detail_index >= len(details):
        return None
    changed = details[detail_index]
    parent_no = str(
        changed.get("parent_question_no")
        or changed.get("question_no")
        or changed.get("question_number")
        or detail_index + 1
    ).strip()
    attempt = db.query(QuestionAttemptEvent).filter(
        QuestionAttemptEvent.grading_result_id == grading_result.id,
        QuestionAttemptEvent.question_no == parent_no,
    ).first()
    if attempt is None:
        return None

    current_rows = []
    for row in details:
        if not isinstance(row, dict):
            continue
        row_parent = str(
            row.get("parent_question_no")
            or row.get("question_no")
            or row.get("question_number")
            or ""
        ).strip()
        if row_parent != parent_no:
            continue
        canonical = dict(row)
        canonical["result"] = _detail_result(row)
        if canonical["result"] != "void":
            current_rows.append(canonical)
    old_result = normalize_attempt_result(attempt.final_result)
    new_result = _aggregate_result(current_rows) if current_rows else "needs_review"

    if old_result != new_result and attempt.analytics_included:
        if old_result == "correct":
            closed = db.query(WrongQuestionCycle).filter(
                WrongQuestionCycle.student_id == attempt.student_id,
                WrongQuestionCycle.closed_attempt_id == attempt.id,
            ).all()
            for cycle in closed:
                db.query(CorrectionEvidence).filter(
                    CorrectionEvidence.cycle_id == cycle.id,
                    CorrectionEvidence.correct_attempt_id == attempt.id,
                ).delete(synchronize_session=False)
                cycle.status = "pending"
                cycle.closed_attempt_id = None
                cycle.close_method = ""
                cycle.relation_id = None
                cycle.closed_at = None
        elif old_result in _WRONG_RESULTS:
            cycles = db.query(WrongQuestionCycle).filter(
                WrongQuestionCycle.student_id == attempt.student_id,
                WrongQuestionCycle.question_identity == attempt.question_identity,
            ).order_by(WrongQuestionCycle.cycle_no.asc()).all()
            owning_cycle = next((
                cycle for cycle in cycles
                if cycle.status != "voided"
                if cycle.opened_at <= attempt.occurred_at
                and (cycle.closed_at is None or attempt.occurred_at <= cycle.closed_at)
            ), None)
            if owning_cycle is not None:
                other_wrong = db.query(QuestionAttemptEvent).filter(
                    QuestionAttemptEvent.student_id == attempt.student_id,
                    QuestionAttemptEvent.question_identity == attempt.question_identity,
                    QuestionAttemptEvent.analytics_included.is_(True),
                    QuestionAttemptEvent.final_result.in_(tuple(_WRONG_RESULTS)),
                    QuestionAttemptEvent.id != attempt.id,
                    QuestionAttemptEvent.occurred_at >= owning_cycle.opened_at,
                ).order_by(
                    QuestionAttemptEvent.occurred_at.asc(),
                    QuestionAttemptEvent.id.asc(),
                ).all()
                if owning_cycle.closed_at is not None:
                    other_wrong = [
                        row for row in other_wrong
                        if row.occurred_at <= owning_cycle.closed_at
                    ]
                if other_wrong:
                    owning_cycle.wrong_count = len(other_wrong)
                    if owning_cycle.opened_attempt_id == attempt.id:
                        owning_cycle.opened_attempt_id = other_wrong[0].id
                        owning_cycle.opened_at = other_wrong[0].occurred_at
                else:
                    db.query(CorrectionEvidence).filter(
                        CorrectionEvidence.cycle_id == owning_cycle.id,
                    ).delete(synchronize_session=False)
                    owning_cycle.status = "voided"
                    owning_cycle.closed_attempt_id = None
                    owning_cycle.close_method = ""
                    owning_cycle.relation_id = None
                    owning_cycle.closed_at = None
                    owning_cycle.wrong_count = 0

    preserved = attempt.question_snapshot()
    if current_rows:
        snapshot, answer_snapshot = _combined_snapshot(parent_no, current_rows)
        for key in ("chapter", "topic", "knowledge_point", "difficulty", "error_type"):
            if preserved.get(key) not in (None, ""):
                snapshot[key] = (
                    sanitize_error_type(preserved[key])
                    if key == "error_type" else preserved[key]
                )
        # Adoption labels the archived row, not the historical identity input.
        # Rejudging must not silently inject a new context into a legacy hash.
        if preserved.get("teaching_context"):
            stamp_teaching_snapshot(snapshot, validate_teaching_context(preserved["teaching_context"]))
        else:
            for key in ("teaching_context", "education_stage", "catalog_version", "knowledge_point_id"):
                if key in preserved:
                    snapshot[key] = preserved[key]
        if preserved.get("teacher_regraded") or mark_teacher_regraded:
            snapshot["teacher_regraded"] = True
        attempt.question_snapshot_json = json.dumps(snapshot, ensure_ascii=False)
        attempt.student_answer_snapshot_json = json.dumps(answer_snapshot, ensure_ascii=False)
    else:
        if mark_teacher_regraded:
            preserved["teacher_regraded"] = True
        attempt.question_snapshot_json = json.dumps(preserved, ensure_ascii=False)
    attempt.final_result = new_result
    if not current_rows:
        attempt.analytics_included = False
        attempt.inclusion_reason = "teacher_void"
    elif old_result != new_result and new_result != "needs_review":
        _apply_attempt(db, attempt)
    return attempt


def project_question_attempts(
    db: Session,
    *,
    student: Student,
    teacher_id: int,
    grading_result: GradingResult,
    details: list[dict],
    knowledge_items: list[dict] | None = None,
    analytics_included: bool,
    inclusion_reason: str,
    observation_period_id: int | None,
    assessment_purpose: str,
    apply_correction: bool = True,
    repair_existing: bool = False,
) -> list[QuestionAttemptEvent]:
    groups, _detail_error = _projection_groups(details, strict=repair_existing)
    if not groups:
        return db.query(QuestionAttemptEvent).filter(
            QuestionAttemptEvent.grading_result_id == grading_result.id,
        ).order_by(QuestionAttemptEvent.id).all()

    existing_rows = db.query(QuestionAttemptEvent).filter(
        QuestionAttemptEvent.grading_result_id == grading_result.id,
    ).order_by(QuestionAttemptEvent.id).all()
    existing_by_number = {
        str(row.question_no or "").strip(): row
        for row in existing_rows
        if str(row.question_no or "").strip()
    }
    created: list[QuestionAttemptEvent] = []
    occurred_at = _stable_question_time(grading_result)
    metadata_by_parent = _metadata_by_parent(details, knowledge_items)
    adoption = _AdoptionCandidates(db)

    for parent_no, rows in groups:
        existing = existing_by_number.get(parent_no)
        if existing is not None:
            if repair_existing and apply_correction:
                if (
                    existing.inclusion_reason == "teacher_void"
                    or normalize_attempt_result(existing.final_result) == "void"
                ):
                    # Preserve an explicit teacher exclusion.
                    continue
                existing.analytics_included = analytics_included
                existing.inclusion_reason = inclusion_reason
                existing.observation_period_id = observation_period_id
                canonical_result = _aggregate_result(rows)
                # Legacy rows may contain Chinese labels or an invalid value.
                # Normalize those labels, but do not invent a verdict for an
                # unknown row (it remains needs_review).
                stored_result = normalize_attempt_result(existing.final_result)
                if str(existing.final_result or "").strip().lower() not in _VALID_RESULTS:
                    existing.final_result = stored_result
                # A teacher may be changing this event right now.  Leave a
                # genuine old/new mismatch to ``revise_question_attempt_projection``;
                # applying the new state here would hide the old state needed to
                # reverse its cycle safely.
                if stored_result == canonical_result and (
                    canonical_result in _WRONG_RESULTS or canonical_result == "correct"
                ):
                    if not _attempt_projection_applied(db, existing):
                        _apply_attempt(db, existing, adoption)
                        # A legacy result can contain repeated question
                        # fingerprints.  Flush the derived cycle before the
                        # next existing event is examined, otherwise two
                        # pending ``cycle_no=1`` rows can be staged together
                        # and violate the unique cycle constraint.
                        db.flush()
            continue

        metadata = metadata_by_parent.get(parent_no) or {}
        event = _build_attempt_event(
            student=student,
            teacher_id=teacher_id,
            grading_result=grading_result,
            parent_no=parent_no,
            rows=rows,
            metadata=metadata,
            occurred_at=occurred_at,
            analytics_included=analytics_included,
            inclusion_reason=inclusion_reason,
            observation_period_id=observation_period_id,
            assessment_purpose=assessment_purpose,
        )
        if any(bool(row.get("teacher_regraded")) for row in rows):
            snapshot = event.question_snapshot()
            snapshot["teacher_regraded"] = True
            event.question_snapshot_json = json.dumps(snapshot, ensure_ascii=False)
        db.add(event)
        db.flush()
        if apply_correction:
            _apply_attempt(db, event, adoption)
        created.append(event)
    return db.query(QuestionAttemptEvent).filter(
        QuestionAttemptEvent.grading_result_id == grading_result.id,
    ).order_by(QuestionAttemptEvent.id).all()


def pending_cycles(db: Session, student_id: int) -> list[WrongQuestionCycle]:
    return (
        db.query(WrongQuestionCycle)
        .filter(WrongQuestionCycle.student_id == student_id, WrongQuestionCycle.status == "pending")
        .order_by(WrongQuestionCycle.opened_at.asc(), WrongQuestionCycle.id.asc())
        .all()
    )


def question_knowledge(db: Session, attempt: QuestionAttemptEvent | None) -> dict:
    """Return trustworthy chapter/topic metadata for an archived attempt."""
    if attempt is None:
        return {"chapter": "", "topic": "", "knowledge_point": "", "difficulty": 3}
    snapshot = attempt.question_snapshot()
    chapter = str(snapshot.get("chapter") or "").strip()
    topic = str(snapshot.get("topic") or snapshot.get("knowledge_point") or "").strip()
    difficulty = snapshot.get("difficulty") or 3
    if topic not in _UNKNOWN_KNOWLEDGE:
        return {
            "chapter": chapter,
            "topic": topic,
            "knowledge_point": topic,
            "difficulty": difficulty,
        }

    query = db.query(KnowledgeEvent).filter(
        KnowledgeEvent.grading_result_id == attempt.grading_result_id,
        KnowledgeEvent.analytics_included.is_(True),
    )
    if attempt.question_bank_id:
        query = query.filter(KnowledgeEvent.question_bank_id == attempt.question_bank_id)
    rows = query.order_by(KnowledgeEvent.id.asc()).all()
    if not attempt.question_bank_id:
        attempts = db.query(QuestionAttemptEvent).filter(
            QuestionAttemptEvent.grading_result_id == attempt.grading_result_id,
        ).order_by(QuestionAttemptEvent.id.asc()).all()
        if len(rows) == len(attempts) and len(rows) > 1:
            attempt_index = next(
                (index for index, row in enumerate(attempts) if row.id == attempt.id),
                None,
            )
            if attempt_index is not None:
                rows = [rows[attempt_index]]
    identities = {
        (str(row.chapter or "").strip(), str(row.topic or "").strip(), int(row.difficulty or 3))
        for row in rows
        if str(row.topic or "").strip() not in _UNKNOWN_KNOWLEDGE
    }
    if len(identities) == 1:
        chapter, topic, difficulty = next(iter(identities))
        return {
            "chapter": chapter,
            "topic": topic,
            "knowledge_point": topic,
            "difficulty": difficulty,
        }
    return {"chapter": chapter, "topic": topic, "knowledge_point": topic, "difficulty": difficulty}


def _chunks(values: Iterable, size: int = 500) -> Iterable[list]:
    """Split ids for ``IN`` lists below SQLite's bound-parameter limit."""
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _stage_cycle_query(query, teaching_context=None):
    context = validate_teaching_context(current_teaching_context() if teaching_context is None else teaching_context)
    if context:
        query = query.join(QuestionAttemptEvent, QuestionAttemptEvent.id == WrongQuestionCycle.opened_attempt_id).join(
            GradingResult, GradingResult.id == QuestionAttemptEvent.grading_result_id,
        ).filter(GradingResult.education_stage == context["education_stage"],
                 GradingResult.catalog_version == context["curriculum_version"])
    return query


def wrong_question_summary(db: Session, student_id: int, *, teaching_context=None) -> list[dict]:
    cycles = (
        _stage_cycle_query(db.query(WrongQuestionCycle), teaching_context)
        .filter(
            WrongQuestionCycle.student_id == student_id,
            WrongQuestionCycle.status != "voided",
        )
        .order_by(WrongQuestionCycle.opened_at.desc(), WrongQuestionCycle.id.desc())
        .all()
    )
    # Read the opening attempts and closing results in bulk, not per cycle.
    opened_by_id: dict[int, QuestionAttemptEvent] = {}
    for chunk in _chunks({cycle.opened_attempt_id for cycle in cycles}):
        for attempt in db.query(QuestionAttemptEvent).filter(QuestionAttemptEvent.id.in_(chunk)):
            opened_by_id[attempt.id] = attempt
    closed_result_ids: dict[int, int] = {}
    for chunk in _chunks({cycle.closed_attempt_id for cycle in cycles if cycle.closed_attempt_id}):
        closed_result_ids.update(db.query(
            QuestionAttemptEvent.id, QuestionAttemptEvent.grading_result_id,
        ).filter(QuestionAttemptEvent.id.in_(chunk)).all())
    items: list[dict] = []
    for cycle in cycles:
        opened = opened_by_id.get(cycle.opened_attempt_id)
        snapshot = opened.question_snapshot() if opened else {}
        context = attempt_teaching_context(db, opened)
        knowledge = question_knowledge(db, opened)
        point = get_knowledge_point(context["education_stage"], knowledge["chapter"], knowledge["topic"]) if context else None
        items.append({
            "cycle_id": cycle.id,
            "question_identity": cycle.question_identity,
            "education_stage": context.get("education_stage", ""),
            "catalog_version": context.get("curriculum_version", ""),
            "knowledge_point_id": point["id"] if point else snapshot.get("knowledge_point_id", ""),
            "question_fingerprint": cycle.question_fingerprint,
            "question_no": opened.question_no if opened else "",
            "question": snapshot.get("question_text") or "",
            "question_type": snapshot.get("question_type") or "solve",
            "chapter": knowledge["chapter"],
            "topic": knowledge["topic"],
            "knowledge_point": knowledge["knowledge_point"],
            "grading_result_id": opened.grading_result_id if opened else None,
            "closed_grading_result_id": closed_result_ids.get(cycle.closed_attempt_id),
            "status": cycle.status,
            "cycle_no": cycle.cycle_no,
            "wrong_count": cycle.wrong_count,
            "close_method": cycle.close_method,
            "opened_at": str(cycle.opened_at) if cycle.opened_at else None,
            "closed_at": str(cycle.closed_at) if cycle.closed_at else None,
            "analytics_included": bool(opened.analytics_included) if opened else True,
        })
    return items


def correction_rate(db: Session, student_id: int, *, teaching_context=None) -> float:
    cycles = _stage_cycle_query(db.query(WrongQuestionCycle), teaching_context).filter(
        WrongQuestionCycle.student_id == student_id,
        WrongQuestionCycle.status != "voided",
    ).all()
    identities = {cycle.question_identity for cycle in cycles}
    if not identities:
        return 0.0
    still_open = {cycle.question_identity for cycle in cycles if cycle.status == "pending"}
    return round(len(identities - still_open) / len(identities), 4)


# Minimum normalized question-text similarity for a "similar question" link.
_SIMILARITY_THRESHOLDS = {"choice": 0.94, "fill": 0.92, "solve": 0.88}


@dataclass
class _OpenQuestion:
    """One distinct question among a class's open wrong records."""

    records: list
    question_type: str
    question_text: str
    effective_fingerprint: str
    knowledge: dict
    # seq2 holds this question's normalized text; difflib caches its index.
    matcher: SequenceMatcher


def _class_open_questions(
    db: Session,
    *,
    teacher_id: int,
    class_id: int,
    context: dict,
    cycle_ids: Iterable[int] | None = None,
) -> list[_OpenQuestion]:
    """Group a class's open wrong records by question.

    Every student in a class sits the same papers, so tens of thousands of
    open records come from a few thousand distinct questions. Records are read
    as plain columns and only one snapshot per question is parsed. Records
    with the same stored fingerprint share question content and, after the
    stage filter, stage; classification also labels them per fingerprint.
    """
    query = db.query(
        WrongQuestionCycle.id.label("cycle_id"),
        WrongQuestionCycle.student_id,
        WrongQuestionCycle.class_id,
        QuestionAttemptEvent.id.label("attempt_id"),
        QuestionAttemptEvent.question_no,
        QuestionAttemptEvent.grading_result_id,
        QuestionAttemptEvent.question_fingerprint,
    ).join(
        QuestionAttemptEvent, QuestionAttemptEvent.id == WrongQuestionCycle.opened_attempt_id,
    ).filter(
        WrongQuestionCycle.teacher_id == teacher_id,
        WrongQuestionCycle.class_id == class_id,
        WrongQuestionCycle.status == "pending",
    )
    if context:
        query = query.join(GradingResult, GradingResult.id == QuestionAttemptEvent.grading_result_id).filter(
            GradingResult.education_stage == context["education_stage"],
            GradingResult.catalog_version == context["curriculum_version"],
        )
    if cycle_ids is None:
        rows = query.all()
    else:
        rows = [
            row
            for chunk in _chunks(sorted(set(cycle_ids)))
            for row in query.filter(WrongQuestionCycle.id.in_(chunk)).all()
        ]
    by_fingerprint: dict[str, list] = {}
    for row in sorted(rows, key=lambda item: item.cycle_id):
        by_fingerprint.setdefault(row.question_fingerprint, []).append(row)
    first_attempt_ids = {
        fingerprint: min(row.attempt_id for row in group)
        for fingerprint, group in by_fingerprint.items()
    }
    attempts: dict[int, QuestionAttemptEvent] = {}
    for chunk in _chunks(first_attempt_ids.values()):
        for attempt in db.query(QuestionAttemptEvent).filter(QuestionAttemptEvent.id.in_(chunk)):
            attempts[attempt.id] = attempt

    questions: list[_OpenQuestion] = []
    for fingerprint, group in by_fingerprint.items():
        attempt = attempts[first_attempt_ids[fingerprint]]
        if context and attempt_teaching_context(db, attempt) != context:
            continue
        snapshot = attempt.question_snapshot()
        matcher = SequenceMatcher(None)
        matcher.set_seq2(_normalize_math_text(snapshot.get("question_text")))
        questions.append(_OpenQuestion(
            records=group,
            question_type=str(snapshot.get("question_type") or "solve"),
            question_text=snapshot.get("question_text") or "",
            effective_fingerprint=_effective_attempt_fingerprint(db, attempt),
            knowledge=question_knowledge(db, attempt),
            matcher=matcher,
        ))
    return questions


def _similarity_at_least(matcher: SequenceMatcher, threshold: float) -> float | None:
    """Return ``ratio()`` if it reaches ``threshold``.

    ``real_quick_ratio`` and ``quick_ratio`` are cheap upper bounds of
    ``ratio``, so rejecting on them never drops a pair ``ratio`` would keep.
    """
    if matcher.real_quick_ratio() < threshold or matcher.quick_ratio() < threshold:
        return None
    score = matcher.ratio()
    return score if score >= threshold else None


def _same_knowledge(source_chapter: str, source_topic: str, knowledge: dict) -> bool:
    target_topic = knowledge["topic"]
    target_chapter = knowledge["chapter"]
    return (
        source_topic not in _UNKNOWN_KNOWLEDGE
        and target_topic not in _UNKNOWN_KNOWLEDGE
        and source_topic == target_topic
        and (
            source_chapter in _UNKNOWN_KNOWLEDGE
            or target_chapter in _UNKNOWN_KNOWLEDGE
            or source_chapter == target_chapter
        )
    )


def _authorized_pairs(db: Session, teacher_id: int, fingerprints: Iterable[str]) -> set[tuple[str, int]]:
    """Active (source fingerprint, target cycle) links of either kind."""
    fingerprints = sorted(set(fingerprints))
    pairs: set[tuple[str, int]] = set()
    for model in (QuestionCorrectionRelation, TemplateQuestionAuthorization):
        for chunk in _chunks(fingerprints):
            pairs.update(
                (fingerprint, cycle_id)
                for fingerprint, cycle_id in db.query(
                    model.source_question_fingerprint, model.target_cycle_id,
                ).filter(
                    model.teacher_id == teacher_id,
                    model.source_question_fingerprint.in_(chunk),
                    model.revoked_at.is_(None),
                ).all()
            )
    return pairs


_CANDIDATE_DIFFERENCE = {
    "same_original": "指纹完全一致",
    "similar_candidate": "题干高度相似；请重点核对数字、符号、选项、小问与图形关系",
}


def template_candidates(
    db: Session,
    *,
    teacher_id: int,
    class_id: int | None,
    questions: Iterable[dict],
    limit: int = 1000,
    teaching_context=None,
    cycle_ids: Iterable[int] | None = None,
) -> list[dict]:
    """List the class's open wrong records each template question can retest.

    A candidate is the same question (same fingerprint) or a highly similar
    one. A shared knowledge point is only summarized per question: answering
    a different question on that point does not prove an old mistake fixed.
    ``cycle_ids`` limits matching to records the teacher has already seen.
    """
    context = validate_teaching_context(current_teaching_context() if teaching_context is None else teaching_context)
    open_questions = [] if class_id is None else _class_open_questions(
        db, teacher_id=teacher_id, class_id=class_id, context=context, cycle_ids=cycle_ids,
    )
    student_ids = {record.student_id for item in open_questions for record in item.records}
    students = {
        student.id: student
        for chunk in _chunks(sorted(student_ids))
        for student in db.query(Student).filter(Student.id.in_(chunk)).all()
    }
    prepared = []
    for raw in questions:
        raw = dict(raw)
        stamp_teaching_snapshot(raw, context)
        prepared.append((raw, question_fingerprint(raw)))
    authorized = _authorized_pairs(db, teacher_id, (fingerprint for _raw, fingerprint in prepared))

    output: list[dict] = []
    for raw, fingerprint in prepared:
        qtype = str(raw.get("question_type") or "solve")
        source_text = _normalize_math_text(raw.get("question_text") or raw.get("question"))
        source_chapter = str(raw.get("chapter") or "").strip()
        source_topic = str(raw.get("topic") or raw.get("knowledge_point") or "").strip()
        threshold = _SIMILARITY_THRESHOLDS.get(qtype, 0.92)
        candidates: list[dict] = []
        same_topic_records: list = []
        for item in open_questions:
            if _same_knowledge(source_chapter, source_topic, item.knowledge):
                same_topic_records.extend(item.records)
            if item.question_type != qtype:
                continue
            if item.effective_fingerprint == fingerprint:
                relation, score = "same_original", 1.0
            else:
                item.matcher.set_seq1(source_text)
                score = _similarity_at_least(item.matcher, threshold)
                if score is None:
                    continue
                relation = "similar_candidate"
            for record in item.records:
                student = students.get(record.student_id)
                candidates.append({
                    "cycle_id": record.cycle_id,
                    "student_id": record.student_id,
                    "student_name": student.name if student else "",
                    "student_no": student.student_no if student else "",
                    "class_id": record.class_id,
                    "historical_question_no": record.question_no,
                    "historical_question": item.question_text,
                    "historical_grading_result_id": record.grading_result_id,
                    "chapter": item.knowledge["chapter"],
                    "topic": item.knowledge["topic"],
                    "knowledge_point": item.knowledge["topic"],
                    "relation": relation,
                    "similarity_score": round(score, 4),
                    "difference_summary": _CANDIDATE_DIFFERENCE[relation],
                    "already_authorized": (fingerprint, record.cycle_id) in authorized,
                })
        candidates.sort(key=lambda item: (
            item["relation"] != "same_original", -item["similarity_score"], item["student_id"], item["cycle_id"],
        ))
        output.append({
            "question_no": str(raw.get("question_no") or raw.get("question_number") or ""),
            "question_fingerprint": fingerprint,
            "current_question": str(raw.get("question_text") or raw.get("question") or ""),
            "current_standard_answer": str(
                raw.get("standard_answer") or raw.get("correct_answer") or ""
            ),
            "chapter": source_chapter,
            "topic": source_topic,
            "knowledge_point": source_topic,
            "candidates": candidates[: max(1, min(limit, 5000))],
            "knowledge_summary": {
                "chapter": source_chapter,
                "topic": source_topic,
                "student_count": len({record.student_id for record in same_topic_records}),
                "record_count": len(same_topic_records),
            } if same_topic_records else None,
        })
    return output
