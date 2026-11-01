"""Unified persistence for grading results.

Both detailed grading and quick-grade write through this module so downstream
reports, Hermes tools, and class analytics see the same data shape.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from models import (
    GradingCorrection,
    GradingResult,
    KnowledgeEvent,
    Student,
    StudentProfile,
    Weakness,
    WeaknessOverride,
    sanitize_error_type,
)
from services.correction_tracking import (
    active_observation_period,
    normalize_attempt_result,
    project_question_attempts,
    revise_question_attempt_projection,
    resolve_grading_teaching_context,
    resolve_archived_teaching_context,
    grading_result_teaching_context,
    stamp_teaching_snapshot,
)
from models_correction import QuestionAttemptEvent
from config import ARCHIVE_DIR
from services.grading_contract import VALID_GRADING_CATEGORIES

logger = logging.getLogger(__name__)


def image_content_hash(path: str) -> str:
    """Return sha256 of an image file, or empty string if unavailable."""
    if not path:
        return ""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


_CONTRACT_FIELDS = (
    "parent_question_no", "question_no", "subquestion_no", "question", "question_text",
    "question_type", "standard_answer", "correct_answer", "answer_variants",
    "solution_steps", "grading_method", "max_score", "blank_count",
    "fill_scoring_mode", "blank_scores",
    "answer_kind", "drawing_reference_hash", "drawing_policy",
    "text_grading_context_hash",
)


def build_grading_contract_hash(details: list[dict], category: str, scoring_version: int, *, teaching_context=None) -> str:
    """Fingerprint the frozen rule inputs, excluding model verdicts and OCR output.

    It deliberately includes answers, grading method and point values but not the
    student's answer/result/explanation.  Thus a retry of the same batch is
    idempotent while a teacher's corrected answer key or mark allocation creates
    a distinct immutable result snapshot.
    """
    units = []
    for index, detail in enumerate(details or []):
        if not isinstance(detail, dict):
            units.append({"position": index, "invalid": True})
            continue
        unit = {key: detail.get(key) for key in _CONTRACT_FIELDS if key in detail}
        if "slot_ids" in detail:
            unit["slot_ids"] = detail["slot_ids"]
            unit["answer_slots"] = detail.get("answer_slots") or []
        # New typed contracts bind slot roles and positional answers. Historical
        # result hashes stay byte-identical when this optional field is absent.
        if detail.get("region_kinds"):
            unit["region_kinds"] = detail["region_kinds"]
            unit["answer_slots"] = detail.get("answer_slots") or []
        blank_results = detail.get("blank_results")
        if isinstance(blank_results, list):
            unit["blank_results"] = [
                {
                    "blank_index": item.get("blank_index"),
                    "standard_answer": item.get("standard_answer"),
                }
                for item in blank_results
                if isinstance(item, dict)
            ]
        units.append(unit)
    payload = {
        "category": category or "daily",
        "scoring_version": int(scoring_version or 1),
        "units": units,
    }
    context = resolve_grading_teaching_context(details, teaching_context)
    if context:
        payload["teaching_context"] = context
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def find_existing_grading_result(
    db: Session,
    student_id: int,
    image_hash: str,
    category: str,
    exam_name: str,
    grading_contract_hash: str,
) -> GradingResult | None:
    """Business-level idempotency lookup for repeated submissions."""
    if not image_hash:
        return None
    return (
        db.query(GradingResult)
        .filter(
            GradingResult.student_id == student_id,
            GradingResult.image_content_hash == image_hash,
            GradingResult.category == (category or "daily"),
            GradingResult.exam_name == (exam_name or ""),
            GradingResult.grading_contract_hash == (grading_contract_hash or ""),
            GradingResult.deleted_at.is_(None),
        )
        .order_by(GradingResult.id.asc())
        .first()
    )


def _as_json_text(value) -> str:
    try:
        return json.dumps(value or {}, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


def _safe_path_part(value: str, fallback: str) -> str:
    name = os.path.basename(str(value or "").strip())
    name = re.sub(r"[^\w\u4e00-\u9fff ._-]+", "_", name)
    name = name.strip(" ._")
    return name or fallback


def _meta_for(details: list[dict], knowledge_items: list[dict] | None, idx: int) -> dict:
    meta = {}
    if knowledge_items and idx < len(knowledge_items) and isinstance(knowledge_items[idx], dict):
        meta.update(knowledge_items[idx])
    if idx < len(details) and isinstance(details[idx], dict):
        detail = details[idx]
        for key in ("chapter", "topic", "difficulty", "question_bank_id", "error_type"):
            if key not in meta and detail.get(key) is not None:
                meta[key] = (
                    sanitize_error_type(detail.get(key))
                    if key == "error_type" else detail.get(key)
                )
    if "error_type" in meta:
        meta["error_type"] = sanitize_error_type(meta.get("error_type"))
    return meta


def _event_from_detail(
    student: Student,
    teacher_id: int,
    grading_result_id: int,
    detail: dict,
    meta: dict,
    *, teaching_context=None,
) -> KnowledgeEvent | None:
    result = (detail.get("result") or "").strip()
    if not result:
        return None
    # 错因分类已下线；KnowledgeEvent 只承载知识点和作答结果。
    error_type = ""
    chapter = (meta.get("chapter") or detail.get("chapter") or "未知").strip()
    topic = (meta.get("topic") or detail.get("topic") or "未知").strip()
    try:
        difficulty = int(meta.get("difficulty") or detail.get("difficulty") or 3)
    except (TypeError, ValueError):
        difficulty = 3
    context = resolve_grading_teaching_context([detail], teaching_context)
    snapshot = {"chapter": chapter, "topic": topic}
    stamp_teaching_snapshot(snapshot, context)
    return KnowledgeEvent(
        student_id=student.id,
        class_id=student.class_id,
        teacher_id=teacher_id,
        grading_result_id=grading_result_id,
        question_bank_id=meta.get("question_bank_id") or detail.get("question_bank_id"),
        chapter=chapter,
        topic=topic,
        result=result,
        error_type=error_type,
        difficulty=difficulty,
        education_stage=context.get("education_stage", ""),
        catalog_version=context.get("curriculum_version", ""),
        knowledge_point_id=snapshot.get("knowledge_point_id", ""),
    )


# ⚠️ 兼容双写：KnowledgeEvent 是主数据源，但 student.py/stats.py/exercise.py/
# report.py/exporter.py/paper_generator.py 仍读 weaknesses 表。
# 迁移完全部读方前不可删除此函数。
def _upsert_weaknesses(
    db: Session,
    student_id: int,
    details: Iterable[dict],
    knowledge_items: list[dict] | None,
    *, teaching_context=None,
) -> None:
    details = list(details)
    context = resolve_grading_teaching_context(details, teaching_context)
    stage = context.get("education_stage", "")
    version = context.get("curriculum_version", "")
    for idx, detail in enumerate(details):
        result = (detail.get("result") or "").strip()
        if result not in ("partial", "wrong", "blank"):
            continue
        meta = _meta_for(list(details) if not isinstance(details, list) else details, knowledge_items, idx)
        chapter = (meta.get("chapter") or detail.get("chapter") or "未知").strip()
        topic = (meta.get("topic") or detail.get("topic") or "未知").strip()
        subtopic = topic
        db.query(WeaknessOverride).filter(
            WeaknessOverride.student_id == student_id,
            WeaknessOverride.topic_key == (f"{stage}||{chapter}||{topic}" if stage else f"{chapter}||{topic}"),
        ).delete(synchronize_session=False)
        error_type = ""
        try:
            difficulty = int(meta.get("difficulty") or detail.get("difficulty") or 3)
        except (TypeError, ValueError):
            difficulty = 3
        existing = (
            db.query(Weakness)
            .filter(
                Weakness.student_id == student_id,
                Weakness.education_stage == stage,
                Weakness.catalog_version == version,
                Weakness.topic == topic,
                Weakness.subtopic == subtopic,
                Weakness.status == "pending",
            )
            .first()
        )
        if existing:
            existing.occurrences += 1
            if not existing.chapter:
                existing.chapter = chapter
            existing.error_type = error_type
            existing.difficulty = difficulty
        else:
            db.add(
                Weakness(
                    student_id=student_id,
                    education_stage=stage,
                    catalog_version=version,
                    chapter=chapter,
                    topic=topic,
                    subtopic=subtopic,
                    error_type=error_type,
                    difficulty=difficulty,
                    occurrences=1,
                )
            )


def synchronize_teacher_confirmation(
    db: Session,
    *,
    student: Student,
    teacher_id: int,
    grading_result: GradingResult,
    previous_details: list[dict],
    details: list[dict],
    detail_index: int,
) -> None:
    """Keep archive, analytics, and correction projections aligned after review."""
    context = resolve_archived_teaching_context(details, grading_result)
    for detail in details:
        if isinstance(detail, dict) and detail.get("teaching_context"):
            # Keep adopted legacy answer-contract inputs in their original
            # shape; the separately migrated archive carries the stage.
            stamp_teaching_snapshot(detail, context)
    profile = db.query(StudentProfile).filter(
        StudentProfile.grading_result_id == grading_result.id,
    ).first()
    if profile is not None:
        profile.education_stage = context.get("education_stage", "")
        profile.catalog_version = context.get("curriculum_version", "")
        profile.score = grading_result.score
        profile.max_score = grading_result.max_score
        profile.correct_rate = grading_result.correct_rate
        profile.scoring_version = grading_result.scoring_version
        profile.total_questions = grading_result.total_questions
        profile.correct_count = grading_result.correct_count
        profile.partial_count = grading_result.partial_count
        profile.wrong_count = grading_result.wrong_count
        profile.needs_review_count = grading_result.needs_review_count
        profile.set_details(details)

    knowledge_events = db.query(KnowledgeEvent).filter(
        KnowledgeEvent.grading_result_id == grading_result.id,
    ).order_by(KnowledgeEvent.id.asc()).all()
    attempts = db.query(QuestionAttemptEvent).filter(
        QuestionAttemptEvent.grading_result_id == grading_result.id,
    ).order_by(QuestionAttemptEvent.id.asc()).all()
    # A legacy path may have left only part of the question projection.  Always
    # reconcile by top-level question number before applying the teacher's edit;
    # ``project_question_attempts`` is idempotent for already present events.
    if grading_result.analytics_included and details:
        attempts = project_question_attempts(
            db,
            student=student,
            teacher_id=teacher_id,
            grading_result=grading_result,
            details=details,
            analytics_included=True,
            inclusion_reason=grading_result.inclusion_reason or "included",
            observation_period_id=grading_result.observation_period_id,
            assessment_purpose=grading_result.assessment_purpose or "standard",
            apply_correction=True,
            repair_existing=True,
        )
    # KnowledgeEvent has no question-number column.  Updating it by array index
    # is safe only when the legacy writer produced one row per detail; otherwise
    # leave it untouched and let the question-level snapshot remain authoritative.
    if 0 <= detail_index < len(details) and len(knowledge_events) == len(details):
        detail = details[detail_index]
        event = knowledge_events[detail_index]
        event.education_stage = context.get("education_stage", "")
        event.catalog_version = context.get("curriculum_version", "")
        new_result = normalize_attempt_result(detail.get("result"))
        event.result = new_result
        event.analytics_included = bool(grading_result.analytics_included) and new_result != "void"
        event.error_type = ""

    revise_question_attempt_projection(
        db,
        grading_result=grading_result,
        previous_details=previous_details,
        details=details,
        detail_index=detail_index,
    )

    if 0 <= detail_index < len(details):
        detail = details[detail_index]
        old_detail = previous_details[detail_index] if detail_index < len(previous_details) else {}
        old_result = normalize_attempt_result((old_detail or {}).get("result"))
        new_result = normalize_attempt_result(detail.get("result"))
        if (
            grading_result.analytics_included
            and new_result in ("partial", "wrong", "blank")
            and old_result not in ("partial", "wrong", "blank")
        ):
            parent_no = str(
                detail.get("parent_question_no")
                or detail.get("question_no")
                or detail.get("question_number")
                or detail_index + 1
            ).strip()
            attempt = next(
                (row for row in attempts if str(row.question_no or "").strip() == parent_no),
                None,
            )
            # Prefer the question-level snapshot, whose parent number is
            # explicit.  Falling back to the aligned legacy event is retained
            # only for old rows that predate QuestionAttemptEvent.
            meta = attempt.question_snapshot() if attempt is not None else {}
            if (
                not any(
                    meta.get(key) not in (None, "")
                    for key in ("chapter", "topic", "knowledge_point")
                )
                and detail_index < len(knowledge_events)
            ):
                event = knowledge_events[detail_index]
                meta = {
                    "question_bank_id": event.question_bank_id,
                    "chapter": event.chapter,
                    "topic": event.topic,
                    "difficulty": event.difficulty,
                    "error_type": sanitize_error_type(event.error_type),
                }
            scoped_detail = dict(detail)
            stamp_teaching_snapshot(scoped_detail, context)
            _upsert_weaknesses(db, student.id, [scoped_detail], [meta], teaching_context=context)


def _write_filesystem_archive(
    student: Student,
    category: str,
    exam_name: str,
    score: float | None,
    max_score: float | None,
    correct_rate: float | None,
    scoring_version: int,
    grading_contract_hash: str,
    total: int,
    correct: int,
    partial: int,
    wrong: int,
    needs_review: int,
    details: list[dict],
) -> None:
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    student_name = _safe_path_part(student.name, f"student_{student.id}")
    safe_exam_name = _safe_path_part(exam_name, date_str)
    contract_suffix = f"_{grading_contract_hash[:8]}" if grading_contract_hash else ""
    if category == "daily":
        subdir = os.path.join(ARCHIVE_DIR, student_name, "日常")
        fname = f"{date_str}_作业{contract_suffix}.json"
    elif category == "exam":
        subdir = os.path.join(ARCHIVE_DIR, student_name, "期中期末")
        fname = f"{safe_exam_name}{contract_suffix}.json"
    else:
        raise ValueError("category must be daily or exam")

    os.makedirs(subdir, exist_ok=True)
    archive_data = {
        "student_name": student.name,
        "category": category,
        "exam_name": exam_name,
        "score": score,
        "max_score": max_score,
        "correct_rate": correct_rate,
        "scoring_version": scoring_version,
        "grading_contract_hash": grading_contract_hash,
        "total": total,
        "correct": correct,
        "partial": partial,
        "wrong": wrong,
        "needs_review": needs_review,
        "details": details,
    }
    with open(os.path.join(subdir, fname), "w", encoding="utf-8") as f:
        json.dump(archive_data, f, ensure_ascii=False, indent=2)


class _ArchiveStudentSnapshot:
    """Small immutable-enough student view safe to use after a DB commit."""

    __slots__ = ("id", "name")

    def __init__(self, student_id: int, name: str):
        self.id = student_id
        self.name = name


def _make_filesystem_archive_job(
    student: Student,
    category: str,
    exam_name: str,
    score: float | None,
    max_score: float | None,
    correct_rate: float | None,
    scoring_version: int,
    grading_contract_hash: str,
    total: int,
    correct: int,
    partial: int,
    wrong: int,
    needs_review: int,
    details: list[dict],
) -> dict:
    """Snapshot archive inputs before SQLAlchemy expires them on commit."""
    return {
        "student_id": int(student.id),
        "student_name": str(student.name or ""),
        "category": category,
        "exam_name": exam_name,
        "score": score,
        "max_score": max_score,
        "correct_rate": correct_rate,
        "scoring_version": scoring_version,
        "grading_contract_hash": grading_contract_hash,
        "total": total,
        "correct": correct,
        "partial": partial,
        "wrong": wrong,
        "needs_review": needs_review,
        "details": details,
    }


def write_deferred_filesystem_archives(jobs: list[dict] | None) -> int:
    """Write archives after the caller's DB transaction has committed.

    File archives are a compatibility/export projection; the database remains
    the source of truth.  A slow or full archive volume therefore cannot hold a
    SQLite writer transaction open, and one failed file does not erase the
    committed grading result.
    """
    written = 0
    for job in list(jobs or []):
        try:
            _write_filesystem_archive(
                _ArchiveStudentSnapshot(job["student_id"], job["student_name"]),
                job["category"],
                job["exam_name"],
                job["score"],
                job["max_score"],
                job["correct_rate"],
                job["scoring_version"],
                job["grading_contract_hash"],
                job["total"],
                job["correct"],
                job["partial"],
                job["wrong"],
                job["needs_review"],
                job["details"],
            )
            written += 1
        except Exception as exc:
            logger.warning(
                "filesystem archive failed after commit for student %s: %s",
                job.get("student_id"),
                exc,
            )
    return written


def _insert_or_get_grading_result(
    db: Session,
    *,
    student: Student,
    category: str,
    exam_name: str,
    image_hash: str,
    grading_contract_hash: str,
    total_questions: int,
    correct_count: int,
    partial_count: int,
    wrong_count: int,
    needs_review_count: int,
    score: float | None,
    max_score: float | None,
    correct_rate: float | None,
    scoring_version: int,
    details: list[dict],
    ocr_raw_name: str,
    ocr_confidence: float,
    ocr_raw_json,
    grading_mode: str = "precise",
    analytics_included: bool = True,
    inclusion_reason: str = "included",
    observation_period_id: int | None = None,
    assessment_purpose: str = "standard",
    teaching_context=None,
) -> tuple[GradingResult, bool]:
    """Insert a new GradingResult, recovering from concurrent-duplicate races.

    Correctness rests on the DB unique index ``uq_grading_student_image_category_exam``
    (see models.GradingResult), NOT on the prior find_existing check, which is a
    non-atomic fast path. Two concurrent submissions of the same paper both miss
    find_existing and race to INSERT; the index guarantees only one row is written.

    The losing INSERT surfaces as either:
      - IntegrityError — its snapshot already saw the winner's committed row, or both
        race within one process; OR
      - OperationalError (SQLITE_BUSY_SNAPSHOT) — under WAL with multiple workers the
        loser's read snapshot predates the winner's commit. busy_timeout does NOT
        retry snapshot conflicts, so it bubbles up here.

    We wrap the INSERT in a SAVEPOINT so a failure rolls back only this row, leaving
    any earlier pending work in the session intact (quick_grade batches several papers
    before one commit). After rollback we re-query: if the winner's row is now visible
    we reuse it (created=False); if our snapshot is still too stale to see it, we
    re-raise — no duplicate was written, and the caller can safely retry.
    """
    grading_result = GradingResult(
        student_id=student.id,
        category=category,
        exam_name=exam_name,
        image_content_hash=image_hash or "",
        grading_contract_hash=grading_contract_hash or "",
        ocr_raw_name=ocr_raw_name or "",
        ocr_confidence=ocr_confidence or 0.0,
        ocr_raw_json=_as_json_text(ocr_raw_json),
        total_questions=total_questions,
        correct_count=correct_count,
        partial_count=partial_count,
        wrong_count=wrong_count,
        needs_review_count=needs_review_count,
        score=score,
        max_score=max_score,
        correct_rate=correct_rate,
        scoring_version=scoring_version,
        grading_mode=grading_mode or "precise",
        analytics_included=analytics_included,
        inclusion_reason=inclusion_reason,
        observation_period_id=observation_period_id,
        assessment_purpose=assessment_purpose,
        education_stage=(teaching_context or {}).get("education_stage", ""),
        catalog_version=(teaching_context or {}).get("curriculum_version", ""),
    )
    grading_result.set_details(details)

    # Empty hash is excluded from the partial unique index → never collides, so the
    # plain path is correct for it too (multiple failed-hash rows may coexist).
    try:
        with db.begin_nested():  # SAVEPOINT around just this INSERT
            db.add(grading_result)
            db.flush()
        return grading_result, True
    except (IntegrityError, OperationalError) as exc:
        # SAVEPOINT rollback (on exiting the begin_nested block) already detached the
        # failed-insert instance from the session — no manual expunge needed.
        winner = find_existing_grading_result(
            db, student.id, image_hash, category, exam_name, grading_contract_hash
        )
        if winner is not None:
            logger.info(
                "persist_grading_result: 并发去重命中 student=%s hash=%s → 复用已存在结果 #%s",
                student.id, (image_hash or "")[:12], winner.id,
            )
            return winner, False
        # Unique index guaranteed no duplicate was written, but our snapshot can't
        # yet see the winner. Surface so the request retries against fresh state.
        logger.warning(
            "persist_grading_result: 并发冲突但快照尚未可见获胜行 student=%s hash=%s (%s) — 交由上层重试",
            student.id, (image_hash or "")[:12], type(exc).__name__,
        )
        raise


def persist_grading_result(
    db: Session,
    *,
    student: Student,
    teacher_id: int,
    category: str,
    exam_name: str,
    image_path: str,
    image_hash: str,
    total_questions: int,
    correct_count: int,
    partial_count: int,
    wrong_count: int,
    needs_review_count: int,
    score: float | None,
    details: list[dict],
    max_score: float | None = None,
    correct_rate: float | None = None,
    scoring_version: int = 2,
    grading_contract_hash: str = "",
    teaching_context=None,
    knowledge_items: list[dict] | None = None,
    ocr_raw_name: str = "",
    ocr_confidence: float = 0.0,
    ocr_raw_json=None,
    grading_mode: str = "precise",
    assessment_purpose: str = "standard",
    template_id: str = "",
    correction_evidence_allowed: bool = True,
    commit: bool = True,
    archive_jobs: list[dict] | None = None,
) -> tuple[GradingResult, StudentProfile | None, bool]:
    """Persist a complete grading result and return (result, profile, created).

    If the same student/image/category/exam/frozen scoring contract was already persisted, related
    derived rows are repaired if missing and the existing result is returned.

    Idempotency is enforced atomically by the DB unique index on
    (student_id, image_content_hash, category, exam_name, grading_contract_hash); concurrent duplicate
    submissions are de-duplicated in _insert_or_get_grading_result rather than
    producing a second row. See that helper for the WAL/multi-worker race details.
    """
    category = str(category or "daily").strip().lower()
    if category not in VALID_GRADING_CATEGORIES:
        raise ValueError("category must be daily or exam")
    exam_name = exam_name or ""
    details = [dict(detail) if isinstance(detail, dict) else detail for detail in (details or [])]
    context = resolve_grading_teaching_context(details, teaching_context)
    for detail in details:
        if isinstance(detail, dict):
            stamp_teaching_snapshot(detail, context)
    template_id = str(template_id or "").strip()
    if template_id:
        for index, detail in enumerate(details):
            if not isinstance(detail, dict):
                continue
            parent_no = str(
                detail.get("parent_question_no")
                or detail.get("question_no")
                or detail.get("question_number")
                or index + 1
            ).strip()
            detail.setdefault("template_question_id", f"{template_id}:{parent_no}")
    assessment_purpose = str(assessment_purpose or "standard").strip() or "standard"
    observation = active_observation_period(db, student.id)
    analytics_included = observation is None
    inclusion_reason = "included" if analytics_included else "observation_mode"
    observation_period_id = observation.id if observation else None
    scoring_version = int(scoring_version or 2)
    # v1 data predates frozen scoring contracts and retains legacy idempotency.
    # Every v2 write carries a deterministic snapshot hash derived from the
    # confirmed rule inputs embedded in its details.
    contract_hash = "" if scoring_version < 2 else (
        str(grading_contract_hash or "").strip()
        or build_grading_contract_hash(details, category, scoring_version)
    )
    # New tasks must bind stage even when older callers supply a legacy hash.
    if context:
        contract_hash = build_grading_contract_hash(details, category, scoring_version, teaching_context=context)
    if needs_review_count > 0:
        score = None
        correct_rate = None
    elif correct_rate is None:
        correct_rate = round(correct_count / total_questions, 4) if total_questions else 0.0
    if category != "exam":
        score = None
        max_score = None
    elif scoring_version >= 2:
        if max_score is None or float(max_score) <= 0:
            raise ValueError("v2 exam results require a positive max_score")
        if score is not None and not 0 <= float(score) <= float(max_score):
            raise ValueError("exam score must be between 0 and max_score")
    existing = find_existing_grading_result(
        db, student.id, image_hash, category, exam_name, contract_hash,
    )
    created = existing is None

    if existing:
        grading_result = existing
        if not grading_result.get_details() and details:
            grading_result.set_details(details)
    else:
        grading_result, created = _insert_or_get_grading_result(
            db,
            student=student,
            category=category,
            exam_name=exam_name,
            image_hash=image_hash,
            grading_contract_hash=contract_hash,
            total_questions=total_questions,
            correct_count=correct_count,
            partial_count=partial_count,
            wrong_count=wrong_count,
            needs_review_count=needs_review_count,
            score=score,
            max_score=max_score,
            correct_rate=correct_rate,
            scoring_version=scoring_version,
            details=details,
            ocr_raw_name=ocr_raw_name,
            ocr_confidence=ocr_confidence,
            ocr_raw_json=ocr_raw_json,
            grading_mode=grading_mode,
            analytics_included=analytics_included,
            inclusion_reason=inclusion_reason,
            observation_period_id=observation_period_id,
            assessment_purpose=assessment_purpose,
            teaching_context=context,
        )
        if not created and not grading_result.get_details() and details:
            grading_result.set_details(details)

    context = grading_result_teaching_context(grading_result)
    profile = (
        db.query(StudentProfile)
        .filter(StudentProfile.grading_result_id == grading_result.id)
        .first()
    )
    pending_archive_jobs: list[dict] = []
    if not profile:
        profile = StudentProfile(
            student_id=student.id,
            education_stage=context.get("education_stage", ""),
            catalog_version=context.get("curriculum_version", ""),
            category=category,
            exam_name=exam_name,
            grading_result_id=grading_result.id,
            score=grading_result.score,
            max_score=grading_result.max_score,
            correct_rate=grading_result.correct_rate,
            scoring_version=grading_result.scoring_version,
            total_questions=grading_result.total_questions,
            correct_count=grading_result.correct_count,
            partial_count=grading_result.partial_count,
            wrong_count=grading_result.wrong_count,
            needs_review_count=grading_result.needs_review_count,
            image_path=image_path,
            image_content_hash=image_hash or "",
            analytics_included=grading_result.analytics_included,
            inclusion_reason=grading_result.inclusion_reason,
            observation_period_id=grading_result.observation_period_id,
            assessment_purpose=grading_result.assessment_purpose,
        )
        profile.set_details(grading_result.get_details())
        db.add(profile)
        db.flush()
        pending_archive_jobs.append(_make_filesystem_archive_job(
            student,
            category,
            exam_name,
            grading_result.score,
            grading_result.max_score,
            grading_result.correct_rate,
            grading_result.scoring_version,
            grading_result.grading_contract_hash,
            grading_result.total_questions,
            grading_result.correct_count,
            grading_result.partial_count,
            grading_result.wrong_count,
            grading_result.needs_review_count,
            grading_result.get_details(),
        ))

    event_exists = (
        db.query(KnowledgeEvent.id)
        .filter(KnowledgeEvent.grading_result_id == grading_result.id)
        .first()
    )
    if grading_result.analytics_included and not event_exists:
        for idx, detail in enumerate(grading_result.get_details()):
            if not isinstance(detail, dict):
                continue
            event = _event_from_detail(
                student,
                teacher_id,
                grading_result.id,
                detail,
                _meta_for(grading_result.get_details(), knowledge_items, idx),
                teaching_context=context,
            )
            if event:
                db.add(event)
        _upsert_weaknesses(db, student.id, grading_result.get_details(), knowledge_items, teaching_context=context)

    has_teacher_regrade = bool(
        db.query(GradingCorrection.id).filter(
            GradingCorrection.grading_result_id == grading_result.id,
        ).first()
    )
    project_question_attempts(
        db,
        student=student,
        teacher_id=teacher_id,
        grading_result=grading_result,
        details=grading_result.get_details(),
        knowledge_items=knowledge_items,
        analytics_included=bool(grading_result.analytics_included),
        inclusion_reason=grading_result.inclusion_reason or "included",
        observation_period_id=grading_result.observation_period_id,
        assessment_purpose=grading_result.assessment_purpose or "standard",
        apply_correction=bool(correction_evidence_allowed) and not has_teacher_regrade,
        repair_existing=bool(correction_evidence_allowed) and not has_teacher_regrade,
    )

    if commit:
        db.commit()
        write_deferred_filesystem_archives(pending_archive_jobs)
    elif archive_jobs is not None:
        # The caller owns the larger transaction and flushes only after commit.
        archive_jobs.extend(pending_archive_jobs)
    else:
        # Preserve the historical direct-call contract for external callers that
        # explicitly opt out of commit without supplying a deferred queue.
        write_deferred_filesystem_archives(pending_archive_jobs)
    return grading_result, profile, created
